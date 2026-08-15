# compactme

让模型给自己发 `/compact`：v2 为**事件驱动两段注入**——MCP 工具只写任务并 fork
一个 per-pane watcher，watcher 确认屏幕出现 `⎿ Compacted` 特征串后，才把唤醒词
动态注入（post-compact 动态注入）。

```
compactme() ──写 task.json──▶ watcher（独立进程，每 pane 一个，多实体互不冲突）
                                │ WAIT_SETTLE  等模型写完收尾总结（默认 6s）
                                │ INJECTING    C-u → /compact 文本 → 1.5s → Enter（分键防抖）
                                │ WATCHING     轮询 capture-pane（只读快照，无换页冲突）：
                                │                Compacting → 等；⎿ Compacted → 注入唤醒词
                                │                esc to interrupt → 被吞了，停下重试（≤2）
                                │ 兜底          超时仍注入唤醒词（含重建线索，晚到无损）
```

单发 `/compact` 只会让新上下文停在待机态；唤醒词是恢复任务的唯一保证，因此工具会**硬校验**唤醒词必须包含重建线索（文件路径、TaskList 等），不合格直接拒绝执行。

## 组成

```
compactme/
├── .claude-plugin/plugin.json   # 插件清单
├── .mcp.json                    # MCP server 注册
├── mcp/compactme_mcp.py         # compactme() + context_stats()（状态栏水位解析）
├── mcp/watcher.py               # 事件驱动注入状态机（per-pane 独立进程）
├── skills/compact-me/SKILL.md   # 可用时机与唤醒词纪律
└── tests/fake_claude.py         # 模拟 TUI 的端到端测试靶
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

- 仅适用于 tmux 内运行的 Claude Code（依赖 `$TMUX_PANE`）
- 多会话并行时 `context_stats` 的 transcript 回退定位可能拿错（按 mtime 取最新），优先走 `CLAUDE_SESSION_ID`
- 桌面 app / 无终端环境无注入面，不可用
