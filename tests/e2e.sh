#!/bin/bash
# e2e.sh — 在真实 tmux pane 里对 fake_claude 跑完整 watcher 流程。
#
# 用法: tests/e2e.sh [mode]
#   mode: normal(默认) | swallow | eat_enter | lost_text
#
# 链路全真：真实 tmux send-keys / capture-pane / 独立 watcher 进程，
# fake_claude 只替代 Claude Code 本体（保真点见其 docstring）。
set -u
MODE="${1:-normal}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
S="cm-e2e"
RUN="/tmp/compactme-e2e/run-$$"
mkdir -p "$RUN"
: > /tmp/compactme-e2e/received.log

tmux kill-session -t "$S" 2>/dev/null
tmux new-session -d -s "$S" -x 120 -y 40 -e MODE="$MODE" \
  "python3 $ROOT/tests/fake_claude.py"
sleep 1
PANE=$(tmux display-message -p -t "$S" '#{pane_id}')

# 贴近真实的长文本（compact_cmd 内嵌唤醒词全文，总长数百字）
WAKEUP="[续任务：e2e 验证] 去哪读：/tmp/compactme-e2e/received.log 与 task.json 终态。读什么：本模式注入-投递全链路痕迹。怎么续：核对 received.log 中唤醒词行数恰为 1（不重复发送）。"
CMD="/compact e2e 测试压缩：保留任务状态、已完成步骤、关键决策与文件路径（长文本贴近生产形态，内嵌唤醒词全文防止截断误判）。$WAKEUP"

cat > "$RUN/task.json" <<EOF
{
  "pane": "$PANE",
  "compact_cmd": "$CMD",
  "wakeup_word": "$WAKEUP",
  "settle_seconds": 0.5,
  "global_timeout": 60,
  "state": "PENDING"
}
EOF

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

echo "── e2e[$MODE] pane=$PANE state=$STATE reason=$END_REASON"
echo "   compact 提交 $N_COMPACT 次 | 唤醒词提交 $N_WAKE 次"
echo "── watcher.log 尾部:"
tail -8 "$RUN/watcher.log" 2>/dev/null | sed 's/^/   /'

# 断言：终态 DONE；compact 在 normal/lost_text=1 次、swallow≥2；唤醒词恰 1 次
FAIL=0
[ "$STATE" = "DONE" ] || { echo "❌ state=$STATE"; FAIL=1; }
[ "$N_WAKE" -eq 1 ] || { echo "❌ 唤醒词 $N_WAKE 次（应为 1，防重复）"; FAIL=1; }
case "$MODE" in
  normal|eat_enter) [ "$N_COMPACT" -eq 1 ] || { echo "❌ compact $N_COMPACT 次（应 1）"; FAIL=1; } ;;
  swallow|lost_text) [ "$N_COMPACT" -ge 1 ] && [ "$N_COMPACT" -le 3 ] || { echo "❌ compact $N_COMPACT 次（应 1-3）"; FAIL=1; } ;;
esac
[ "$FAIL" = 0 ] && echo "✅ e2e[$MODE] PASS"
tmux kill-session -t "$S" 2>/dev/null
exit $FAIL
