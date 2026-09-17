#!/usr/bin/env python3
"""compactme watcher v0.3 — hooks + transcript 双通道信号源。

由 compactme_mcp.py fork，读 /tmp/compactme/<pane>/task.json，只盯自己的 pane。
注入动作走 tmux send-keys；**全部状态确认不再看屏幕**——v0.2.x 四次生产
事故全是屏幕形态问题（折叠→折行→窗口→布局，2026-09-13/09-17，见 VERIFY），
2026-09-17 pane %3（backend）双发 /compact + 唤醒词空转事故后整体换轨：

  compacting 开始 = PreCompact hook 标记（插件 hooks/hooks.json 注册，
                   hooks/compact_hook.py 写 /tmp/compactme/hooks/<sid>.pre.json）
  compacted 完成 = PostCompact hook 标记；兜底 = transcript isCompactSummary
  唤醒词送达    = transcript 新增 user 条目含 head 且非 /compact 前缀

transcript 优势（2026-09-17 事故 transcript 铁证）：写入先于 TUI 渲染 10s+；
折叠 paste 提交后记录完整原文（屏幕只显 [Pasted text #N]）——屏幕雷区全免疫。

状态机：
  WAIT_SETTLE 等 TUI 回到输入态（模型写完收尾总结）
  INJECTING  清框 → /compact 文本 + 0.3s + Enter → 等 PreCompact 标记
             （PRECOMPACT_TIMEOUT=90s 覆盖 busy 期输入排队——/compact 进
             队列没丢，21s 等屏幕特征误判重发是 v0.2.x 双发根源）。
             无标记才重试：swallow（被当普通消息吞）时 PreCompact 不触发，
             自然落入重试，不再需要 busy 屏幕判定。
  COMPACTING 等 PostCompact 标记或 transcript isCompactSummary。绝不重注。
  DELIVERING 清框 → 唤醒词 + 0.3s + Enter 连发 → tail transcript 等新增
             user 条目含 head。注入前行数做基线，历史条目不参与判定
             （compact_cmd 条目内嵌唤醒词全文，靠 /compact 前缀排除；
              isCompactSummary 摘要可能复述唤醒词，显式跳过）。
             轮前复查上轮延迟提交（busy 排队的 Enter 忙完才提交）；
             三轮退避重注，失败 dump 屏幕留痕 + FAILED 落盘（不假 DONE）。

注入+Enter 直发的形态覆盖（不再需要落地确认/折叠分流）：
  idle     文本进框 → Enter 提交
  busy     两者都进输入队列 → 忙完按序处理 → 提交（Enter 排队延迟，
           transcript 条目最终出现，轮前复查/长验证窗兜住）
  折叠     文本折叠为 blob → Enter 直接提交 blob（用户手动验证过的路径，
           transcript 记完整原文）
  文本被吞 Enter 提交空框（无害）→ transcript 无新条目 → 重注
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HOOK_ROOT = Path(os.environ.get("COMPACTME_HOOK_ROOT", "/tmp/compactme/hooks"))
# transcript 根目录可被测试环境覆盖（e2e/单测用隔离目录，不碰真实会话记录）
PROJECTS_ROOT = Path(os.environ.get(
    "COMPACTME_PROJECTS_ROOT", str(Path.home() / ".claude" / "projects")))
MARK_COMPACT_CMD = "/compact"  # compact 命令条目前缀（transcript 残留排除）

POLL_INTERVAL = 0.5
PRECOMPACT_TIMEOUT = 90    # 等 PreCompact 标记：覆盖 busy 期 /compact 输入排队
COMPACT_DONE_TIMEOUT = 240 # 等 PostCompact：大上下文摘要生成可达分钟级
DELIVER_SENT_TIMEOUT = 30  # 等唤醒词 transcript 条目：覆盖 busy 排队提交
DELIVER_BACKOFF = (0.0, 3.0, 8.0)  # 各轮注入前退避
DELIVER_ROUNDS = len(DELIVER_BACKOFF)
CLEAR_BS = 1200            # 清框 BSpace 数：唤醒词实测 1036 字符，宁多勿少
KEY_TEXT_SETTLE = 0.3      # 注入文本与 Enter 之间的间隔（防乱序）


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
    # 只抓当前屏幕（-S 0），失败留痕诊断用（v0.3 不参与任何状态判定）
    return tmux("capture-pane", "-p", "-t", pane, "-S", "0")


def send_text(pane: str, text: str) -> None:
    tmux("send-keys", "-t", pane, "-l", text)


def send_key(pane: str, key: str) -> None:
    tmux("send-keys", "-t", pane, key)


def clear_input(pane: str) -> None:
    """清空输入框：BSpace 逐字符删，空框无操作（幂等）。

    C-u 在 Claude Code 无清行语义（2026-09-12 实测：注入 727 字符后发 C-u
    原样不动，最终 BSpace×800 删净）。残留不清会把唤醒词拼接污染成脏消息发出。"""
    tmux("send-keys", "-t", pane, *(["BSpace"] * CLEAR_BS))
    time.sleep(0.3)


def wait_until(pred, timeout: float, interval: float = 0.2):
    """轮询等谓词为真；超时后最后再验一次（边界竞态容忍）。

    返回使谓词命中的 pred() 真实返回值，非布尔（v0.2.6 教训：循环内
    `return True` 把三态布尔化，折叠展开主路成死代码）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = pred()
        if result:
            return result
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


