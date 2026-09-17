#!/usr/bin/env python3
"""deliver_wakeup + transcript/hook 信号源的单元测试（v0.3）。

v0.3 判据：唤醒词送达 = transcript **新增** user 条目含 head、非 /compact
前缀、非 isCompactSummary（compact_cmd 内嵌唤醒词全文、压缩摘要复述唤醒词
都必须排除——2026-09-17 backend 事故 transcript 段实证形态）；压缩生命
周期 = PreCompact/PostCompact hook 标记，兜底 transcript isCompactSummary。
屏幕抓取仅剩 dump 留痕。

覆盖路径：
  1. happy        注入+Enter → transcript 条目出现 → 一次成功
  2. delayed      round1 验证窗后才写入（busy 期 Enter 排队、忙完才提交）
                  → round2 轮前复查命中 → 不重注（防双发核心不变量）
  3. all_fail     transcript 条目永不出现 → 三轮重注后 FAILED（不假 DONE）
  4. residual     回归（v0.2.2 事故的 transcript 形态）：基线前有 /compact
                  命令条目（内嵌唤醒词全文）+ isCompactSummary 摘要（复述
                  唤醒词）→ 轮前复查不误判，正常投递成功
  5. no_transcript transcript 定位失败 → 无判据，三轮后诚实 FAILED
  6. run 主干     pre/post 标记齐 → COMPACTING → deliver 成功
  7-10. scan_transcript 直测：head 命中 / compact 前缀排除 / 摘要排除 /
        基线增量不漏 / list 型 content
  11-12. hook 标记直测：陈旧 ts 拒绝（上次压缩遗留不误判）/ 启动清删
  13. wait_until 语义直测（v0.2.6 mock 失真教训，跨版本保留）

确定性策略：time.sleep 置 no-op、wait_until 单次求值——transcript 是真实
临时文件，路径完全可推演，无真实时间依赖。
"""
import importlib.util
import json
import time
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "watcher", Path(__file__).parent.parent / "mcp" / "watcher.py"
)
watcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watcher)

_REAL_WAIT_UNTIL = watcher.wait_until  # fast_time mock 前保存真实实现

WORD = "[续任务：批B开工] 去哪读：memory/MEMORY.md 与 project_deploy-8ca3617-20260910.md。怎么续：启动批B workflow 四步。"
HEAD = WORD[:12]
SID = "test-session-abc123"


def write_entry(path: Path, content, compact_summary: bool = False, type_: str = "user") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({
            "type": type_, "isCompactSummary": compact_summary,
            "message": {"role": "user", "content": content},
        }, ensure_ascii=False) + "\n")


class FakeSession:
    """模拟注入目标（tmux 调用记录）+ Claude Code 提交语义（写 transcript）。

    behavior:
      accept  注入的唤醒词即写入 transcript（提交即写——fast_time 下
              wait_until 单次求值发生在 inject_line 之后，行为等价）
      never   永不写（文本被吞 / Enter 全丢）
      delayed round1 验证窗超时后才写入——挂在 watcher.dump_bottom 调用点
              （round1 失败留痕的必经路径），模拟「忙完才提交、写入晚于
              验证窗」（2026-09-17 backend 事故时序）
    """

    def __init__(self, transcript: Path, behavior: str = "accept"):
        self.transcript = transcript
        self.behavior = behavior
        self.text_calls = 0
        self.enter_calls = 0
        self.flushed = False

    def _maybe_write(self, text: str) -> None:
        if self.transcript is None:
            return  # 定位失败的会话：无从写入
        if self.behavior == "accept" or (self.behavior == "delayed" and self.flushed):
            write_entry(self.transcript, text)

    def send_text(self, pane, text):
        self.text_calls += 1
        self._maybe_write(text)

    def send_key(self, pane, key):
        if key == "Enter":
            self.enter_calls += 1

    def tmux(self, *args):
        return ""  # 清框 BSpace 轰炸 / capture 均无害空串


@pytest.fixture(autouse=True)
def fast_time(monkeypatch):
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(watcher, "wait_until", lambda pred, timeout, interval=0.2: pred())


def run_deliver(monkeypatch, tmp_path, session):
    cfg = tmp_path / "task.json"
    cfg.write_text(json.dumps({
        "pane": "%9", "session_id": SID, "wakeup_word": WORD, "state": "PENDING",
    }))
    task = watcher.Task(cfg)
    monkeypatch.setattr(watcher, "send_text", session.send_text)
    monkeypatch.setattr(watcher, "send_key", session.send_key)
    monkeypatch.setattr(watcher, "tmux", session.tmux)
    monkeypatch.setattr(watcher, "capture", lambda pane: "screen")
    monkeypatch.setattr(watcher, "find_transcript", lambda sid: session.transcript)
    ok = watcher.deliver_wakeup(task, "%9", SID, "test reason")
    return ok, task.cfg


