# compactme

让模型给自己发 `/compact`：v0.3.0 为 **hooks + transcript 双通道信号源**——MCP 工具
只写任务并 fork 一个 per-pane watcher；watcher 注入 `/compact` 后靠 **PreCompact /
PostCompact hook 标记**确认压缩生命周期，靠 **session transcript 增量条目**确认唤醒词
真实送达。屏幕抓取仅剩注入动作与失败留痕。

```
compactme() ──写 task.json（pane + session_id）──▶ watcher（独立进程，每 pane 一个）
                                                   │ WAIT_SETTLE  等模型写完收尾总结
                                                   │ INJECTING    清框 → /compact + Enter
                                                   │              → 等 PreCompact hook 标记
                                                   │                （90s 覆盖 busy 期输入排队；
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

唤醒词送达判据 = transcript **新增** user 条目（注入前行数做基线）：
  含 head（词首 12 字符）且非 /compact 前缀（compact_cmd 内嵌唤醒词全文——
  v0.2.2 事故形态）且非 isCompactSummary（摘要可能复述唤醒词）。
注入+Enter 直发覆盖三形态：idle 明文 / busy 输入排队（忙完按序提交，条目
  最终出现，轮前复查 + 30s 验证窗兜住）/ 折叠 blob 直接提交（transcript 记
  原文）；文本被吞则 Enter 提交空框（无害）→ 无条目 → 重注。三轮退避重注，
  仍无条目 → state=FAILED 落盘（不假 DONE）+ 屏幕底部 dump 留痕。
```

单发 `/compact` 只会让新上下文停在待机态；唤醒词是恢复任务的唯一保证，因此工具会**硬校验**唤醒词必须包含重建线索（文件路径、TaskList 等），不合格直接拒绝执行。

## 组成

```
compactme/
├── .claude-plugin/plugin.json   # 插件清单
├── .mcp.json                    # MCP server 注册
├── hooks/hooks.json             # PreCompact/PostCompact hook 注册（v0.3）
├── hooks/compact_hook.py        # hook 落地：stdin JSON → 标记文件（毫秒级）
├── mcp/compactme_mcp.py         # compactme() + context_stats()（状态栏水位解析）
├── mcp/watcher.py               # hooks+transcript 信号源状态机 + 投递协议
├── skills/compact-me/SKILL.md   # 可用时机与唤醒词纪律
├── tests/test_deliver_wakeup.py # 单元测试（14 路径：happy/delayed 复查/全失败/
│                                #   残留条目回归/无 transcript 诚实失败/run 主干/
│                                #   scan 判据直测×5/hook 陈旧拒绝+清删/
│                                #   wait_until 语义直测）
├── tests/fake_claude.py         # 模拟 Claude Code 的 e2e 靶（raw 输入+折行渲染+
│                                #   transcript 写盘+hook 标记+折叠 paste+六模式）
└── tests/e2e.sh                 # e2e 六模式：normal/swallow/eat_enter/lost_text/
                                 #   paste_collapse/busy_defer
```

## 安装

```bash
pip install --user fastmcp   # 唯一依赖
claude plugin validate /home/remedy/workspace/compactme
# 然后在 Claude Code 里：/plugin add /home/remedy/workspace/compactme
```

## 工具

- `compactme(compact_instructions, wakeup_word, settle_seconds=6.0)` — 派发 watcher。必须作为本轮**最后一个**工具调用，之后在 settle 窗口内写完收尾总结并结束回合
- `context_stats()` — 优先解析本 pane 状态栏的 `🧠 125.7k/1M 12.6%`（天然按对话隔离），回退 session transcript 的 usage 字段

## 限制

- 仅适用于 tmux 内运行的 Claude Code（依赖 `$TMUX_PANE`）与 `CLAUDE_CODE_SESSION_ID`（v0.3 信号源定位，缺失拒绝执行）
- hook 标记依赖插件 reload 后 hooks.json 生效；未生效时自动降级 transcript 兜底通道（isCompactSummary）
- 多会话并行时 `context_stats` 的 transcript 回退定位可能拿错（按 mtime 取最新），优先走 `CLAUDE_CODE_SESSION_ID`
- 桌面 app / 无终端环境无注入面，不可用
