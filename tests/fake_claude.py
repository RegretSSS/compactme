#!/usr/bin/env python3
"""fake_claude — 模拟 Claude Code TUI 的 compact 行为，供 watcher 端到端测试。

模式（环境变量 MODE）：
  normal （默认） /compact → Compacting… → 2s → Compacted
  swallow 第 1 个 /compact 被当普通消息（渲染 esc to interrupt 3s 后应答），
          第 2 个起正常压缩——测 watcher 的等-idle-重试路径

用 ANSI 清屏模拟真 TUI 的整屏重绘：旧行（如 busy 标记）在下一次渲染时消失，
与 Claude Code 的真实行为一致（追加式 print 会让 busy 标记永远留在屏幕上，
导致 wait_for_idle 误判）。
所有收到的行记录到 /tmp/compactme-e2e/received.log 供断言。
"""
import os
import sys
import time

MODE = os.environ.get("MODE", "normal")
swallowed = 0
LOG = open("/tmp/compactme-e2e/received.log", "a", buffering=1)
screen = []

# 进 alternate screen buffer（真 TUI 的做法）：清屏不产生 scrollback，
# capture-pane 只能看到当前帧，与 Claude Code 的真实渲染语义一致
print("\033[?1049h", end="", flush=True)


def render():
    print("\033[2J\033[H" + "\n".join(screen), flush=True)


screen.append("fake-claude ready (idle)")
render()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    LOG.write(f"RECV|{time.strftime('%H:%M:%S')}|{line}\n")
    if line.startswith("/compact") and not (MODE == "swallow" and swallowed == 0):
        screen.append("  ✶ Compacting conversation…")
        render()
        time.sleep(2)
        screen.append("  ⎿ Compacted (ctrl+o to see full summary)")
        render()
    else:
        if line.startswith("/compact"):
            swallowed += 1
        screen.append("  (esc to interrupt while thinking…)")
        render()
        time.sleep(3)
        screen[-1] = f"  (assistant reply to: {line[:50]})"
        render()
