#!/bin/bash
# e2e.sh — 在真实 tmux pane 里对 fake_claude 跑完整 watcher 流程（v0.3：
# hooks + transcript 信号源）。
#
# 用法: tests/e2e.sh [mode]
#   mode: normal(默认) | swallow | eat_enter | lost_text | paste_collapse | busy_defer
#
# 链路全真：真实 tmux send-keys / 独立 watcher 进程 / 真实文件系统
# （transcript jsonl + hook 标记文件，路径经 COMPACTME_PROJECTS_ROOT 隔离），
# fake_claude 只替代 Claude Code 本体（保真点见其 docstring）。
set -u
MODE="${1:-normal}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="cm-e2e"
RUN="/tmp/compactme-e2e/run-$$"
SID="e2e-$$"
PROJROOT="/tmp/compactme-e2e/projects"
TRANSCRIPT="$PROJROOT/e2e/$SID.jsonl"
mkdir -p "$RUN" "$PROJROOT/e2e"
: > /tmp/compactme-e2e/received.log
rm -f "$TRANSCRIPT" /tmp/compactme/hooks/"$SID".pre.json /tmp/compactme/hooks/"$SID".post.json

tmux kill-session -t "$S" 2>/dev/null
tmux new-session -d -s "$S" -x 120 -y 40 \
  -e MODE="$MODE" -e SESSION_ID="$SID" -e TRANSCRIPT="$TRANSCRIPT" \
  "python3 $ROOT/tests/fake_claude.py"
sleep 1
PANE=$(tmux display-message -p -t "$S" '#{pane_id}')

# 贴近真实的长文本（compact_cmd 内嵌唤醒词全文，总长数百字）
WAKEUP="[续任务：e2e 验证] 去哪读：/tmp/compactme-e2e/received.log 与 task.json 终态。读什么：本模式注入-投递全链路痕迹。怎么续：核对 received.log 中唤醒词行数恰为 1（不重复发送）。"
CMD="/compact e2e 测试压缩：保留任务状态、已完成步骤、关键决策与文件路径（长文本贴近生产形态，内嵌唤醒词全文防止截断误判）。$WAKEUP"

# swallow 模式：缩短 PreCompact 等待（默认 90s 为覆盖 busy 排队，e2e 无 busy）
EXTRA="\"settle_seconds\": 0.5, \"global_timeout\": 120"
[ "$MODE" = "swallow" ] && EXTRA="$EXTRA, \"precompact_timeout\": 5"

cat > "$RUN/task.json" <<EOF
{
  "pane": "$PANE",
  "session_id": "$SID",
  "compact_cmd": "$CMD",
  "wakeup_word": "$WAKEUP",
  $EXTRA,
  "state": "PENDING"
}
EOF

COMPACTME_PROJECTS_ROOT="$PROJROOT" \
  python3 "$ROOT/mcp/watcher.py" "$RUN/task.json" >/dev/null 2>&1 &

STATE=""
for _ in $(seq 1 120); do
  sleep 1
  STATE=$(python3 -c "import json;print(json.load(open('$RUN/task.json')).get('state',''))" 2>/dev/null || true)
  case "$STATE" in DONE|FAILED|ABORTED) break ;; esac
done

END_REASON=$(python3 -c "import json;print(json.load(open('$RUN/task.json')).get('end_reason',''))" 2>/dev/null)
# 唤醒词计数须排除 compact_cmd 内嵌的唤醒词全文（compact 提交行同串命中）
N_WAKE=$(grep "续任务：e2e 验证" /tmp/compactme-e2e/received.log | grep -vc "|/compact" || true)
N_COMPACT=$(grep -c "^RECV|.*|/compact" /tmp/compactme-e2e/received.log || true)
# transcript 层断言（与 watcher 判据同语义：user 条目以唤醒词开头、非摘要、
# 非 /compact 前缀——compact_cmd 内嵌全文与摘要复述都不算送达）
T_WAKE=$(python3 - "$TRANSCRIPT" <<'PYEOF'
import json, sys
n = 0
try:
    lines = open(sys.argv[1]).read().splitlines()
except OSError:
    lines = []
for ln in lines:
    try: d = json.loads(ln)
    except json.JSONDecodeError: continue
    if d.get("type") != "user" or d.get("isCompactSummary"): continue
    c = d.get("message", {}).get("content")
    if isinstance(c, list):
        c = "".join(x.get("text", "") for x in c if isinstance(x, dict))
    s = (c or "").strip()
    if s.startswith("[续任务：e2e 验证]"): n += 1   # 完整原文（折叠 blob 提交也记原文）
print(n)
PYEOF
)
T_COMPACT=$(grep -c '"content": "/compact' "$TRANSCRIPT" 2>/dev/null || true)
T_SUMMARY=$(grep -c "isCompactSummary.: true" "$TRANSCRIPT" 2>/dev/null || true)
H_POST=$([ -f /tmp/compactme/hooks/"$SID".post.json ] && echo 1 || echo 0)

echo "── e2e[$MODE] pane=$PANE state=$STATE reason=$END_REASON"
echo "   compact 提交 $N_COMPACT 次 | 唤醒词提交 $N_WAKE 次 | transcript: 唤醒词(前缀判据) $T_WAKE / compact $T_COMPACT / 摘要 $T_SUMMARY | post hook $H_POST"
echo "── watcher.log 尾部:"
tail -8 "$RUN/watcher.log" 2>/dev/null | sed 's/^/   /'

# 断言：终态 DONE；唤醒词恰 1 次（RECV 与 transcript 双口径）；压缩链路完整
FAIL=0
[ "$STATE" = "DONE" ] || { echo "❌ state=$STATE"; FAIL=1; }
[ "$N_WAKE" -eq 1 ] || { echo "❌ 唤醒词 $N_WAKE 次（应为 1，防重复）"; FAIL=1; }
[ "$T_WAKE" -eq 1 ] || { echo "❌ transcript 唤醒词条目 $T_WAKE 条（应为 1，前缀判据）"; FAIL=1; }
[ "$T_SUMMARY" -ge 1 ] || { echo "❌ transcript 无 isCompactSummary 条目"; FAIL=1; }
[ "$H_POST" = 1 ] || { echo "❌ PostCompact 标记缺失"; FAIL=1; }
case "$MODE" in
  swallow)
    # 被吞的 /compact 即普通 user 消息，transcript 同样落盘（前缀条目）——
    # 但不产生压缩摘要（watcher 判据免疫），重试后恰一次真压缩
    [ "$N_COMPACT" -ge 2 ] || { echo "❌ compact $N_COMPACT 次（swallow 应重试 ≥2）"; FAIL=1; }
    [ "$T_COMPACT" -eq "$N_COMPACT" ] || { echo "❌ transcript compact 条目 $T_COMPACT ≠ RECV $N_COMPACT"; FAIL=1; }
    [ "$T_SUMMARY" -eq 1 ] || { echo "❌ 摘要 $T_SUMMARY 条（被吞的不得产生摘要，应恰 1）"; FAIL=1; } ;;
  *)
    [ "$N_COMPACT" -eq 1 ] || { echo "❌ compact $N_COMPACT 次（应 1，防双发）"; FAIL=1; } ;;
esac
[ "$FAIL" = 0 ] && echo "✅ e2e[$MODE] PASS"
tmux kill-session -t "$S" 2>/dev/null
exit $FAIL
