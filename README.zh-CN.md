# compactme

**让模型给自己发 `/compact`——压缩完再把自己叫醒。**

Claude Code 插件(MCP server + hooks):模型自主安排 `/compact`,压缩完成后由
watcher 把一条携带任务重建线索的**唤醒词**注入回**同一个会话**。

[English README](README.md)

## 为什么需要它

长会话必然退化——上下文越长注意力越稀释。`/compact` 能回收窗口,但压缩后的
新上下文只会停在待机态:没有任何东西告诉它"把任务捡起来接着干"。compactme
补上这一环:

1. 模型把 `compactme` 作为**本轮最后一个工具调用**发起,随后给用户写收尾总结;
2. 独立 watcher 把 `/compact` 注入本 pane 输入框;
3. hooks + transcript 双通道确认压缩**真正完成**后,注入模型事先拟好的唤醒词
   ——内含重建线索(*去哪读:文件路径 / TaskList、读什么、怎么续*);
4. 压缩后的会话靠唤醒词自主恢复任务。

没有重建线索的唤醒词毫无价值,因此工具会**硬校验**唤醒词必须携带线索
(路径、TaskList 标记等),不合格直接拒绝执行。

## 快速开始

**前置条件:** Linux 或 WSL · tmux · Python ≥ 3.10 · [fastmcp]

```bash
pip install --user fastmcp     # 或用 pipx/venv,只要下面这条能通过:
python3 -c "import fastmcp"    # 必须 exit 0(MCP server 由 `python3` 拉起)
```

```bash
# 1. 先开 tmux,再在 tmux 里面启动 Claude Code(不要在裸终端里跑)
tmux new -s claude
cd 你的项目/ && claude

# 2. 在 Claude Code 里:
#    /plugin marketplace add RegretSSS/compactme
#    /plugin install compactme@compactme
# 3. 重启 Claude Code(或 reload 插件),让 PreCompact/PostCompact hooks 生效
```

验证:对 Claude 说 *"调用 context_stats"*,应返回当前上下文水位;若报
`$TMUX_PANE` 未检测到,说明 Claude 没跑在 tmux 里。

之后正常干活即可。上下文变重时(≥ 400k 无条件压;≥ 200k 且处于天然断点建议压),
模型可自行压缩——你会看到它发一段收尾总结、调用工具、安静等压缩、然后从唤醒词
处恢复任务。也可以主动提示它:*"上下文很重了,自己压缩一下继续。 "*

本地开发安装:

```bash
git clone https://github.com/RegretSSS/compactme
claude plugin validate /path/to/compactme
# 然后在 Claude Code 里:/plugin add /path/to/compactme
```

## tmux / pane 环境设置(不工作先看这里)

compactme 唯一的注入面是 pane 的输入框(走 `tmux send-keys`),一切前提是
Claude Code 跑在 **tmux pane** 里:

```
tmux pane ($TMUX_PANE)
  └─ shell
      └─ claude  ← 会话进程;/proc/<pid>/cwd = 会话目录
          └─ ~/.claude/projects/<目录名转义>/<mtime 最新>.jsonl
             = 活跃 transcript
```

- **必须在 tmux 里启动 claude。** `tmux new -s claude` 后在 pane 里运行
  `claude`。裸终端、桌面 app、非 tmux 复用器都没有注入面(工具会诚实报
  `$TMUX_PANE 未检测到`)。
- **启动 claude 前先 cd 进项目目录。** watcher 靠 pane 进程树里的 claude 进程
  的 `/proc/<pid>/cwd` 定位会话目录 → `~/.claude/projects/<转义目录>/` →
  mtime 最新 `.jsonl`。启动后 shell 再 cd 走不影响——锚点是 claude 进程的
  cwd,不是 shell 的。
- **一 pane 一会话。** 任务、watcher、注入目标全部按 pane 隔离,不同 pane /
  不同目录并行会话互不冲突。**同 cwd** 双会话同时压缩时 hook 扫描可能串
  (最坏后果:压缩开始被提前确认——投递判据独立,不受影响)。
- **ssh 远程没问题**,只要 tmux 和 claude 在同一台(远程)机器上。会话 pane
  避免嵌套 tmux-in-tmux。