# ---- hook 标记通道（compacting/compacted 的稳定信号）----

def hook_path(session_id: str, event: str) -> Path:
    """event: 'pre' | 'post'。标记文件名与 hooks/compact_hook.py 约定一致。"""
    return HOOK_ROOT / f"{session_id}.{event}.json"


def clear_stale_hooks(session_id: str, watcher_start: float) -> None:
    """删除本 session 的陈旧 hook 标记（上次压缩遗留）。

    双保险：删除 + 时序校验（ts 早于 watcher 启动的标记视为陈旧）——
    删除失败/竞态时序校验兜底。"""
    for event in ("pre", "post"):
        p = hook_path(session_id, event)
        if p.exists():
            try:
                p.unlink()
                continue
            except OSError:
                pass
        # 删不掉的留给 read_hook 的时序校验拒绝


def read_hook(session_id: str, event: str, watcher_start: float) -> dict | None:
    """读 hook 标记；陈旧标记（ts 早于 watcher 启动）返回 None。"""
    p = hook_path(session_id, event)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if float(data.get("ts", 0)) < watcher_start:
        return None
    return data


# ---- transcript 通道（压缩完成兜底 + 唤醒词送达确认）----

def find_transcript(session_id: str) -> Path | None:
    """按 session_id 定位 transcript（~/.claude/projects/*/<sid>.jsonl）。"""
    if PROJECTS_ROOT.is_dir():
        hits = sorted(PROJECTS_ROOT.glob(f"*/{session_id}.jsonl"))
        if hits:
            return hits[0]
    return None


def transcript_lines(path: Path | None) -> int:
    """transcript 行数（基线锚点）。文件不存在/不可读返回 0。"""
    if path is None or not path.exists():
        return 0
    try:
        with path.open() as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def scan_transcript(path: Path | None, baseline: int, *,
                    head: str | None = None,
                    compact_summary: bool = False) -> str | None:
    """扫描 transcript 第 baseline 行之后的新增条目。

    head 给定：找第一条含 head 且非 /compact 前缀且非 isCompactSummary 的
    user 消息（唤醒词送达确认——compact_cmd 条目内嵌唤醒词全文、压缩摘要
    可能复述唤醒词，两者都必须排除；基线之前的历史条目天然不参与）。
    compact_summary=True：找 isCompactSummary 条目（压缩完成兜底信号）。
    返回命中条目的内容（truthy），未命中 None。"""
    if path is None or not path.exists():
        return None
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return None
    for ln in lines[baseline:]:
        try:
            d = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if compact_summary:
            if d.get("isCompactSummary"):
                return d.get("message", {}).get("content") or "compact summary"
            continue
        if d.get("type") != "user" or d.get("isCompactSummary"):
            continue
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
        if not isinstance(c, str):
            continue
        s = c.strip()
        if not s or s.startswith(MARK_COMPACT_CMD):
            continue
        if head and head in s:
            return s
    return None