def test_happy_path(monkeypatch, tmp_path):
    session = FakeSession(tmp_path / "proj" / f"{SID}.jsonl")
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert ok and cfg["state"] == "DONE"
    assert session.text_calls == 1 and session.enter_calls == 1
    assert cfg["end_reason"] == "test reason"


def test_delayed_submit_recovered_pre_round(monkeypatch, tmp_path):
    """round1 的 Enter 被 busy 排队：transcript 条目写入晚于轮内验证窗；
    round2 退避后轮前复查命中 → 不重注（防双发）。写入时机挂在
    dump_bottom（round1 失败留痕必经点，时序上恰在验证窗超时后）。"""
    session = FakeSession(tmp_path / "proj" / f"{SID}.jsonl", behavior="delayed")
    real_dump = watcher.dump_bottom

    def dump_then_flush(task, pane):
        session.flushed = True
        write_entry(session.transcript, WORD)  # 忙完提交：验证窗超时后才写入
        real_dump(task, pane)

    monkeypatch.setattr(watcher, "dump_bottom", dump_then_flush)
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert ok and cfg["state"] == "DONE"
    assert "delayed confirm" in cfg["end_reason"]
    assert session.text_calls == 1            # 未重注（上轮已送达，写入晚）
    assert session.enter_calls == 1


def test_all_fail_reports_failed(monkeypatch, tmp_path):
    """transcript 条目永不出现（文本反复被吞）→ 三轮重注后 FAILED（不假 DONE）。"""
    session = FakeSession(tmp_path / "proj" / f"{SID}.jsonl", behavior="never")
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert not ok and cfg["state"] == "FAILED"
    assert session.text_calls == 3            # 恰好三轮（退避 0/3/8s），不无限重试
    log = (tmp_path / "watcher.log").read_text()
    assert "bottom 10 lines dump" in log      # 失败现场留痕


def test_residual_entries_no_false_sent(monkeypatch, tmp_path):
    """回归（v0.2.2 事故的 transcript 形态）：基线前已有 /compact 命令条目
    （内嵌唤醒词全文）+ isCompactSummary 摘要条目（复述唤醒词）——轮前复查
    不得误判「已送达」，必须走真实投递。"""
    transcript = tmp_path / "proj" / f"{SID}.jsonl"
    write_entry(transcript, f"/compact 保留任务状态（唤醒词：{WORD}）")
    write_entry(transcript, f"This session is being continued… 摘要含唤醒词：{WORD}",
                compact_summary=True)
    session = FakeSession(transcript)
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert ok and cfg["state"] == "DONE"
    assert session.text_calls == 1            # 真实注入了一次（不是复查短路）
    assert "delayed confirm" not in cfg["end_reason"]


def test_no_transcript_honest_fail(monkeypatch, tmp_path):
    """transcript 定位失败（异常环境）→ 无送达判据 → 三轮后诚实 FAILED。"""
    session = FakeSession(tmp_path / "proj" / f"{SID}.jsonl", behavior="accept")
    session.transcript = None                 # find_transcript 返回 None
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert not ok and cfg["state"] == "FAILED"
    log = (tmp_path / "watcher.log").read_text()
    assert "transcript not found" in log


def test_run_happy_with_hooks(monkeypatch, tmp_path):
    """run 主干：/compact 注入 → pre/post 标记（注入后写入——模拟真实
    hook 由 Claude Code 在接受命令/完成压缩时触发，而非预置）→
    INJECTING 一次成功 → COMPACTING → deliver 成功（不重注 /compact）。"""
    hook_root = tmp_path / "hooks"
    monkeypatch.setattr(watcher, "HOOK_ROOT", hook_root)
    transcript = tmp_path / "proj" / f"{SID}.jsonl"
    write_entry(transcript, "chat history line")   # 基线前的历史

    def write_marker(event: str) -> None:
        hook_root.mkdir(parents=True, exist_ok=True)
        (hook_root / f"{SID}.{event}.json").write_text(json.dumps(
            {"event": event, "ts": time.time() + 3600,   # 未来值确保新于 watcher_start
             "transcript_path": str(transcript)}))

    session = FakeSession(transcript)
    orig_send_text = session.send_text

    def send_text_and_fire_hooks(pane, text):
        orig_send_text(pane, text)
        if text.startswith("/compact"):
            write_marker("pre")   # Claude Code 接受命令 → PreCompact
            write_marker("post")  # fast_time 下压缩即时完成 → PostCompact

    cfg = tmp_path / "task.json"
    cfg.write_text(json.dumps({
        "pane": "%9", "session_id": SID, "compact_cmd": f"/compact xxx（唤醒词：{WORD}）",
        "wakeup_word": WORD, "settle_seconds": 0, "state": "PENDING",
    }))
    task = watcher.Task(cfg)
    monkeypatch.setattr(watcher, "send_text", send_text_and_fire_hooks)
    monkeypatch.setattr(watcher, "send_key", session.send_key)
    monkeypatch.setattr(watcher, "tmux", session.tmux)
    monkeypatch.setattr(watcher, "capture", lambda pane: "screen")
    monkeypatch.setattr(watcher, "pane_alive", lambda pane: True)
    monkeypatch.setattr(watcher, "find_transcript", lambda sid: transcript)
    watcher.run(task)

    assert task.cfg["state"] == "DONE"
    assert task.cfg["end_reason"] == "compacted confirmed"
    assert session.text_calls == 2            # /compact 一次 + 唤醒词一次，无重注
    log = (tmp_path / "watcher.log").read_text()
    assert "compaction accepted (PreCompact hook)" in log
    assert "compaction done (PostCompact hook)" in log