- **context_stats 加分项:** 状态栏若显示 `🧠 125.7k/1M 12.6%`,工具优先解析
  (天然按对话隔离);否则回退 transcript 的 usage 字段。配置方法见
  [状态栏水位设置](#可选状态栏上下文水位推荐)小节,一分钟搞定。

运行时产物在 `/tmp/compactme/<pane>/`(`task.json`、`watcher.log`),随时可删,
不碰系统其他任何东西。

## 可选:状态栏上下文水位(推荐)

`context_stats()` 识别上下文水位**优先走 pane 状态栏**(`🧠 已用/窗口 百分比`
——实时、天然按对话隔离);没配状态栏则回退 transcript 的 usage 字段(可用,
但多会话并行时可能拿错文件)。Claude Code 默认状态栏不显示水位,因此仓库自带
一个零依赖的最小 statusline 脚本:

```jsonc
// ~/.claude/settings.json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /compactme绝对路径/statusline/context_statusline.py"
  }
}
```

重载 Claude Code 后状态栏即显示如 `🧠 121.4k/1.0M 12.1%`。窗口默认 200k,
1M 窗口模型加环境变量:

```jsonc
"command": "COMPACTME_CONTEXT_WINDOW=1000000 python3 /.../context_statusline.py"
```

(脚本读会话 transcript 最新一条 assistant `usage`——input + cache 创建 +
cache 读取,与 Claude Code 自身的 token 记账口径一致。)

## 工作原理

```
compactme() ──写 task.json（pane）──▶ watcher（独立进程，每 pane 一个）
                                                   │ 定位         claude 进程 cwd → projects 子目录
                                                   │              （session_id 仅信息性——compact 会
                                                   │               切 session id 新文件、pane environ
                                                   │               固化启动值，按 sid 定位在压缩后
                                                   │               必失效，2026-09-18 事故实证）
                                                   │ WAIT_SETTLE  等模型写完收尾总结
                                                   │ INJECTING    清框 → /compact + Enter
                                                   │              → 等 PreCompact hook 标记
                                                   │                （扫描式：ts 新于 watcher_start
                                                   │                 且 transcript_path 目录匹配；
                                                   │                 90s 覆盖 busy 期输入排队；
                                                   │                 swallow 被 LLM 当消息吞时不触发，
                                                   │                 超时自然重试 ≤2）
                                                   │ COMPACTING   等 PostCompact 标记；兜底 =
                                                   │              transcript isCompactSummary
                                                   │              （hook 未注册的降级通道）。绝不重注
                                                   │ DELIVERING   清框 → 唤醒词 + Enter 连发 →
                                                   │              transcript 新增 user 条目含 head
                                                   │ 兜底          重试耗尽/全局超时仍投递（晚到无损）

hooks（插件自注册，hooks/hooks.json）：
  PreCompact / PostCompact → compact_hook.py 写 /tmp/compactme/hooks/<sid>.pre/.post.json
  （stdin JSON 带 session_id / transcript_path / trigger；hook 毫秒级、永不阻塞会话）

为什么弃屏幕判据（v0.2.x 四次生产事故，全是屏幕形态问题）：
  折叠 paste（明文不上屏）→ 二次注入展开；折行（tail 落无前缀续行）→ 位置判据；
  窗口错位 → 全屏搜；布局漂移（输入框动态高度+todo 面板）→ 无解——2026-09-17
  pane %3 事故（busy 排队双发 /compact + 唤醒词明文在框却判未落地）后换轨。
transcript 优势（同事故 transcript 铁证）：写入先于 TUI 渲染 10s+；折叠 blob
提交后记录完整原文（屏幕只显 [Pasted text #N]）——屏幕雷区全部免疫。

为什么弃 session_id 锚点（v0.3.2，2026-09-18 第三次试用事故）：
  compact 时 Claude Code 开新 continuation transcript（**新 session id 新文件**），
  pane 进程 environ 的 CLAUDE_CODE_SESSION_ID 固化在启动值永不更新——按 sid 找
  transcript / 等 hook 标记双通道全盲（盯 392726e5，标记与摘要实际在 b1935f68
  名下），90s×3 重注双发。改锚 pane cwd：transcript = projects 子目录 mtime
  最新 jsonl（活跃文件，压缩切换自动跟上）；hook 标记 = 扫描式匹配（不预知 sid）。

唤醒词送达判据 = transcript **新增** user 条目（注入前行数做基线）：
  含 head（词首 12 字符）且非 /compact 前缀（compact_cmd 内嵌唤醒词全文——
  v0.2.2 事故形态）且非 isCompactSummary（摘要可能复述唤醒词）。
注入+Enter 直发覆盖三形态：idle 明文 / busy 输入排队（忙完按序提交，条目
  最终出现，轮前复查 + 30s 验证窗兜住）/ 折叠 blob 直接提交（transcript 记
  原文）；文本被吞则 Enter 提交空框（无害）→ 无条目 → 重注。三轮退避重注，
  仍无条目 → state=FAILED 落盘（不假 DONE）+ 屏幕底部 dump 留痕。
```

## 工具

- `compactme(compact_instructions, wakeup_word, settle_seconds=6.0)` — 派发
  watcher。必须作为本轮**最后一个**工具调用,之后在 settle 窗口内写完收尾
  总结并结束回合。唤醒词缺重建线索会被拒绝。
- `context_stats()` — 上下文水位:优先解析本 pane 状态栏 `🧠 125.7k/1M 12.6%`,
  回退 session transcript 的 usage 字段。

## 限制

- **仅 Linux / WSL** — watcher 读 `/proc/<pid>/environ` 与 `/proc/<pid>/cwd`,
  macOS 不支持。
- **仅 tmux** — 注入依赖 `$TMUX_PANE`;桌面 app / 裸终端无注入面。
- hook 标记依赖插件 reload 后 hooks.json 生效;未生效时自动降级 transcript
  兜底通道(isCompactSummary)。压缩若切换 transcript 文件,该兜底通道会失效
  (新文件行数 < 基线)——由 hook 主路兜住。
- 同 cwd 双会话同时压缩时 hook 扫描可能串(后果仅提前确认压缩开始,投递判据独立)。
- 多会话并行时 `context_stats` 的 transcript 回退定位可能拿错(按 mtime 取最新),
  优先走 `CLAUDE_CODE_SESSION_ID`。
- 压缩本身仍会丢细节——唤醒词的线索质量决定任务恢复质量上限。工具只强制
  最低标准,不保证完美。

## 测试

```bash
python3 -m pytest tests/test_deliver_wakeup.py -q   # 17 条用例：happy/delayed 复查/
                                                    #   全失败/残留条目回归/诚实失败/
                                                    #   压缩切 transcript 回归/scan 判据直测/
                                                    #   mtime 切换/wait_until 语义
bash tests/e2e.sh                                   # e2e 七模式：normal/swallow/eat_enter/
                                                    #   lost_text/paste_collapse/busy_defer/
                                                    #   switch_sid（fake TUI 靶）
```

## 许可

[MIT](LICENSE)
