# compactme

让模型给自己发 `/compact`：v0.2.6 为**事件驱动两段注入 + ❯ 判据投递协议**——MCP 工具
只写任务并 fork 一个 per-pane watcher，watcher 确认屏幕出现 `⎿ Compacted` 特征串后，
才把唤醒词动态注入（post-compact 动态注入），并用 ❯ 用户消息形态验证**真正发送成功**。

```
compactme() ──写 task.json──▶ watcher（独立进程，每 pane 一个，多实体互不冲突）
                                │ WAIT_SETTLE  等模型写完收尾总结（默认 6s）
                                │ INJECTING    BSpace 清框 → /compact 文本 → 落地确认 → Enter
                                │ WATCHING     轮询 capture-pane（只读快照，无换页冲突）：
                                │                Compacting → 等；⎿ Compacted → 投递唤醒词
                                │                esc to interrupt → 被吞了，停下重试（≤2）
                                │ 兜底          超时仍投递唤醒词（含重建线索，晚到无损）

deliver_wakeup 可靠投递（v0.2.5，四轮生产事故的教训）：
  成功判据 = ❯ 用户消息形态（字节级实测）：输入框行是 ❯+nbsp(\xa0)，
  已提交用户消息是 ❯+普通空格——「文本在框」vs「已发出」由此可靠区分，
  不依赖行区域划分（压缩刚完成时消息流只有几行，新消息会落进底部窗口）。
  最后一条 ❯+空格消息含唤醒词开头、非 /compact 前缀（compact_cmd 残留
  内嵌唤醒词全文，v0.2.2 两次 already-sent 误判漏发的形态，靠前缀排除）。
  busy 不作成功信号：压缩后模型自动继续也会 busy，误判即假 DONE 漏发。
  BSpace×1200 清框（C-u 实测无清行语义）→ 注入 → 落地确认 → Enter → 等 ❯ 判据
  in_input 位置判据（v0.2.6）：tail 所在行必须在输入框提示符行（❯+nbsp）下方
  或同行——提交消息的折行续行（无前缀、含 tail）落在提示符上方，按行前缀
  区分不了，曾把已提交消息误当「文本在框」→ sent 永假 → 三轮重发双发
  （179 宽下唤醒词恰好单行不折行，漏洞被宽度巧合掩盖，120 宽才暴露）
  折叠 paste 态（v0.2.5）：busy 期大块注入被折叠为 [Pasted text #N] 占位符、
  明文不上屏（dump 留痕定位：三轮注入编号递增=文本全到、tail 判据恒假）→
  主路：再注入一次模拟「再粘贴一次」展开为明文（paste again to expand）；
  兜底：展开失败 → 折叠 blob 直接 Enter 提交（用户手动验证过），成功判据 =
  提示消失 + 编号大于入口基线的新 blob 消息行（历史/compact blob 不误判）
  wait_until 返回谓词真值（v0.2.6）：三态判定（text/collapsed）不得被布尔化
  ——真实试用抓出循环内 `return True` 使 `state == "collapsed"` 永不匹配、
  折叠展开主路在生产成死代码（单测 mock 恰返回 pred() 值，语义失真漏检）
  判据未中且文本仍在框（Enter 被吞/排队）→ 只补 Enter（幂等，不重发文本）
  每轮注入前复查上轮是否已延迟成功（busy 期 Enter 排队、忙完才提交渲染）
  落地失败（忙期不收输入/文本被吞）→ dump 屏幕底部留痕 + 轮前退避 0/3/8s，
  共 3 轮（跨过压缩后 TUI 摘要渲染忙期——pane %2 FAILED 事故根因）
  三轮仍失败 → state=FAILED 落盘（不假 DONE），watcher.log 全程留痕
```

单发 `/compact` 只会让新上下文停在待机态；唤醒词是恢复任务的唯一保证，因此工具会**硬校验**唤醒词必须包含重建线索（文件路径、TaskList 等），不合格直接拒绝执行。

## 组成

```
compactme/
├── .claude-plugin/plugin.json   # 插件清单
├── .mcp.json                    # MCP server 注册
├── mcp/compactme_mcp.py         # compactme() + context_stats()（状态栏水位解析）
├── mcp/watcher.py               # 事件驱动注入状态机 + deliver_wakeup 投递协议
├── skills/compact-me/SKILL.md   # 可用时机与唤醒词纪律
├── tests/test_deliver_wakeup.py # 投递协议单元测试（21 路径：happy/吞Enter/吞文本/
│                                #   残留回归/渲染滞后/无busy/全失败/清框纯净/
│                                #   折叠展开/折叠提交兜底/延迟复查/入口blob不误判/
│                                #   判据直测×9——nbsp 区分/单行框/窗口错位/
│                                #   折行落窗口/wait_until 语义/折叠三态）
├── tests/fake_claude.py         # 模拟 TUI 的 e2e 靶（raw 输入+折行渲染+❯nbsp/空格
│                                #   双形态+BSpace 删字+底部锚定布局+折叠 paste）
└── tests/e2e.sh                 # e2e 五模式：normal/swallow/eat_enter/lost_text/
                                 #   paste_collapse
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