# ---- scan_transcript 判据直测 ----

def test_scan_hits_head_in_new_user_entry(tmp_path):
    p = tmp_path / "t.jsonl"
    write_entry(p, "chat history")
    base = 1
    write_entry(p, WORD)
    assert watcher.scan_transcript(p, base, head=HEAD) == WORD


def test_scan_excludes_compact_cmd_and_summary(tmp_path):
    """compact_cmd 条目（/compact 前缀，内嵌唤醒词全文）与 isCompactSummary
    摘要（复述唤醒词）都不是唤醒词送达。"""
    p = tmp_path / "t.jsonl"
    write_entry(p, "chat history")
    base = 1
    write_entry(p, f"/compact 保留任务状态（唤醒词：{WORD}）")
    write_entry(p, f"摘要复述：{WORD}", compact_summary=True)
    assert watcher.scan_transcript(p, base, head=HEAD) is None


def test_scan_baseline_excludes_history(tmp_path):
    """基线之前的历史条目（含真实唤醒词——上轮已送达）不参与本轮判定。"""
    p = tmp_path / "t.jsonl"
    write_entry(p, WORD)                      # 历史：上轮送达的唤醒词
    assert watcher.scan_transcript(p, 1, head=HEAD) is None


def test_scan_list_content(tmp_path):
    """content 为 list 型（多段 text block）时拼接匹配。"""
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({
        "type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": WORD[:20]}, {"type": "text", "text": WORD[20:]},
        ]},
    }) + "\n")
    assert watcher.scan_transcript(p, 0, head=HEAD) == WORD


def test_scan_compact_summary_mode(tmp_path):
    """compact_summary 模式：找 isCompactSummary 条目（压缩完成兜底信号）。
    lines[baseline:] 语义：前 baseline 行是历史，基线=文件总行数。"""
    p = tmp_path / "t.jsonl"
    write_entry(p, "summary text", compact_summary=True)   # 行 0：历史摘要
    write_entry(p, "chat history")                         # 行 1：基线锚点
    assert watcher.scan_transcript(p, 2, compact_summary=True) is None  # 基线后无
    write_entry(p, "new summary", compact_summary=True)    # 行 2：本轮压缩
    assert watcher.scan_transcript(p, 2, compact_summary=True) is not None


# ---- hook 标记直测 ----

def test_read_hook_rejects_stale(tmp_path, monkeypatch):
    """上次压缩遗留的标记（ts 早于本次 watcher 启动）不得当作本次信号。"""
    monkeypatch.setattr(watcher, "HOOK_ROOT", tmp_path)
    marker = tmp_path / f"{SID}.pre.json"
    marker.write_text(json.dumps({"event": "PreCompact", "ts": 100.0}))
    assert watcher.read_hook(SID, "pre", watcher_start=200.0) is None    # 陈旧
    marker.write_text(json.dumps({"event": "PreCompact", "ts": 300.0}))
    assert watcher.read_hook(SID, "pre", watcher_start=200.0) is not None


def test_clear_stale_hooks_removes_markers(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "HOOK_ROOT", tmp_path)
    for event in ("pre", "post"):
        (tmp_path / f"{SID}.{event}.json").write_text("{}")
    watcher.clear_stale_hooks(SID, time.time())
    assert not (tmp_path / f"{SID}.pre.json").exists()
    assert not (tmp_path / f"{SID}.post.json").exists()


# ---- wait_until 语义直测（v0.2.6 mock 失真教训，跨版本保留）----

def test_wait_until_returns_pred_value(monkeypatch):
    """wait_until 直测（真实实现，不受 fast_time mock）：必须返回 pred()
    真实返回值而非布尔——v0.2.5 真实试用抓出循环内 `return True` 把三态
    布尔化、单测 mock 恰返回 pred() 值的语义失真。"""
    monkeypatch.setattr(watcher, "wait_until", _REAL_WAIT_UNTIL)
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    assert watcher.wait_until(lambda: "hit", 1.0, 0.01) == "hit"
    ticks = iter([0.0, 100.0])  # deadline=1.0；首查 0<1 进循环体，复查 100≥1 退出
    monkeypatch.setattr(watcher.time, "time", lambda: next(ticks))
    assert watcher.wait_until(lambda: None, 1.0, 0.01) is None
