#!/usr/bin/env python3
"""compactme — 模型给自己注入 /compact + 唤醒词两条消息的 MCP server。

v2 架构（事件驱动）：
  compactme() 校验参数 → 写 /tmp/compactme/<pane>/task.json → fork watcher → 立即返回
  watcher 独立进程盯着本 pane 的屏幕帧：确认 Compacted 后再注入唤醒词（post-compact 动态注入）。

多实体隔离：每个 Claude 实例有自己的 $TMUX_PANE，任务目录、watcher、注入目标
全部按 pane 隔离，互不冲突。capture-pane 是只读内存快照，并发轮询安全。
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("compactme")

PANE = os.environ.get("TMUX_PANE")
SESSION_ID = os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID")
HERE = Path(__file__).parent
TASK_ROOT = Path("/tmp/compactme")

# 唤醒词必须携带的"重建线索"最低标准：至少命中一个（或包含具体路径）
CLUE_MARKERS = (
    "TaskList", "任务清单", "任务追踪",
    "CLAUDE.md", "MEMORY.md", "plans/", "PLAN", "TODO", "HANDOFF",
)

# 触发阈值用绝对 token 而非窗口占比：注意力质量随绝对量衰减，不随占比。
# 1M 窗口的 70% = 700K，那时质量早已崩坏；200K 窗口的 70% = 140K 又太松。
HARD_THRESHOLD = 400_000  # ≥ 此值：无条件建议压缩
SOFT_THRESHOLD = 200_000  # ≥ 此值且处于天然断点：建议在断点处压缩

# /compact 注入命令长度上限（v0.3.4，与 watcher.py SAFE_CMD_MAX 同值互指）：
# Claude Code 2.1.312 起超长单块输入被 TUI 折叠成 paste blob，blob 提交不再
# 解析 slash 命令（实测 426 字符过、860 折叠），200 留 2 倍裕量。
SAFE_CMD_MAX = 200


def _verdict(tokens: int) -> tuple[str, str]:
    """三档判定，规则详见 SKILL.md「触发水位」。

    hard: ≥400K，无条件压缩
    soft: ≥200K，需叠加"明显天然断点"才压
    ok:   余量充足（第三档"核心改动前主动整理"由模型判断，不在此判定）
    """
    if tokens >= HARD_THRESHOLD:
        return "已达 400K，无条件建议压缩（先落盘重建线索）", "hard"
    if tokens >= SOFT_THRESHOLD:
        return "已达 200K，若处于明显天然断点（阶段完成/任务收尾）建议在此压缩", "soft"
    return "余量充足", "ok"


def _has_rebuild_clue(wakeup_word: str) -> bool:
    if any(marker in wakeup_word for marker in CLUE_MARKERS):
        return True
    return any("/" in token for token in wakeup_word.split())


def _tmux(*args: str) -> str:
    try:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return ""


def _capture_self() -> str:
    """抓自己 pane 的当前帧（只读）。"""
    if not PANE:
        return ""
    return _tmux("capture-pane", "-p", "-t", PANE, "-S", "-10")


# 状态栏示例：🧠 125.7k/1M 12.6%
_CTX_RE = re.compile(r"🧠\s*([\d.]+)\s*([kKmM]?)\s*/\s*([\d.]+)\s*([kKmM]?)\s*(?:·|/)?\s*([\d.]+)?%")


def _parse_statusbar_context() -> dict | None:
    """从自己 pane 的状态栏解析上下文水位，失败返回 None。"""
    frame = _capture_self()
    m = _CTX_RE.search(frame)
    if not m:
        return None
    mult = {"": 1, "k": 1_000, "m": 1_000_000, "K": 1_000, "M": 1_000_000}
    used = float(m.group(1)) * mult.get(m.group(2), 1)
    total = float(m.group(3)) * mult.get(m.group(4), 1)
    pct = float(m.group(5)) if m.group(5) else (used / total * 100 if total else None)
    return {"context_tokens": int(used), "context_total": int(total), "pct": round(pct, 1) if pct else None}


def _find_transcript() -> Path | None:
    projects = Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return None
    if SESSION_ID:
        for p in projects.glob(f"*/{SESSION_ID}.jsonl"):
            return p
    # 回退：最近修改的 transcript（多会话并行时可能拿错，仅作估算）
    candidates = sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime)
    return candidates[-1] if candidates else None


def _last_usage(transcript: Path) -> dict | None:
    usage = None
    with transcript.open() as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = (entry.get("message") or {}).get("usage")
            if u:
                usage = u
    return usage


@mcp.tool()
def context_stats() -> str:
    """估算当前会话的上下文 token 用量。优先解析本 pane 状态栏（🧠 k/M 数据），
    失败时回退 session transcript 的 usage 字段。"""
    info: dict = {"pane": PANE}
    bar = _parse_statusbar_context()
    if bar:
        info.update(bar, source="statusbar")
        msg, level = _verdict(bar["context_tokens"])
        info.update(threshold_hard_k=HARD_THRESHOLD // 1000, threshold_soft_k=SOFT_THRESHOLD // 1000, level=level)
        info["verdict"] = msg
        return json.dumps(info, ensure_ascii=False)

    transcript = _find_transcript()
    if not transcript:
        return json.dumps({"error": "状态栏未解析到，且找不到 session transcript"}, ensure_ascii=False)
    usage = _last_usage(transcript)
    if not usage:
        return json.dumps({"error": "transcript 中无 usage 记录"}, ensure_ascii=False)
    ctx = (
        usage.get("input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
    )
    msg, level = _verdict(ctx)
    return json.dumps(
        {
            "source": "transcript",
            "transcript": str(transcript),
            "context_tokens": ctx,
            "context_k": round(ctx / 1000, 1),
            "threshold_hard_k": HARD_THRESHOLD // 1000,
            "threshold_soft_k": SOFT_THRESHOLD // 1000,
            "level": level,
            "verdict": msg,
        },
        ensure_ascii=False,
    )


@mcp.tool()
def compactme(compact_instructions: str, wakeup_word: str, settle_seconds: float = 6.0) -> str:
    """给自己安排一次自压缩 + 自唤醒。调用后立即返回；后台 watcher 通过
    PreCompact/PostCompact hook 标记与 session transcript 确认压缩生命周期
    （v0.3 信号源，不依赖屏幕形态），压缩完成后注入唤醒词。

    必须作为本轮最后一个工具调用，之后立即写收尾总结并结束回合。

    Args:
        compact_instructions: /compact 的描述——压缩摘要的侧重，一句话即可
            （如「保留任务状态、已完成步骤与下一步」）。任务重建线索落盘
            是调用前的准备动作，不写进命令；唤醒词也不进命令（压缩后由
            watcher 独立投递）。命令超长会被截断（新版 TUI 超长输入被折
            叠成 paste blob 后 /compact 不再执行，v0.3.4 事故）。
        wakeup_word: 压缩完成后送达的唤醒词，必须包含任务重建线索三要素：
            去哪读（文件路径 / TaskList）、读什么、怎么续（下一步第一个动作）。
            缺少线索会被拒绝执行。
        settle_seconds: 留给模型写收尾总结的等待秒数，默认 6.0。
    """
    if not PANE:
        return "错误：未检测到 $TMUX_PANE——compactme 只在 tmux 内运行的 Claude Code 会话中可用。"
    if not SESSION_ID:
        return "错误：未检测到 CLAUDE_CODE_SESSION_ID——v0.3 依赖 hook 标记与 transcript 信号源，无法定位本会话。"
    if not wakeup_word.strip():
        return "错误：唤醒词不能为空——没有唤醒词，压缩后的新上下文无法恢复任务。"
    if not _has_rebuild_clue(wakeup_word):
        return (
            "错误：唤醒词缺少任务重建线索。必须在唤醒词中写明：去哪读（文件路径或 "
            f"TaskList）、读什么、怎么续。可用线索标记：{CLUE_MARKERS}，或直接写具体路径。"
        )

    compact_instructions = compact_instructions.strip() or "保留当前任务状态、已完成步骤与下一步动作。"
    # v0.3.4 命令瘦身（2026-10-30 两连挂事故，详见 watcher.py v0.3.4 段）：
    # 唤醒词全文不再拼进 /compact——它由 watcher 在压缩完成后独立投递，
    # 塞进命令只是膨胀长度；超长命令会被 2.1.312 TUI 折叠成 paste blob
    # 后当普通消息吞掉（slash 命令不再解析）。压缩准备（任务状态/线索
    # 落盘）本就该在调用 compactme 前完成，命令只带一句摘要侧重即可。
    compact_cmd = f"/compact {compact_instructions}".rstrip()
    if len(compact_cmd) > SAFE_CMD_MAX:
        compact_cmd = compact_cmd[: SAFE_CMD_MAX - 1].rstrip() + "…"

    # per-pane 任务目录：多实体天然隔离
    safe_pane = PANE.lstrip("%") or "unknown"
    task_dir = TASK_ROOT / safe_pane
    task_dir.mkdir(parents=True, exist_ok=True)
    task_file = task_dir / "task.json"

    task = {
        "pane": PANE,
        "session_id": SESSION_ID,
        "compact_cmd": compact_cmd,
        "wakeup_word": wakeup_word,
        "settle_seconds": settle_seconds,
        "state": "PENDING",
        "created_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
    }
    task_file.write_text(json.dumps(task, ensure_ascii=False, indent=2))

    # fork watcher：nohup 脱离本进程生命周期（MCP server 会随 compact 断连重启）
    subprocess.Popen(
        ["nohup", sys.executable, str(HERE / "watcher.py"), str(task_file)],
        stdout=subprocess.DEVNULL,
        stderr=open(task_dir / "watcher.log", "a"),
        start_new_session=True,
    )

    return (
        f"已派发 watcher（任务目录 {task_dir}，session {SESSION_ID[:8]}…）。\n"
        f"  1. {settle_seconds}s 后注入：{compact_cmd}\n"
        f"  2. PostCompact hook / transcript 确认压缩完成后注入唤醒词：{wakeup_word}\n"
        "现在立即结束回合（先给用户一段收尾总结），不要再发起任何工具调用。"
    )


if __name__ == "__main__":
    mcp.run()
