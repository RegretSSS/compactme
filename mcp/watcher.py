#!/usr/bin/env python3
"""compactme watcher — 事件驱动的注入状态机（每 pane 一个实例，互不干扰）。

由 compactme_mcp.py fork，读 /tmp/compactme/<pane>/task.json，只盯自己的 pane。
所有屏幕检测用 tmux capture-pane（只读内存快照，无换页、无锁、并发安全）。

状态机（v2.1，修复四类时序缺陷）：
  WAIT_SETTLE  等 TUI 回到输入态（模型写完收尾总结）
  INJECTING    C-u 清行 → /compact 文本 → 短延迟 → 单独 Enter（分键防抖）
  AWAIT_START  只认 "Compacting conversation"/▰ 为压缩已开始。
               防陈旧误判：屏幕残留上次 compact 的 "⎿ Compacted" 不算数——
               LLM 摘要必然持续数秒，Compacting 特征串必现，未见到它之前
               Compacted 一律不接受。
               若先看到 "esc to interrupt" → /compact 被当普通消息吞掉：
               轮询等 busy 标记消失（真 idle）再重试，不盲等固定秒数。
  COMPACTING   已确认压缩进行中。唯一出路：看到 Compacted → 注入唤醒词；
               或全局超时 → 兜底注入唤醒词。绝不重注 /compact（防双杀）。
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

POLL_INTERVAL = 0.4
START_TIMEOUT = 20  # 等 Compacting 出现的窗口（秒）
IDLE_POLL = 1.0


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


def inject_wakeup(task: Task, reason: str) -> None:
    pane = task.cfg["pane"]
    send_key(pane, "C-u")
    time.sleep(0.2)
    send_text(pane, task.cfg["wakeup_word"])
    time.sleep(0.3)
    send_key(pane, "Enter")
    task.update(state="DONE", end_reason=reason)
    task.log(f"wakeup injected ({reason})")


def wait_for_idle(task: Task, pane: str, t0: float, global_deadline: float) -> bool:
    """轮询等 busy 标记消失（真 idle）。返回 False 表示全局超时。"""
    while time.time() - t0 < global_deadline:
        if MARK_BUSY not in capture(pane):
            return True
        time.sleep(IDLE_POLL)
    return False


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

        # 分键注入：文本先进输入框，Enter 独立后发（#28559 的防抖手法）
        task.update(state="INJECTING", attempt=attempt)
        send_key(pane, "C-u")
        time.sleep(0.2)
        send_text(pane, task.cfg["compact_cmd"])
        time.sleep(1.5)
        send_key(pane, "Enter")
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
                    time.sleep(0.6)  # 等 TUI 完成重绘
                    inject_wakeup(task, "compacted confirmed")
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

    inject_wakeup(task, "retries/global-timeout fallback")


if __name__ == "__main__":
    run(Task(Path(sys.argv[1])))