def wakeup_head(word: str) -> str:
    """唤醒词开头片段——transcript user 条目的匹配特征。配合「非 /compact
    前缀 + 基线增量」双排除（compact_cmd 内嵌唤醒词全文是 v0.2.2 事故形态）。"""
    return word[:12]


def dump_bottom(task: Task, pane: str) -> None:
    """失败时把屏幕底部留痕进日志（v0.3 屏幕抓取仅剩此用途——诊断，不判定）。"""
    task.log("wakeup unconfirmed, bottom 10 lines dump:")
    for line in capture(pane).rstrip("\n").split("\n")[-10:]:
        task.log(f"  | {line[:120]}")


def inject_line(pane: str, text: str) -> None:
    """注入一行文本并提交：清框 → 文本 → 短歇 → Enter。

    文本+Enter 直发不再做屏幕落地确认——三种形态（idle 明文 / busy 排队 /
    折叠 blob）最终都会提交且 transcript 记录完整原文（模块 docstring）；
    文本被吞则 Enter 提交空框（无害），transcript 无新条目 → 上层重注。"""
    clear_input(pane)
    send_text(pane, text)
    time.sleep(KEY_TEXT_SETTLE)
    send_key(pane, "Enter")


def deliver_wakeup(task: Task, pane: str, session_id: str, reason: str) -> bool:
    """投递唤醒词：注入+Enter 连发 → transcript 出现新增 user 条目含 head。

    轮前复查上轮延迟提交（busy 期 Enter 排队、忙完才写入 transcript）；
    三轮退避重注；失败 FAILED 落盘 + dump 留痕。"""
    word = task.cfg["wakeup_word"]
    head = wakeup_head(word)
    transcript = find_transcript(session_id)
    task.log(f"wakeup transcript: {transcript} (session {session_id[:8]}…)")
    if transcript is None:
        task.log("transcript not found — deliver 确认降级为纯重试（无送达判据）")

    # 基线：注入前的行数。历史条目（compact_cmd / 压缩摘要，均可能内嵌
    # 唤醒词全文）天然不参与判定
    baseline = transcript_lines(transcript)
    for rnd in range(1, DELIVER_ROUNDS + 1):
        time.sleep(DELIVER_BACKOFF[rnd - 1])  # 轮前退避：跨过压缩后忙期
        # 轮前复查：上轮 Enter 排队、模型忙完才写入 transcript（晚于轮内
        # 验证窗）——不复查会重注双发
        if scan_transcript(transcript, baseline, head=head):
            task.update(state="DONE", end_reason=f"{reason} (delayed confirm)")
            task.log(f"wakeup delivered by pre-round check (round {rnd}, {reason})")
            return True

        task.update(state="DELIVERING", wakeup_round=rnd)
        inject_line(pane, word)
        task.log(f"wakeup round {rnd}: injected, waiting transcript entry")
        hit = wait_until(
            lambda: scan_transcript(transcript, baseline, head=head),
            DELIVER_SENT_TIMEOUT, POLL_INTERVAL,
        )
        if hit:
            task.update(state="DONE", end_reason=reason)
            task.log(f"wakeup delivered ({reason})")
            return True
        task.log(f"wakeup round {rnd}: no transcript entry after {DELIVER_SENT_TIMEOUT}s")
        dump_bottom(task, pane)  # 留痕：文本被吞/忙期不收输入的现场

    task.update(state="FAILED", end_reason=f"wakeup unconfirmed ({reason})")
    task.log(f"wakeup FAILED after {DELIVER_ROUNDS} rounds ({reason})")
    return False


