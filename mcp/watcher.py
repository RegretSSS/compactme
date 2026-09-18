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

v0.3.2（2026-09-18 第三次试用事故）：锚点从 session_id 换成 pane cwd。
压缩时 Claude Code 会开新的 continuation transcript（**新 session id 新文件**），
pane 进程 environ 的 CLAUDE_CODE_SESSION_ID 固化在启动值永不更新——v0.3.1 的
一次性反查在上次压缩后即失效（盯 392726e5，标记/transcript 实际在 b1935f68
名下），双通道全盲 → 90s×3 重注双发。修复：
  transcript = claude 进程 cwd → projects 子目录 → mtime 最新 jsonl（活跃文件，
              压缩切文件自动跟上）
  hook 标记  = 扫描式匹配：ts > watcher_start 且 transcript_path 父目录 ==
              本 pane 的 projects 目录（不预知 session id）
  session_id 降级为信息性记录，不参与任何判定。

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

def scan_hook(event: str, watcher_start: float, projects_dir: str) -> dict | None:
    """扫描式匹配 hook 标记，不预知 session id。

    v0.3.2 事故教训：hook 用**压缩时刻的真实 session id** 命名标记文件，
    而 compact 会切换 session id——watcher 无从预知文件名（盯 392726e5，
    标记实际写在 b1935f68 名下）。改扫 HOOK_ROOT 全部 *.<event>.json：
      ts > watcher_start      陈旧标记（上次压缩遗留/同名覆盖前的旧值）天然过滤
      transcript_path 父目录名 == 本 pane 的 projects 目录   跨会话区分
    返回 ts 最新一条（含权威 transcript_path）。同 cwd 双会话同时压缩会串
    （后果仅提前确认压缩开始，deliver 判据独立，可接受）。"""
    best: dict | None = None
    try:
        paths = list(HOOK_ROOT.glob(f"*.{event}.json"))
    except OSError:
        return None
    for p in paths:
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        try:
            ts = float(data.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if ts <= watcher_start:
            continue
        if Path(str(data.get("transcript_path", ""))).parent.name != projects_dir:
            continue
        if best is None or ts > float(best.get("ts", 0)):
            best = data
    return best


# ---- pane 会话定位（v0.3.2：cwd 锚定）----

def _readlink(path: str) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def pane_context(pane: str) -> tuple[str | None, str | None]:
    """BFS pane 进程树找 claude 进程，返回 (session_id, cwd)。

    session_id 仅信息性记录（environ 固化在进程启动值、压缩切换后即过期，
    v0.3.2 起不参与任何判定）；cwd 取 claude 进程的 /proc/<pid>/cwd——
    shell 的 cwd 可能被用户 cd 走，claude 进程的才是会话目录。
    COMPACTME_CWD / COMPACTME_SESSION_ID env 可覆盖（e2e/单测隔离）。"""
    override = os.environ.get("COMPACTME_CWD")
    if override:
        return os.environ.get("COMPACTME_SESSION_ID"), override
    root = tmux("display-message", "-p", "-t", pane, "#{pane_pid}").strip()
    if not root:
        return None, None
    seen: set[str] = set()
    queue = [root]
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        try:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            env = []
        sid = None
        for e in env:
            if e.startswith(b"CLAUDE_CODE_SESSION_ID="):
                sid = e.decode("utf-8", "ignore").split("=", 1)[1]
                break
        if sid:
            return sid, _readlink(f"/proc/{pid}/cwd")
        queue.extend(_child_pids(pid))
    return None, None


def projects_dir_name(cwd: str) -> str:
    """会话 cwd → projects 子目录名（Claude Code 规则：/ 换 -）。
    /home/remedy/x → -home-remedy-x"""
    return cwd.replace("/", "-")


def active_transcript(projects_dir: str) -> Path | None:
    """projects 子目录下 mtime 最新的 jsonl = 活跃 transcript。

    压缩切换 transcript 文件后（v0.3.2 事故形态），新文件必然是最近写入
    的——mtime 排序自动跟上，不依赖任何 session id。"""
    d = PROJECTS_ROOT / projects_dir
    if not d.is_dir():
        return None
    best: Path | None = None
    best_mtime = -1.0
    for p in d.glob("*.jsonl"):
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if m > best_mtime:
            best, best_mtime = p, m
    return best


# ---- transcript 通道（压缩完成兜底 + 唤醒词送达确认）----

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


def deliver_wakeup(task: Task, pane: str, projects_dir: str, reason: str,
                   transcript_hint: Path | None = None) -> bool:
    """投递唤醒词：注入+Enter 连发 → transcript 出现新增 user 条目含 head。

    transcript 定位：hint（post 标记的 transcript_path——hook 直接携带的
    压缩时刻权威路径，免 mtime 猜）优先，active_transcript 兜底（无标记的
    fallback 路径/压缩切文件）。基线锚在压缩后的文件上，唤醒词条目必在
    其后。轮前复查上轮延迟提交（busy 期 Enter 排队、忙完才写入 transcript）；
    三轮退避重注；失败 FAILED 落盘 + dump 留痕。"""
    word = task.cfg["wakeup_word"]
    head = wakeup_head(word)
    transcript = None
    if transcript_hint is not None and transcript_hint.exists():
        transcript = transcript_hint
    if transcript is None:
        transcript = active_transcript(projects_dir)
    task.log(f"wakeup transcript: {transcript}"
             + (" (post hook path)" if transcript is transcript_hint else ""))
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


def _child_pids(pid: str) -> list[str]:
    try:
        out = subprocess.run(
            ["pgrep", "-P", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout
        return out.split()
    except Exception:
        return []


def run(task: Task) -> None:
    pane = task.cfg["pane"]
    # 会话定位（v0.3.2）：claude 进程 cwd → projects 子目录。session_id 仅
    # 信息性记录——environ 固化在启动值、压缩切换后即过期，不参与判定
    session_id, cwd = pane_context(pane)
    if not cwd:
        task.update(state="ABORTED", end_reason="no claude process in pane")
        task.log("ABORTED: pane process scan found no CLAUDE_CODE_SESSION_ID "
                 "(not a Claude Code pane?)")
        return
    projects_dir = projects_dir_name(cwd)
    if session_id and task.cfg.get("session_id") != session_id:
        task.update(session_id=session_id)
    settle = float(task.cfg.get("settle_seconds", 6))
    global_deadline = float(task.cfg.get("global_timeout", 600))
    retries = int(task.cfg.get("retry_limit", 2))

    watcher_start = time.time()
    kill_previous(task)
    task.update(watcher_pid=os.getpid(), state="WAIT_SETTLE")
    task.log(f"watcher start pid={os.getpid()} pane={pane} cwd={cwd} "
             f"projects_dir={projects_dir} session={session_id[:8] if session_id else '?'}…")

    t0 = time.time()
    time.sleep(settle)  # 等模型收尾总结、TUI 回到输入态

    transcript = active_transcript(projects_dir)
    task.log(f"active transcript: {transcript}")
    # 基线在**注入 /compact 之前**取：本轮压缩的 isCompactSummary 条目落在
    # 新增区（多 attempt 重注也全在基线后）；本会话历史压缩的摘要条目在
    # 基线前，天然不参与判定。压缩若切新文件，isCompactSummary 兜底失效
    # （新文件行数 < 基线），由 hook 标记主路兜住——诚实降级边界
    baseline = transcript_lines(transcript)

    def compact_signal():
        """压缩已被接受且（或已开始）的信号：PreCompact 标记（扫描式），或
        transcript 出现本轮压缩摘要（hook 未注册/旧版 Claude Code 的兜底
        ——那时 pre 永不到，但压缩真的执行，isCompactSummary 出现 = 已完成）。"""
        return scan_hook("pre", watcher_start, projects_dir) or \
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
        deliver_wakeup(task, pane, projects_dir, "no compaction signal fallback")
        return

    # 阶段二：压缩进行中——等 PostCompact 标记（主路）或 transcript
    # isCompactSummary（兜底）。绝不重注 /compact。pre 标记里的
    # transcript_path 是压缩时刻的权威路径，据此锁定 transcript。
    if isinstance(compact_started, dict) and compact_started.get("transcript_path"):
        tp = Path(compact_started["transcript_path"])
        if tp.exists():
            transcript = tp
    task.update(state="COMPACTING")
    task.log("compaction accepted (PreCompact hook)" if scan_hook("pre", watcher_start, projects_dir)
             else "compaction detected (transcript isCompactSummary)")
    done = wait_until(
        lambda: scan_hook("post", watcher_start, projects_dir)
        or scan_transcript(transcript, baseline, compact_summary=True),
        COMPACT_DONE_TIMEOUT, POLL_INTERVAL,
    )
    if done:
        task.log("compaction done"
                 + (" (PostCompact hook)" if scan_hook("post", watcher_start, projects_dir) else " (transcript isCompactSummary)"))
    else:
        task.log(f"no completion signal in {COMPACT_DONE_TIMEOUT}s, fallback deliver")

    # post 标记直接携带压缩时刻的权威 transcript_path（hook stdin JSON），
    # 免 mtime 猜——同 cwd 多会话（目录下他人 jsonl 更活跃）也不会锚错
    post_mark = scan_hook("post", watcher_start, projects_dir) if done else None
    hint = None
    if post_mark and post_mark.get("transcript_path"):
        hint = Path(post_mark["transcript_path"])
    deliver_wakeup(task, pane, projects_dir, "compacted confirmed", hint)


if __name__ == "__main__":
    run(Task(Path(sys.argv[1])))
