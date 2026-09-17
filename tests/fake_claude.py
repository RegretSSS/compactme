#!/usr/bin/env python3
"""fake_claude — 模拟 Claude Code 的 compact 生命周期，供 watcher 端到端测试。

v4（配合 watcher v0.3 hooks+transcript 信号源）：watcher 不再看屏幕——
fake 的新增保真点是 Claude Code 的**持久层信号**，屏幕渲染保留仅为 dump
留痕与人工诊断：
  - transcript 写盘（env TRANSCRIPT）：提交即写 user 条目；/compact 被接受
    时按真实 transcript 形态（2026-09-17 backend 事故段实证）依次写：
    命令条目（/compact 前缀）→ isCompactSummary 摘要条目 → <command-name>
    条目——watcher 的唤醒词判据必须忽略前两者（compact_cmd 内嵌唤醒词
    全文、摘要可能复述唤醒词）
  - hook 标记（env SESSION_ID + HOOK_DIR）：/compact 被接受时写 .pre.json，
    压缩完成写 .post.json（真实环境由插件 hooks/hooks.json 的
    PreCompact/PostCompact hook 写，形态一致）

模式（环境变量 MODE）：
  normal    （默认）/compact → pre → Compacting… 2s → 摘要/命令条目 → post → Compacted
  swallow   第 1 个 /compact 被当普通消息（不写 hook/不压缩，3s 后应答），
            第 2 个起正常压缩——测 watcher 无信号重试路径
  eat_enter 第 1 个 Enter 被吞（文本滞留输入框，transcript 无条目）——测重注
  lost_text 第 1 段注入文本被重绘吞掉——测重注
  paste_collapse 大块文本段（>100 字符）折叠为 [Pasted text #N] 占位符
            （明文不上屏）；Enter 对折叠 blob 直接提交——**transcript 仍记
            完整原文**（2026-09-13 pane %2 生产事故形态 + 09-17 transcript
            实证：这正是 v0.3 免疫屏幕雷区的根基）
  busy_defer 非 compact 提交的 transcript 条目延迟 5s 写（busy 期 Enter
            排队、模型忙完才提交写入——2026-09-17 backend 事故时序）——
            测 watcher 长验证窗轮询命中

保真要点：
- 输入侧 raw（ICANON/ICRNL 关）——tmux send-keys 的 \r 原样交付，
  「文本在框、Enter 提交」分离语义（e2e 实测教训）；输出侧保留 OPOST
- alternate screen + 整帧重绘；消息/输入框按终端宽度折行（东亚宽 2 简化）
- C-u 无操作、BSpace 逐字符删、空框 Enter 无操作（真 TUI 实测行为）
所有提交的行记录到 /tmp/compactme-e2e/received.log 供断言。
"""
import json
import os
import shutil
import termios
import time
from pathlib import Path

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
SESSION_ID = os.environ.get("SESSION_ID", "e2e-fake-session")
TRANSCRIPT = Path(os.environ.get("TRANSCRIPT", "/tmp/compactme-e2e/transcript.jsonl"))
HOOK_DIR = Path(os.environ.get("HOOK_DIR", "/tmp/compactme/hooks"))

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
        # 折叠 paste 预览（仿 2026-09-13 pane %2 dump）：blob 预览行 + 提示
        lines.append("─" * 30)
        lines.append(f"❯ [Pasted text #{blob_seq}]")
        lines.append("─" * 30)
        lines.append("  paste again to expand")
        lines.append("")
    # 输入框行 = ❯ + nbsp；已提交消息才是 ❯ + 普通空格（v0.2.x 屏幕判据遗产，
    # v0.3 watcher 已不依赖——保留渲染形态供 dump 留痕比对）
    lines += fold("❯\xa0" + input_buf, WIDTH)
    lines.append("─" * 20 + " status " + "─" * 20)
    lines = [""] * max(0, HEIGHT - len(lines)) + lines[-HEIGHT:]
    print("\033[2J\033[H" + "\n".join(lines), end="", flush=True)


def write_hook(event: str):
    """写 hook 标记——真实环境由插件 hooks/compact_hook.py 写，形态一致。"""
    suffix = {"PreCompact": "pre", "PostCompact": "post"}[event]
    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    (HOOK_DIR / f"{SESSION_ID}.{suffix}.json").write_text(json.dumps({
        "event": event, "ts": time.time(),
        "transcript_path": str(TRANSCRIPT), "trigger": "manual",
    }, ensure_ascii=False))


def append_transcript(content: str, compact_summary: bool = False):
    """追加 transcript 条目（真实 jsonl 形态的简化子集：watcher 判据相关字段）。"""
    TRANSCRIPT.parent.mkdir(parents=True, exist_ok=True)
    with TRANSCRIPT.open("a") as f:
        f.write(json.dumps({
            "type": "user", "isCompactSummary": compact_summary,
            "message": {"role": "user", "content": content},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z",
            "session_id": SESSION_ID,
        }, ensure_ascii=False) + "\n")


def handle_submit(line: str, paste_seq: int | None = None):
    """paste_seq 非 None = 折叠 blob 提交：屏幕渲染折叠形态行，但 transcript
    记完整原文（真实行为——v0.3 判据免疫折叠的根基）。"""
    global busy, swallowed
    LOG.write(f"RECV|{time.strftime('%H:%M:%S')}|{line}\n")
    screen.append(f"❯ [Pasted text #{paste_seq}]" if paste_seq else f"❯ {line}")
    render()
    if line.startswith("/compact") and not (MODE == "swallow" and swallowed == 0):
        # transcript 先记命令条目（含唤醒词全文——watcher 判据须 /compact 前缀排除）
        append_transcript(line)
        write_hook("PreCompact")
        screen.append("  ✶ Compacting conversation…")
        render()
        time.sleep(2)
        # 摘要条目可能复述唤醒词（isCompactSummary 排除依据）+ 命令回显条目
        append_transcript(
            "This session is being continued from a previous conversation… 摘要"
            f"（含唤醒词预告）：{line[:120]}", compact_summary=True)
        append_transcript("<command-name>/compact</command-name>  <local-command-stdout>Compacted</local-command-stdout>")
        write_hook("PostCompact")
        screen.append("  ⎿ Compacted (ctrl+o to see full summary)")
        render()
    else:
        if line.startswith("/compact"):
            swallowed += 1
        busy = True
        render()
        if MODE == "busy_defer":
            time.sleep(5)  # busy 排队：模型忙完才提交写入 transcript
        append_transcript(line)
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
                collapsed = True
                blob_seq += 1
                pending = part
            else:
                input_buf += part
        if idx < len(parts) - 1:  # 本段后有 \r
            enter_count += 1
            if MODE == "eat_enter" and enter_count == 1:
                pass  # Enter 被吞：文本滞留输入框（transcript 无条目）
            elif collapsed:
                # 折叠 blob 直接提交（2026-09-13 pane %2 用户手动 Enter 验证）
                collapsed = False
                line, pending = pending, ""
                handle_submit(line, paste_seq=blob_seq)
            elif input_buf.strip():
                line, input_buf = input_buf, ""
                handle_submit(line)
    render()