def run(task: Task) -> None:
    pane = task.cfg["pane"]
    session_id = task.cfg["session_id"]
    settle = float(task.cfg.get("settle_seconds", 6))
    global_deadline = float(task.cfg.get("global_timeout", 600))
    retries = int(task.cfg.get("retry_limit", 2))

    watcher_start = time.time()
    kill_previous(task)
    task.update(watcher_pid=os.getpid(), state="WAIT_SETTLE")
    task.log(f"watcher start pid={os.getpid()} pane={pane} session={session_id[:8]}…")

    clear_stale_hooks(session_id, watcher_start)

    t0 = time.time()
    time.sleep(settle)  # 等模型收尾总结、TUI 回到输入态

    transcript = find_transcript(session_id)
    # 基线在**注入 /compact 之前**取：本轮压缩的 isCompactSummary 条目落在
    # 新增区（多 attempt 重注也全在基线后）；同 session 上次压缩的历史
    # 摘要条目在基线前，天然不参与判定
    baseline = transcript_lines(transcript)

    def compact_signal():
        """压缩已被接受且（或已开始）的信号：PreCompact 标记，或 transcript
        出现本轮压缩摘要（hook 未注册/旧版 Claude Code 的兜底——那时 pre
        永不到，但压缩真的执行，isCompactSummary 出现 = 已完成）。"""
        return read_hook(session_id, "pre", watcher_start) or \
            scan_transcript(transcript, baseline, compact_summary=True)

    compact_started = None
    for attempt in range(1, retries + 2):
        if not pane_alive(pane):
            task.update(state="ABORTED", end_reason="pane gone")
            return
        if time.time() - t0 > global_deadline:
            break

        task.update(state="INJECTING", attempt=attempt)
        inject_line(pane, task.cfg["compact_cmd"])
        task.log(f"compact injected (attempt {attempt}), waiting PreCompact/transcript signal")

        # 阶段一：PreCompact 标记 = Claude Code 确切接受了 /compact 命令。
        # 长超时覆盖 busy 期输入排队（/compact 在队列里没丢——v0.2.x 21s 等
        # 屏幕特征误判「被吞」双发的事故根源）。swallow 形态（被当普通消息
        # 吞）两信号都不触发，超时自然重试。task.json 可覆盖超时（e2e 加速）。
        pre_timeout = float(task.cfg.get("precompact_timeout", PRECOMPACT_TIMEOUT))
        sig = wait_until(compact_signal, pre_timeout, POLL_INTERVAL)
        if sig:
            compact_started = sig
            break
        task.log(f"attempt {attempt}: no compaction signal in {pre_timeout:g}s, retrying")

    if compact_started is None:
        # 压缩从未被接受（重试耗尽/全局超时）——仍投递唤醒词（晚到无损）
        task.log("no compaction signal after retries, fallback deliver")
        deliver_wakeup(task, pane, session_id, "no compaction signal fallback")
        return

    # 阶段二：压缩进行中——等 PostCompact 标记（主路）或 transcript
    # isCompactSummary（兜底）。绝不重注 /compact。
    task.update(state="COMPACTING")
    task.log("compaction accepted (PreCompact hook)" if read_hook(session_id, "pre", watcher_start)
             else "compaction detected (transcript isCompactSummary)")
    done = wait_until(
        lambda: read_hook(session_id, "post", watcher_start)
        or scan_transcript(transcript, baseline, compact_summary=True),
        COMPACT_DONE_TIMEOUT, POLL_INTERVAL,
    )
    if done:
        task.log("compaction done"
                 + (" (PostCompact hook)" if read_hook(session_id, "post", watcher_start) else " (transcript isCompactSummary)"))
    else:
        task.log(f"no completion signal in {COMPACT_DONE_TIMEOUT}s, fallback deliver")

    deliver_wakeup(task, pane, session_id, "compacted confirmed")


if __name__ == "__main__":
    run(Task(Path(sys.argv[1])))
