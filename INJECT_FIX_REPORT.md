# compactme 注入通道失效调查报告（2026-10-30）

> 调查人：ops 会话（用户授权点名的调查任务）；仓：compactme v0.3.3 → 修复后 v0.3.4。
> 现象：ClaudeCode 升级 **2.1.312**（2026-10-30 08:35 安装，原生二进制分发）后，watcher
> 用 `tmux send-keys -l` 注入的 `/compact` 被新版 TUI 当成**普通消息文本**发给模型，
> 两连挂（`/tmp/compactme/0/watcher.log` 08:51/08:53 attempt 1/2）。旧版（Node cli.js
> 时代，2.1.288 及之前）同样注入一直成功（同日志 15:32/19:38 段历史成功轮次）。

## 一、事故链证据（transcript 铁证）

生产现场：pane %0（backend 会话，claude pid 629918，08:39 启动于升级之后 = 新版），
watcher pid 632113（08:50 起，现已退出）。

`4de6b01d….jsonl`（backend 会话 transcript，timestamp 为 UTC）：

| UTC 时刻 | 条目 | 说明 |
|---|---|---|
| 00:51:02 | assistant 收尾总结 | 模型写完收尾，TUI idle |
| **00:51:03** | user `'/compact 压缩前保留：①今日六批收官全景：…'` | **attempt 1 注入（idle 态）→ 被当普通消息** |
| 00:51:11 | assistant `压缩指令已注入，执行压缩…` | 模型把被吞的命令当文本回了话 |
| **00:53:26** | user `'/compact 压缩前保留：…'` | **attempt 2（busy 排队 18s 后提交）同样被吞** |
| — | 无 `<command-name>/compact</command-name>` 条目 | **TUI 从未解析执行该命令** |
| — | PreCompact hook 未触发、无 isCompactSummary | watcher 两通道全盲 → 90s×2 超时 |

idle 与 busy 两种形态都被吞 → 排除 busy 队列因素，指向 TUI 输入解析本身变化。

## 二、根因

### 2.1 新版 TUI 的长度折叠（paste blob）

2.1.312 起包改为**原生二进制分发**（`install.cjs` postinstall 把 ~293MB ELF 拷到
`bin/claude.exe`，不再有可读 cli.js）。对二进制做 strings 逆向，输入处理函数
（偏移 ~53.6MB 处，minified Ink/React 组件）逻辑解码：

```js
Rf = 规范化后的本次到达文本块
if (输入框为空) { tG = WS(Rf); if (tG !== "prompt") { Tn(tG); Rf = UD(Rf) } }  // 命令模式检测
if (Rf.length > S8 || 行数 > TB || 是链接) {
    g("input_paste_large")                       // ★ 超阈值 → 整块折叠为 paste blob
    pastedContents[id] = {type:"text", content:Rf}   // 输入框只剩 [Pasted text #N] 占位
} else {
    dv(Rf, { isTyped: true })                    // 短块 → 正常 typed 路径
}
```

- 阈值 S8 为编译期常量；**实测边界：426 字符过、860 字符折叠**（见 §3 实验矩阵）。
- `input_paste_large` 是官方 telemetry 事件名（二进制内嵌事件清单可查），此行为是有意设计。
- 折叠后的 blob 提交（Enter）**不再走 slash 命令解析**，直接作为 prompt 文本发送。
- 旧版（Node 时代）即使长输入折叠成 blob，**提交时仍解析命令**（watcher docstring
  43-49 行记录的「折叠 blob 提交」正是历史成功路径）——这个兼容行为在新版没了。

### 2.2 compact_cmd 膨胀把注入推过阈值

`compactme_mcp.py`（v0.3.3）构造：

```python
compact_cmd = f"/compact {compact_instructions}（压缩完成后将收到唤醒词：{wakeup_word}）"
```

**唤醒词全文被拼进命令**，生产实例 860~1100+ 字符，必超折叠阈值 → 必被吞。
这是两连挂的直接原因；同时也是劣化形态（用户裁决）：压缩准备（任务状态/重建
线索落盘）本就该在调用 compactme 前完成，命令只该带一句摘要侧重。

### 2.3 实验矩阵（独立 tmux session，claude 2.1.312，实测）

| 实验 | 注入形态 | 长度 | 结果 |
|---|---|---|---|
| exp1 | `send-keys -l "/compact 填充…"` + Enter | 426 | ✅ 触发：transcript 出现 `<command-name>/compact</command-name>` 条目 + PreCompact hook（09:09:17）+ Compacting UI + continuation summary |
| exp2 | 同上 | 860 | ❌ 被吞：仅 user 原文条目，无 command-name、无 hook；模型当消息回复（完整复现生产两连挂） |
| exp3 | `send-keys -l "/compact"` + Enter | 7 | ✅ 触发：PreCompact hook mtime 与注入时刻完全一致（09:15:20.184），Compacting UI 出现 |

阈值落在 (426, 860] 区间；纯 `/compact` 短命令走 typed 路径稳定执行。

