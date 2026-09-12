#!/usr/bin/env python3
"""fake_claude — 模拟 Claude Code TUI 的 compact 行为，供 watcher 端到端测试。

v3（配合 watcher v0.2.4 ❯ 判据）：按 2026-09-12 pane %2 实测的真实 TUI 形态修正——
  - 输入框**折行**渲染（v2 的「横向左滚只露尾部」模型是错的：实测 727 字符
    在 179 列 pane 折成 5+ 行占满屏幕；tail 恒在末行，watcher 的底部 4 行
    检测依然有效）
  - 用户消息渲染为 ❯ 前缀行（真 TUI 用户消息带灰底，程序判据只看 ❯ 前缀）
  - C-u **无操作**（实测不清框）；0x7f BSpace 逐字符删——watcher 清框依赖此
  - 空框 Enter 无操作（真 TUI 行为；watcher 补 Enter 的安全性依赖于此）

模式（环境变量 MODE）：
  normal    （默认）/compact → Compacting… → 2s → Compacted
  swallow   第 1 个 /compact 被当普通消息（渲染 esc to interrupt 3s 后应答），
            第 2 个起正常压缩——测 watcher 的等-idle-重试路径
  eat_enter 第 1 个 Enter 被吞（文本滞留输入框）——测补 Enter 幂等路径
  lost_text 第 1 段注入文本被重绘吞掉（不回显不进框）——测退避重注路径
  paste_collapse 大块文本段（>100 字符）折叠为 [Pasted text #N] 占位符 +
            paste again to expand 提示（明文不上屏——2026-09-13 pane %2 生产
            事故形态）；再次大块到达 → 展开为明文（两次拼接）；Enter 对折叠
            blob 直接提交（提交内容记录 RECV，消息流渲染折叠形态行）——
            测 watcher 的折叠识别/二次注入展开/兜底提交三路径

保真要点：
- alternate screen + ANSI 清屏整帧重绘（旧行/busy 标记在下一次渲染消失）
- 消息流行与输入框行均按终端宽度折行（东亚字符宽 2 的简化 wcwidth）
所有提交的行记录到 /tmp/compactme-e2e/received.log 供断言。
"""
import os
import shutil
import termios
import time

# 真 TUI 的输入侧是 raw 的：ICANON/ICRNL 关闭，tmux send-keys 的 \r 原样交付、
# 文本段不等待行终结即可读——否则 Enter 被 pty 行缓冲吃掉，无法模拟
# 「文本在框、Enter 提交」的分离语义（e2e 实测教训）。
# 输出侧保留 OPOST（\n 自动映射 \r\n）——tty.setraw 会连 OPOST 一起关，
# 渲染会阶梯错位；只动输入标志。
try:
    _attrs = termios.tcgetattr(0)
    _attrs[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)  # lflag
    _attrs[0] &= ~(termios.ICRNL | termios.IXON)                   # iflag
    termios.tcsetattr(0, termios.TCSANOW, _attrs)
except termios.error:
    pass  # 非 tty（管道驱动）时退化为行缓冲模型

MODE = os.environ.get("MODE", "normal")

LOG = open("/tmp/compactme-e2e/received.log", "a", buffering=1)
WIDTH, HEIGHT = shutil.get_terminal_size().columns, shutil.get_terminal_size().lines
screen = []        # 消息流行
input_buf = ""     # 输入框全文（提交语义用）
busy = False
enter_count = 0
text_seg_count = 0
swallowed = 0
# 折叠 paste 态（paste_collapse 模式）
collapsed = False
pending = ""       # 折叠 blob 持有的原文（提交语义用）
blob_seq = 0       # paste 编号（会话级递增）

# 进 alternate screen buffer（真 TUI 的做法）：清屏不产生 scrollback，
# capture-pane 只能看到当前帧，与 Claude Code 的真实渲染语义一致
print("\033[?1049h", end="", flush=True)


def disp_width(s: str) -> int:
    return sum(2 if ord(c) > 0x2E7F else 1 for c in s)  # 东亚宽 2 的简化


def fold(s: str, width: int) -> list[str]:
    """按显示宽度折行（真 TUI 行为：长文本折成多行而非横向滚动）。"""
    lines, cur, w = [], "", 0
    for ch in s:
        cw = 2 if ord(ch) > 0x2E7F else 1
        if w + cw > width:
            lines.append(cur)
            cur, w = "", 0
        cur += ch
        w += cw
    lines.append(cur)
    return lines


