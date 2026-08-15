# compactme 自触发实验 — 任务状态（post-compact 重建入口）

> 本文件是压缩后的任务重建线索。唤醒词会指向这里 + TaskList。

## 实验目的

用户要求：全量 review compactme v2、为当前会话安装、然后由我（模型）自己触发
自压缩技能，验证「/compact + 唤醒词」双消息注入在真实 Claude Code 会话中闭环。

## 已完成（压缩前）

1. **全量 review**（Task #1 完成）：发现并修复 4 个时序缺陷
   - busy-loop 烧 CPU（Compacting 分支 continue 跳过 sleep）
   - 陈旧 Compacted 误判（改为必须先见 Compacting 才接受 Compacted）
   - 慢压缩双杀（COMPACTING 态绝不重注 /compact）
   - 盲等 5s 重试（改为轮询等 busy 标记真消失）
   - 修复 scrollback 污染：capture 改用 `-S 0` 只看当前帧
2. **e2e 验证**（Task #2 完成，fake TUI，alternate screen 保真模拟）：
   - normal：10:08:20 注入 /compact → 10:08:23 唤醒词，DONE/compacted confirmed/attempt 1
   - swallow：10:07:47 首注被吞 → 等 idle → 10:07:52 重注 → 10:07:55 唤醒词，
     DONE/attempt 2（等-idle-重试路径验证通过）
3. **安装**（Task #3 完成）：注册 compactme@local v0.2.1 到
   ~/.claude/plugins/installed_plugins.json，cache 位于
   ~/.claude/plugins/cache/local/compactme/0.2.1（claude CLI 在 WSL 指到
   Windows .exe 无法执行，故手动注册，结构已校验）

## 压缩后待办（按序）

1. 读 /tmp/compactme/8/task.json 与 /tmp/compactme/8/watcher.log（pane=%8），
   核对时序：compact 注入时刻 → Compacting → Compacted → 唤醒词注入时刻
2. 核对本轮屏幕确实出现 ⎿ Compacted（ctrl+o（用户可肉眼证实）
3. 向用户报告实验结果（含上面 e2e 数据表 + 真实时序），然后完成 Task #5
4. 可选：commit compactme 仓库（用户尚未授权 commit，问一句）

## 关键路径

- 插件源码：/home/remedy/workspace/compactme/（未 commit）
- 本任务文件：/home/remedy/workspace/compactme/PLAN.md
- 运行时任务：/tmp/compactme/<pane-id>/task.json + watcher.log
- 我的 pane：%8（engassistant session，ops-agent 窗口）

## 给压缩后的自己

不要凭记忆臆测细节——先读 task.json/watcher.log 的真实时间戳再汇报。
若 task.json state=DONE 且 end_reason=compacted confirmed，实验即成功；
若是 fallback，如实报告走了哪条兜底路径。
