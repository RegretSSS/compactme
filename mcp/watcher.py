#!/usr/bin/env python3
"""compactme watcher — 事件驱动的注入状态机（每 pane 一个实例，互不干扰）。

由 compactme_mcp.py fork，读 /tmp/compactme/<pane>/task.json，只盯自己的 pane。
所有屏幕检测用 tmux capture-pane（只读内存快照，无换页、无锁、并发安全）。

状态机（v0.2.2，唤醒词投递可靠性改造）：
  WAIT_SETTLE  等 TUI 回到输入态（模型写完收尾总结）
  INJECTING    C-u 清行 → /compact 文本 → 等文本落地屏幕输入区（落地确认替代
               固定延迟）→ 单独 Enter
  AWAIT_START  只认 "Compacting conversation"/▰ 为压缩已开始。
               防陈旧误判：屏幕残留上次 compact 的 "⎿ Compacted" 不算数——
               LLM 摘要必然持续数秒，Compacting 特征串必现，未见到它之前
               Compacted 一律不接受。
               若先看到 "esc to interrupt" → /compact 被当普通消息吞掉：
               轮询等 busy 标记消失（真 idle）再重试，不盲等固定秒数。
  COMPACTING   已确认压缩进行中。唯一出路：看到 Compacted → 投递唤醒词；
               或全局超时 → 兜底投递唤醒词。绝不重注 /compact（防双杀）。

deliver_wakeup 可靠投递协议（v0.2.4，三轮迭代的教训）：
  成功信号 = ❯ 用户消息判据（2026-09-12 用户观察「用户发言灰底渲染」的程序化）：
  消息流区**最后一条 ❯ 开头的行**包含唤醒词开头片段、且不是 /compact 命令。
  compact_cmd 残留也是 ❯ 用户消息且内嵌唤醒词全文（v0.2.2 两次 already-sent
  误判漏发的屏幕形态），靠「/compact 前缀排除」精确封堵；输入框折行时首行
  （含 ❯ 与 head）可能落进消息流区，靠「tail 仍在输入区 ⇔ 尚未提交」核心
  不变量守卫。v0.2.3 的 busy 信号退役：压缩后模型自动继续也会 busy，误判
  成功即假 DONE 漏发；❯ 消息是提交本身的直接证据，且先于 busy 渲染。
  流程：
  1. BSpace 轰炸清框 → 注入文本 → 轮询等尾部片段出现在输入区（落地确认，
     ≤3s）。C-u 在 Claude Code 无清行语义（实测注入 727 字符后 C-u 原样
     不动），BSpace 逐字符删、空框无操作，幂等。
  2. 发 Enter → 轮询 ❯ 判据（≤3s）
  3. 判据未命中且尾部仍在输入区（Enter 被吞）→ 只补 Enter（≤3 次）
  4. 落地失败（文本被吞，或压缩后 TUI 忙期不收输入——2026-09-12 pane %2
     FAILED 事故：Compacted+1s 注入、两轮间隔仅 0.5s 全撞在摘要渲染忙期，
     实证 idle 后同长度注入 0.15s 即落地）→ dump 屏幕底部进日志 + 轮前
     退避 (0, 3, 8)s 跨过忙期，共 3 轮
  5. 三轮仍失败 → state=FAILED 落盘（不假 DONE），日志全程留痕
  注：不做「进入时已发送」幂等跳过——重入防重复由 kill_previous 保证；
  幂等检查在 compact 残留屏幕上必然误判（v0.2.2 教训），误判漏发比罕见
  重发（多发一条唤醒词，无害）代价高得多。
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# 屏幕特征串（子串匹配，容错空格/修饰）
MARK_COMPACTING = "Compacting conversation"
MARK_COMPACTED = "Compacted (ctrl+o"
MARK_BUSY = "esc to interrupt"
# ❯ 的两种形态（2026-09-12 pane %2/%3 实测字节级验证）：
# 输入框行 = ❯ + nbsp(\xa0)，空框/有文本/折行首行同形态；
# 已提交用户消息 = ❯ + 普通空格。这是「文本在框」vs「已发出」的可靠区分，
# 不依赖行区域划分——消息流内容少时（压缩刚完成）新消息行会落进底部
# N 行窗口，按区域判分必错。
MARK_USER_MSG = "❯ "     # 已提交用户消息行前缀
MARK_INPUT = "❯\xa0"     # 输入框行前缀
MARK_COMPACT_CMD = "/compact"  # compact 命令前缀（残留排除用）

POLL_INTERVAL = 0.4
START_TIMEOUT = 20  # 等 Compacting 出现的窗口（秒）
IDLE_POLL = 1.0

# 屏幕底部 N 行视为输入区（Claude Code：输入框末行 + 边框 + 状态行）。
# 长文本折行时输入框占多行，但 tail 恒在末行——仍在窗口内（实测验证）。
INPUT_AREA_LINES = 4

DELIVER_TEXT_TIMEOUT = 3.0    # 等注入文本落地输入区
DELIVER_SENT_TIMEOUT = 3.0    # 等发送成功信号（❯ 消息判据）
DELIVER_RECHECK = 1.5         # 补 Enter 失败后的延迟复检（渲染滞后容忍）
DELIVER_BACKOFF = (0.0, 3.0, 8.0)  # 各轮注入前退避：跨过压缩后 TUI 忙期
DELIVER_ROUNDS = len(DELIVER_BACKOFF)
ENTER_NUDGES = 3              # 补 Enter 次数上限
CLEAR_BS = 1200               # 清框 BSpace 数：唤醒词实测 1036 字符，宁多勿少


def tmux(*args: str) -> str:
    """执行 tmux 子命令，失败返回空串（pane 可能已死，别崩）。"""
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return ""


def pane_alive(pane: str) -> bool:
    return pane in tmux("list-panes", "-a", "-F", "#{pane_id}").split()


def capture(pane: str) -> str:
    # 只抓当前屏幕（-S 0），不含 scrollback：
    # TUI 整屏重绘时旧帧可能残留在 scrollback（如 \033[2J 清屏会把旧行
    # 拷入 history），特征串匹配必须只看当前帧，否则陈旧行会污染状态判断。
    return tmux("capture-pane", "-p", "-t", pane, "-S", "0")


def send_text(pane: str, text: str) -> None:
    tmux("send-keys", "-t", pane, "-l", text)


def send_key(pane: str, key: str) -> None:
    tmux("send-keys", "-t", pane, key)


def clear_input(pane: str) -> None:
    """清空输入框：BSpace 逐字符删，空框无操作（幂等）。

    C-u 在 Claude Code 无清行语义（2026-09-12 实测：注入 727 字符后发 C-u
    原样不动，最终 BSpace×800 删净）——旧版用 C-u 清框碰巧一直框是空的才
    没暴露。残留不清会把唤醒词拼接污染成脏消息发出。"""
    tmux("send-keys", "-t", pane, *(["BSpace"] * CLEAR_BS))
    time.sleep(0.3)


def split_screen(frame: str) -> tuple[str, str]:
    """返回 (消息流区, 输入区)。行数不足时输入区=全屏，消息流区=空。

    只去掉尾换行的 split artifact，保留真实空行——rstrip 会把屏幕底部的
    真实空行吃掉，输入区窗口上移一行，把消息流最后一行（往往恰是刚渲染
    的 ❯ 新消息）误划进输入区，污染 sent_signal / in_input 判定。"""
    lines = frame.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) <= INPUT_AREA_LINES:
        return "", "\n".join(lines)
    return "\n".join(lines[:-INPUT_AREA_LINES]), "\n".join(lines[-INPUT_AREA_LINES:])


def wait_until(pred, timeout: float, interval: float = 0.2) -> bool:
    """轮询等谓词为真；超时后最后再验一次（边界帧竞态容忍）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(interval)
    return pred()


