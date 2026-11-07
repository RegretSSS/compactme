#!/usr/bin/env python3
"""PostToolUse 水位/时间低频注入 hook(compactme 插件版,全会话生效)。

每个 Claude Code 会话各自的 hook 进程拉起本脚本,stdin 携带**本会话**的
session_id / transcript_path——状态按 session_id 隔离(~/.cache/cc-gauge/),
水位从本会话 transcript 计算,会话之间零串扰。

注入策略(其余工具调用完全静默,空 stdout + exit 0,实测 ~20ms/次):
  - 会话首次工具调用:基线注入一次(告知监控存在+当前水位)
  - 每 N 次工具调用(CC_GAUGE_N,默认 25)或距上次注入 ≥ T 秒(默认 900)
  - SOFT/HARD 阈值穿越瞬间立即注入(不等计数/计时窗口)

窗口与阈值自适应(未被 env 覆盖时):从本会话 transcript 最新 assistant 行的
model 字段判定——含 "1m"(如 auto/glm-5.3[1m])→ 1M 窗口,阈值 300k/500k
(对齐用户 CLAUDE.md「判断水位用绝对 token 数」原则);否则 200k 窗口,
阈值 150k/180k(接近原生 autocompact 前的落盘线索提醒)。

环境变量旋钮(显式设置则覆盖自适应):CC_GAUGE_N / CC_GAUGE_T /
CC_GAUGE_WINDOW / CC_GAUGE_SOFT / CC_GAUGE_HARD。
"""
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

N = int(os.environ.get("CC_GAUGE_N", "25"))
T = int(os.environ.get("CC_GAUGE_T", "900"))
ENV_WINDOW = os.environ.get("CC_GAUGE_WINDOW")
ENV_SOFT = os.environ.get("CC_GAUGE_SOFT")
ENV_HARD = os.environ.get("CC_GAUGE_HARD")
TAIL_BYTES = 1 << 20  # 与 compactme statusline 同口径:尾部 1MB 找最新 assistant usage
STATE_DIR = Path.home() / ".cache" / "cc-gauge"


def last_assistant(transcript_path: str):
    """transcript 尾部倒序最新 assistant 行 → (tokens, model)。

    tokens 三项求和(input+cache_creation+cache_read),与 Claude Code 记账
    口径一致;model 取顶层 requestedModel(如 "auto/glm-5.3[1m]",含窗口
    标记;message.model 是 API 路由 id 如 "group/auto-glm-5-3",无标记)。
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return 0, ""
    size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().splitlines()
    if size > TAIL_BYTES and lines:
        lines = lines[1:]
    for raw in reversed(lines):
        if b'"type":"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
            usage = (d.get("message") or {}).get("usage") or {}
        except ValueError:
            continue
        if usage:
            tokens = (usage.get("input_tokens", 0)
                      + usage.get("cache_creation_input_tokens", 0)
                      + usage.get("cache_read_input_tokens", 0))
            return tokens, str(d.get("requestedModel") or "")
    return 0, ""


def fmt(n: int) -> str:
    return f"{n / 1000:.0f}k" if n < 1_000_000 else f"{n / 1_000_000:.2f}M"


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return  # 解析失败静默退出,hook 永不阻塞工具流
    sid = str(data.get("session_id") or "")[:64] or "unknown"
    now = time.time()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 低概率顺带清理陈旧状态(7 天)
    if random.random() < 0.02:
        try:
            for p in STATE_DIR.glob("*.json"):
                if now - p.stat().st_mtime > 7 * 86400:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    sf = STATE_DIR / f"{sid}.json"
    calls, last_ts, last_used = 0, 0.0, 0
    try:
        st = json.loads(sf.read_text())
        calls = int(st.get("calls", 0))
        last_ts = float(st.get("last_ts", 0))
        last_used = int(st.get("last_used", 0))
    except (OSError, ValueError):
        pass

    calls += 1
    # 水位每调用都算(尾部 1MB 读,毫秒级)——支撑越线即注
    used, model = last_assistant(str(data.get("transcript_path") or ""))
    # 窗口判定:env 显式 > requestedModel 含 [1m] 标记 > used 超 200k 逻辑必然 > 默认 200k
    if ENV_WINDOW:
        window = int(ENV_WINDOW)
    elif "1m" in model.lower():
        window = 1_000_000
    elif used > 200_000:
        window = 1_000_000
    else:
        window = 200_000
    soft = int(ENV_SOFT) if ENV_SOFT else (300_000 if window >= 1_000_000 else 150_000)
    hard = int(ENV_HARD) if ENV_HARD else (500_000 if window >= 1_000_000 else 180_000)

    crossing = (last_used < soft <= used) or (last_used < hard <= used)
    due = last_ts == 0 or calls >= N or now - last_ts >= T or crossing
    if not due:
        sf.write_text(json.dumps({"calls": calls, "last_ts": last_ts, "last_used": used}))
        return

    # 命中注入点:时间现取(文档明示注入值会过期,故每注入点重算)
    now_cn = datetime.now(timezone(timedelta(hours=8)))
    parts = [f"⚙ 自主运行监控:上下文水位 {fmt(used)}/{fmt(window)}"
             f"({used / window * 100:.0f}%);当前时间 "
             f"{now_cn.strftime('%Y-%m-%d %H:%M')} {['周一','周二','周三','周四','周五','周六','周日'][now_cn.weekday()]}(UTC+8)。"]
    if used >= hard:
        parts.append(f"‼ 已过硬上限 {fmt(hard)}:立即落盘任务重建线索(memory/plan/progress)并走压缩流程,禁止继续拖。")
    elif used >= soft:
        parts.append(f"⚠ 已过软上限 {fmt(soft)}:在最近的天然断点(阶段完成/任务收尾)主动发起压缩。")
    sf.write_text(json.dumps({"calls": 0, "last_ts": now, "last_used": used}))
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": " ".join(parts),
        }
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