## 三、修复（v0.3.4，两层）

### 3.1 源头瘦身：`mcp/compactme_mcp.py`

- compact_cmd 不再拼唤醒词全文（唤醒词由 watcher 压缩完成后独立投递，本就不该进命令）。
- 新增 `SAFE_CMD_MAX = 200`（实测挂点 860 的一半以下、426 通过值的一半，双裕量），
  `/compact {instructions}` 超长截断加 `…`。
- 工具 docstring 同步新契约：instructions 一句话侧重；重建线索落盘是调用前准备，
  不写进命令。

### 3.2 注入防御：`mcp/watcher.py`

- 新增 `safe_compact_cmd(cmd)`：超长（>SAFE_CMD_MAX）或前缀异常 → 降级为纯
  `/compact` 注入（保命令可执行，丢摘要侧重——无损降级）。
- `run()` 注入前统一取安全形态并记降级日志。上游再膨胀（旧 task.json 残留、模型
  又写长 instructions）也保证注入走 typed 路径。
- 唤醒词投递（deliver_wakeup）**不改**：实测 blob 提交后 transcript 记**完整原文**
  （exp2 条目 len=860 全文），`head` 匹配判据仍有效；且唤醒词本就以普通消息形态
  进对话，被折叠无碍。

### 3.3 测试

- `tests/test_safe_compact_cmd.py`（新增 7 用例）：短命令透传 / 超长降级 / 前缀归一 /
  边界（恰 SAFE_CMD_MAX 含端、+1 降级）/ 降级闭环安全不变量。
- `tests/e2e.sh`（断言以 `/compact` 前缀口径匹配，天然兼容降级）+ 注释更新。
- 结果：**pytest 24 passed**（17 旧回归 + 7 新）；**e2e normal 模式 PASS**
  （compact 1 次 / 唤醒词 1 次 / state=DONE，fake_claude 全链路）。

## 四、真实链路验证

1. **单测 + e2e**（见 §3.3）。
2. **真实 2.1.312 会话（inject-test，ops 仓 job 会话）**：构造 661 字符劣化 compact_cmd
   → watcher 日志 `compact_cmd degraded to pure /compact (len=661 > 200 …)` →
   注入 1s 内 PreCompact hook 落盘 → transcript 出现 `/compact` +
   `<command-name>` 条目。**降级→注入→命令被接受**全链路实证。
3. **生产交互会话（frontend，用户授权）· 全周期闭环**：注入纯 `/compact`
   （09:48:20）→ PreCompact hook 09:48:21（1s）→ Compacting → **PostCompact hook
   09:50** → 上下文 229.5k → **0/1M**，"Skills restored"，会话干净 idle。
   注入通道在新版 2.1.312 标准交互形态上从注入到压缩确认**全链路恢复**。
   （另一次 09:33:03 的注入同样 1s 触发 PreCompact，压缩进行中被 09:36 整机
   重启打断——与注入通道无关。）

## 五、遗留观察（非本次 patch 范围，记录备查）

1. **inject-test 压缩未完成疑点**：09:21:40 PreCompact 触发、命令被接受，但 240s 内
   无 PostCompact / isCompactSummary，且唤醒词注入后模型即时响应（说明无压缩在
   阻塞）——**PreCompact 触发 ≠ 压缩必然执行**，新版存在「命令接受后压缩流程未跑」
   的形态（该会话是 job 形态，条件未复现干净）。watcher 的 PRECOMPACT_TIMEOUT /
   COMPACT_DONE_TIMEOUT 语义下这会落入 fallback deliver（唤醒词晚到无损），但值得
   后续观察。
2. **唤醒词三连发假阴性**（同 2 场景暴露）：`active_transcript` 按 mtime 锚定在**同
   projects 目录多会话**（本调查 ops 仓双会话并存）时会锚错文件，且 post hook 缺失时
   hint 失效 → 送达判据假阴性 → 三轮重发。生产单会话场景无此竞争；根治思路是 deliver
   阶段复用 pre 标记的 transcript_path 或按 pane 会话过滤，留作后续小改。
3. **无头（-p）形态从未测过**（用户指出）：compactme 依赖 tmux pane 注入，纯 headless
   下无 pane，当前架构不适用；如需支持要走 `claude -p`/SDK 通道，另行立项。
4. 09:36 系统重启（WSL 整机）打断了 frontend 第一次压缩验证，/tmp/compactme 目录被
   清空（hook 会自动重建，无持久化影响）。

## 六、改动清单

| 文件 | 改动 |
|---|---|
| `mcp/watcher.py` | +`SAFE_CMD_MAX`、+`safe_compact_cmd()`、run() 注入前降级、docstring v0.3.4 事故段 |
| `mcp/compactme_mcp.py` | compact_cmd 瘦身（不拼唤醒词、SAFE_CMD_MAX 截断）、docstring 契约更新 |
| `tests/test_safe_compact_cmd.py` | 新增 7 用例 |
| `tests/e2e.sh` | CMD 注释补充 v0.3.4 降级路径说明 |