class Task:
    def __init__(self, cfg_path: Path):
        self.cfg_path = cfg_path
        self.cfg = json.loads(cfg_path.read_text())

    def update(self, **fields) -> None:
        self.cfg.update(fields)
        tmp = self.cfg_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2))
        tmp.replace(self.cfg_path)

    def log(self, msg: str) -> None:
        with (self.cfg_path.parent / "watcher.log").open("a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def kill_previous(task: Task) -> None:
    """同一 pane 重入：先干掉上一个 watcher（如果还活着）。"""
    old_pid = task.cfg.get("watcher_pid")
    if old_pid and old_pid != os.getpid():
        try:
            os.kill(int(old_pid), signal.SIGTERM)
            task.log(f"killed previous watcher pid={old_pid}")
        except (ProcessLookupError, ValueError):
            pass


def wakeup_markers(word: str) -> tuple[str, str]:
    """唤醒词的可见特征片段：(head, tail)。

    head：判「已作为用户消息提交」用——❯ 消息行的开头片段。必须配合
    「非 /compact 前缀」使用：compact_cmd 内嵌唤醒词全文，其残留 ❯ 行
    会被裸 head 检测命中（v0.2.2 事故形态）。
    tail：判「仍在输入框」用——提交前文本折行渲染，tail 恒在输入框末行；
    提交后框清空。tail 在框 ⇔ 尚未提交，是成功判据的前置守卫。
    """
    return word[:12], word.replace("\n", "")[-12:]


def last_user_line(frame: str) -> str | None:
    """全屏最后一条已提交用户消息行（❯ + 普通空格前缀；输入框是 ❯+nbsp，
    天然不进此判据）。全屏搜而非只搜 chat 区：消息流内容少时（压缩刚
    完成）新消息落进底部 N 行窗口，按区域搜会漏 → 判据永假 → 双发。"""
    last = None
    for line in frame.split("\n"):
        if line.lstrip().startswith(MARK_USER_MSG):
            last = line
    return last


def sent_signal(frame: str, head: str, tail: str) -> bool:
    """唤醒词已成功提交的屏幕信号：❯ 用户消息判据（v0.2.4）。

    最后一条已提交用户消息（❯ +普通空格前缀）包含 head、且不是 /compact
    命令（compact_cmd 残留同為用户消息且内嵌唤醒词全文，靠前缀排除），
    且 tail 不在输入框（文本在框 ⇔ 尚未提交）。唤醒词提交后模型开始处理
    会渲染 busy——但压缩后模型自动继续也会 busy，故 busy 不能作成功信号
    （假 DONE 漏发），仅用于 swallow 等 idle 逻辑。"""
    if in_input(frame, tail):
        return False
    line = last_user_line(frame)
    if line is None:
        return False
    body = line.lstrip()[len(MARK_USER_MSG):]
    return head in line and not body.lstrip().startswith(MARK_COMPACT_CMD)


def in_input(frame: str, tail: str) -> bool:
    """tail 是否仍在输入框（⇔ 尚未提交）。

    tail 所在行是输入框行（❯+nbsp 前缀，或折行后无前缀的续行）才算在框；
    ❯+普通空格的行是已提交的用户消息——消息流内容少时（压缩刚完成只有
    摘要几行）新消息会落进底部 N 行窗口，按区域+裸子串判定会把它误当
    「文本在框」，sent 判据被永假拦截 → FAILED + 三轮重注双发。"""
    for line in split_screen(frame)[1].split("\n"):
        if tail in line and not line.lstrip().startswith(MARK_USER_MSG):
            return True
    return False


def dump_bottom(task: Task, pane: str) -> None:
    """落地失败时把屏幕底部留痕进日志——2026-09-12 pane %2 FAILED 事故没有
    现场形态，只能推断；此后失败可精确归因。"""
    task.log("landed=False, bottom 10 lines dump:")
    for line in capture(pane).rstrip("\n").split("\n")[-10:]:
        task.log(f"  | {line[:120]}")


def deliver_wakeup(task: Task, reason: str) -> bool:
    """投递唤醒词：注入 → 落地确认 → Enter → 验证 → 分级补救。见模块 docstring。"""
    pane = task.cfg["pane"]
    word = task.cfg["wakeup_word"]
    head, tail = wakeup_markers(word)

    for rnd in range(1, DELIVER_ROUNDS + 1):
        time.sleep(DELIVER_BACKOFF[rnd - 1])  # 轮前退避：跨过压缩后 TUI 忙期
        task.update(state="DELIVERING", wakeup_round=rnd)
        clear_input(pane)
        send_text(pane, word)
        landed = wait_until(lambda: in_input(capture(pane), tail), DELIVER_TEXT_TIMEOUT, 0.15)
        task.log(f"wakeup round {rnd}: text landed={landed}")
        if not landed:
            dump_bottom(task, pane)  # 文本被吞或忙期不收输入 → 留痕后退避重试
            continue

        send_key(pane, "Enter")
        ok = wait_until(lambda: sent_signal(capture(pane), head, tail), DELIVER_SENT_TIMEOUT, 0.2)
        task.log(f"wakeup round {rnd}: after enter, sent={ok}")
        if ok:
            task.update(state="DONE", end_reason=reason)
            task.log(f"wakeup delivered ({reason})")
            return True

        # 分级补救：文本仍在输入区（Enter 被吞）→ 只补 Enter（幂等，不重发文本）
        for nudge in range(1, ENTER_NUDGES + 1):
            frame = capture(pane)
            if sent_signal(frame, head, tail):
                task.update(state="DONE", end_reason=reason)
                task.log(f"wakeup delivered after nudge {nudge} ({reason})")
                return True
            if in_input(frame, tail):
                task.log(f"wakeup enter nudge {nudge} (text still in input)")
                send_key(pane, "Enter")
                time.sleep(0.8)
            else:
                break  # 文本不在框：已提交待渲染或被吞，走延迟复检

        # 渲染滞后容忍：❯ 消息行可能晚于 Enter 数百毫秒出现
        time.sleep(DELIVER_RECHECK)
        if sent_signal(capture(pane), head, tail):
            task.update(state="DONE", end_reason=f"{reason} (late render confirm)")
            task.log(f"wakeup delivered by late-render confirm ({reason})")
            return True

    task.update(state="FAILED", end_reason=f"wakeup unconfirmed ({reason})")
    task.log(f"wakeup FAILED after {DELIVER_ROUNDS} rounds ({reason})")
    return False


def wait_for_idle(task: Task, pane: str, t0: float, global_deadline: float) -> bool:
    """轮询等 busy 标记消失（真 idle）。返回 False 表示全局超时。"""
    while time.time() - t0 < global_deadline:
        if MARK_BUSY not in capture(pane):
            return True
        time.sleep(IDLE_POLL)
    return False


def inject_compact_cmd(task: Task, pane: str) -> None:
    """注入 /compact 命令：清框 → 文本落地确认后再发 Enter（与唤醒词同一防抖手法）。"""
    cmd = task.cfg["compact_cmd"]
    tail = cmd.replace("\n", "")[-12:]
    clear_input(pane)
    send_text(pane, cmd)
    # 落地确认替代固定 1.5s：命令是数百字长串（含唤醒词全文），瞬时注入后
    # 必须等 TUI 真正把它渲染进输入框，否则 Enter 可能发在文本落地前。
    wait_until(lambda: tail in split_screen(capture(pane))[1], DELIVER_TEXT_TIMEOUT, 0.15)
    time.sleep(0.2)
    send_key(pane, "Enter")


def run(task: Task) -> None:
    pane = task.cfg["pane"]
    settle = float(task.cfg.get("settle_seconds", 6))
    global_deadline = float(task.cfg.get("global_timeout", 300))
    retries = int(task.cfg.get("retry_limit", 2))

    kill_previous(task)
    task.update(watcher_pid=os.getpid(), state="WAIT_SETTLE")
    task.log(f"watcher start pid={os.getpid()} pane={pane}")

    t0 = time.time()
    time.sleep(settle)  # 等模型收尾总结、TUI 回到输入态

    for attempt in range(1, retries + 2):
        if not pane_alive(pane):
            task.update(state="ABORTED", end_reason="pane gone")
            return
        if time.time() - t0 > global_deadline:
            break

        task.update(state="INJECTING", attempt=attempt)
        inject_compact_cmd(task, pane)
        task.log(f"compact injected (attempt {attempt})")

        # 阶段一：等压缩真正开始（只认 Compacting/▰；陈旧 Compacted 不算）
        started = False
        swallowed = False
        start_by = time.time() + START_TIMEOUT
        while time.time() < start_by:
            if time.time() - t0 > global_deadline:
                break
            frame = capture(pane)
            if MARK_COMPACTING in frame or "▰" in frame:
                started = True
                break
            if MARK_BUSY in frame:
                swallowed = True
                break
            time.sleep(POLL_INTERVAL)

        if started:
            # 阶段二：压缩进行中——只等完成或全局超时，绝不重注
            task.update(state="COMPACTING")
            task.log("compaction confirmed running")
            while time.time() - t0 < global_deadline:
                if MARK_COMPACTED in capture(pane):
                    time.sleep(1.0)  # 等 TUI 完成首帧重绘再投递
                    deliver_wakeup(task, "compacted confirmed")
                    return
                time.sleep(POLL_INTERVAL)
            break  # 全局超时 → 兜底

        if swallowed:
            # /compact 被当成普通消息：等模型真正 idle 再重试
            task.log("compact swallowed as message, waiting for idle")
            if not wait_for_idle(task, pane, t0, global_deadline):
                break  # 全局超时 → 兜底
            continue

        # 既没开始也没被吞（屏幕无特征）：小睡后重试
        task.log(f"attempt {attempt}: no markers on screen, retrying")
        time.sleep(2)

    deliver_wakeup(task, "retries/global-timeout fallback")


if __name__ == "__main__":
    run(Task(Path(sys.argv[1])))