def render():
    lines = []
    for row in screen:  # 消息流（含用户消息 ❯ 前缀）同样折行
        lines += fold(row, WIDTH)
    if busy:
        lines.append("  (esc to interrupt while thinking…)")
    if collapsed:
        # 折叠 paste 预览（仿 2026-09-13 pane %2 dump）：blob 预览行（上下
        # 分隔线）+ 状态行 + 展开提示 + 空行 + 输入框——明文不上屏
        lines.append("─" * 30)
        lines.append(f"❯ [Pasted text #{blob_seq}]")
        lines.append("─" * 30)
        lines.append("  fake@localhost · status")
        lines.append("  paste again to expand")
        lines.append("")
    # 输入框行 = ❯ + nbsp（真 TUI 字节级形态，空框/有文本/折行首行同形态；
    # 已提交消息才是 ❯ + 普通空格——watcher 靠这两态区分「在框」vs「已发出」）
    lines += fold("❯\xa0" + input_buf, WIDTH)
    lines.append("─" * 20 + " status " + "─" * 20)
    # 真 TUI 布局锚定屏幕底部：内容沉底、输入框与状态行钉在 pane 末行——
    # watcher 的「底部 N 行=输入区」依赖此（顶部锚定会让输入区取到空行）
    lines = [""] * max(0, HEIGHT - len(lines)) + lines[-HEIGHT:]
    print("\033[2J\033[H" + "\n".join(lines), end="", flush=True)


def handle_submit(line: str, paste_seq: int | None = None):
    """paste_seq 非 None = 折叠 blob 提交：内容仍是完整原文（RECV 记录），
    消息流渲染为折叠形态行（真 TUI 行为）。"""
    global busy, swallowed
    LOG.write(f"RECV|{time.strftime('%H:%M:%S')}|{line}\n")
    # 真 TUI 把用户命令渲染为 ❯ 前缀消息行（含 compact_cmd 内嵌的唤醒词全文，
    # 压缩完成后仍留在屏幕上——v0.2.2 生产事故的屏幕形态，e2e 必须复现）
    screen.append(f"❯ [Pasted text #{paste_seq}]" if paste_seq else f"❯ {line}")
    render()
    if line.startswith("/compact") and not (MODE == "swallow" and swallowed == 0):
        screen.append("  ✶ Compacting conversation…")
        render()
        time.sleep(2)
        screen.append("  ⎿ Compacted (ctrl+o to see full summary)")
        render()
    else:
        if line.startswith("/compact"):
            swallowed += 1
        busy = True
        render()
        time.sleep(3)
        busy = False
        screen.append(f"  (assistant reply to: {line[:50]})")
        render()


screen.append("fake-claude ready (idle)")
render()

while True:
    try:
        data = os.read(0, 65536)
    except OSError:
        break
    if not data:
        break
    seg = data.decode("utf-8", "ignore")
    # 以 \r（tmux send-keys Enter）为提交界逐段处理
    parts = seg.split("\r")
    for idx, part in enumerate(parts):
        if "\x15" in part:
            pass  # C-u：真 TUI 无清行语义（实测），fake 同样无操作
        if "\x7f" in part:  # BSpace：逐字符删（watcher 清框依赖，一字一删）
            n_bs = part.count("\x7f")
            part = part.replace("\x7f", "")
            input_buf = input_buf[: max(0, len(input_buf) - n_bs)]
        if part:
            text_seg_count += 1
            if MODE == "lost_text" and text_seg_count == 1:
                pass  # 文本段被 TUI 重绘吞掉：不回显不进框
            elif MODE == "paste_collapse" and len(part) > 100:
                # 大块瞬时文本 → 按 paste 处理折叠为 blob（明文不上屏）
                if collapsed:
                    # 再粘贴一次 → 展开（paste again to expand）：两份拼接
                    input_buf = pending + part
                    pending = ""
                    collapsed = False
                else:
                    collapsed = True
                    blob_seq += 1
                    pending = part
            else:
                input_buf += part
        if idx < len(parts) - 1:  # 本段后有 \r
            enter_count += 1
            if MODE == "eat_enter" and enter_count == 1:
                pass  # Enter 被吞：文本滞留输入框
            elif collapsed:
                # 折叠 blob 直接提交（2026-09-13 pane %2 用户手动 Enter 验证）
                collapsed = False
                line, pending = pending, ""
                handle_submit(line, paste_seq=blob_seq)
            elif input_buf.strip():
                line, input_buf = input_buf, ""
                handle_submit(line)
    render()
