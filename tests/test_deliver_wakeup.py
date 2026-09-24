#!/usr/bin/env python3
"""deliver_wakeup + transcript/hook 信号源的单元测试（v0.3.2）。

v0.3 判据：唤醒词送达 = transcript **新增** user 条目含 head、非 /compact
前缀、非 isCompactSummary（compact_cmd 内嵌唤醒词全文、压缩摘要复述唤醒词
都必须排除——2026-09-17 backend 事故 transcript 段实证形态）；压缩生命
周期 = PreCompact/PostCompact hook 标记，兜底 transcript isCompactSummary。
屏幕抓取仅剩 dump 留痕。

v0.3.2 锚点：pane cwd → projects 子目录 → mtime 最新 jsonl；hook 标记扫描式
匹配（ts > watcher_start 且 transcript_path 父目录 == projects 目录）。
session_id 不参与判定（2026-09-18 第三次试用事故：compact 切换 session id
新文件、environ 固化旧值，盯 sid 的双通道全盲 → 双发）。

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
  7. session 切换回归（v0.3.2 事故形态）：压缩切新 transcript 文件后
     active_transcript 按 mtime 跟上，deliver 锚对文件
  8-12. scan_transcript 直测：head 命中 / compact 前缀排除 / 摘要排除 /
        基线增量不漏 / list 型 content
  13-14. scan_hook 直测：陈旧 ts 拒绝 + 跨会话目录过滤 + 文件名 session id 无关
  15. wait_until 语义直测（v0.2.6 mock 失真教训，跨版本保留）

确定性策略：time.sleep 置 no-op、wait_until 单次求值——transcript 是真实
临时文件（目录结构对齐 PROJECTS_ROOT/<projects_dir>/），路径完全可推演，
无真实时间依赖。
"""
import importlib.util
import json
import os
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
CWD = "/proj"                     # 会话 cwd → projects 子目录 "-proj"
PROJECTS_DIR = "-proj"


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
    """真实目录结构：tmp_path 充当 PROJECTS_ROOT，transcript 落在
    <root>/<projects_dir>/<sid>.jsonl——active_transcript 走真实实现。"""
    monkeypatch.setattr(watcher, "PROJECTS_ROOT", tmp_path)
    cfg = tmp_path / "task.json"
    cfg.write_text(json.dumps({
        "pane": "%9", "session_id": SID, "wakeup_word": WORD, "state": "PENDING",
    }))
    task = watcher.Task(cfg)
    monkeypatch.setattr(watcher, "send_text", session.send_text)
    monkeypatch.setattr(watcher, "send_key", session.send_key)
    monkeypatch.setattr(watcher, "tmux", session.tmux)
    monkeypatch.setattr(watcher, "capture", lambda pane: "screen")
    ok = watcher.deliver_wakeup(task, "%9", PROJECTS_DIR, "test reason")
    return ok, task.cfg


def test_happy_path(monkeypatch, tmp_path):
    transcript = tmp_path / PROJECTS_DIR / f"{SID}.jsonl"
    write_entry(transcript, "chat history")    # 真实场景：会话进行中 transcript 必已存在
    session = FakeSession(transcript)
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert ok and cfg["state"] == "DONE"
    assert session.text_calls == 1 and session.enter_calls == 1
    assert cfg["end_reason"] == "test reason"


def test_delayed_submit_recovered_pre_round(monkeypatch, tmp_path):
    """round1 的 Enter 被 busy 排队：transcript 条目写入晚于轮内验证窗；
    round2 退避后轮前复查命中 → 不重注（防双发）。写入时机挂在
    dump_bottom（round1 失败留痕必经点，时序上恰在验证窗超时后）。"""
    transcript = tmp_path / PROJECTS_DIR / f"{SID}.jsonl"
    write_entry(transcript, "chat history")
    session = FakeSession(transcript, behavior="delayed")
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
    transcript = tmp_path / PROJECTS_DIR / f"{SID}.jsonl"
    write_entry(transcript, "chat history")
    session = FakeSession(transcript, behavior="never")
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert not ok and cfg["state"] == "FAILED"
    assert session.text_calls == 3            # 恰好三轮（退避 0/3/8s），不无限重试
    log = (tmp_path / "watcher.log").read_text()
    assert "bottom 10 lines dump" in log      # 失败现场留痕


def test_residual_entries_no_false_sent(monkeypatch, tmp_path):
    """回归（v0.2.2 事故的 transcript 形态）：基线前已有 /compact 命令条目
    （内嵌唤醒词全文）+ isCompactSummary 摘要条目（复述唤醒词）——轮前复查
    不得误判「已送达」，必须走真实投递。"""
    transcript = tmp_path / PROJECTS_DIR / f"{SID}.jsonl"
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
    session = FakeSession(None, behavior="accept")   # projects 目录里没有任何 jsonl
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert not ok and cfg["state"] == "FAILED"
    log = (tmp_path / "watcher.log").read_text()
    assert "transcript not found" in log
    # hint 与 transcript 双 None 时不得误标 "(post hook path)"（v0.3.3
    # log bug：jzx 事故 fallback deliver 打出 None (post hook path)）
    assert "(post hook path)" not in log


def test_run_happy_with_hooks(monkeypatch, tmp_path):
    """run 主干：/compact 注入 → pre/post 标记（注入后写入——模拟真实
    hook 由 Claude Code 在接受命令/完成压缩时触发，而非预置）→
    INJECTING 一次成功 → COMPACTING → deliver 成功（不重注 /compact）。

    标记文件名用**与 task.json 不同的 session id**——v0.3.2 语义：hook 按
    压缩时刻真实 sid 命名，watcher 扫描式匹配不预知文件名。"""
    monkeypatch.setattr(watcher, "PROJECTS_ROOT", tmp_path)
    hook_root = tmp_path / "hooks"
    monkeypatch.setattr(watcher, "HOOK_ROOT", hook_root)
    transcript = tmp_path / PROJECTS_DIR / f"{SID}.jsonl"
    write_entry(transcript, "chat history line")   # 基线前的历史

    real_sid = "real-session-xyz789"               # 压缩时刻的真实 sid ≠ task 记录

    def write_marker(event: str) -> None:
        hook_root.mkdir(parents=True, exist_ok=True)
        (hook_root / f"{real_sid}.{event}.json").write_text(json.dumps(
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
    monkeypatch.setattr(watcher, "pane_context", lambda pane: (SID, CWD))
    watcher.run(task)

    assert task.cfg["state"] == "DONE"
    assert task.cfg["end_reason"] == "compacted confirmed"
    assert session.text_calls == 2            # /compact 一次 + 唤醒词一次，无重注
    log = (tmp_path / "watcher.log").read_text()
    assert "compaction accepted (PreCompact hook)" in log
    assert "compaction done (PostCompact hook)" in log


def test_deliver_after_transcript_switch(monkeypatch, tmp_path):
    """v0.3.2 事故核心回归：压缩把写入切到**新 session id 的新 transcript
    文件**（environ 里的旧 sid 定位失效）。deliver 进门按 mtime 重新定位
    活跃文件，唤醒词条目在新文件上确认。"""
    d = tmp_path / PROJECTS_DIR
    old = d / "392726e5.jsonl"
    write_entry(old, "旧文件历史（上次压缩前）")
    new = d / "b1935f68.jsonl"
    write_entry(new, "continuation 开头（压缩摘要落在这里）")
    os.utime(old, (1, 1))                     # 旧文件 mtime 拉开，新文件为活跃

    session = FakeSession(new)                # 唤醒词将写入新文件
    ok, cfg = run_deliver(monkeypatch, tmp_path, session)
    assert ok and cfg["state"] == "DONE"
    assert session.text_calls == 1
    # 送达判据命中的确实是新文件（旧文件不含唤醒词，若锚错必 FAILED/误判）
    assert HEAD in new.read_text()


def test_deliver_prefers_post_hook_path_over_mtime(monkeypatch, tmp_path):
    """post 标记的 transcript_path 是 hook stdin 直接携带的权威路径——
    同 cwd 另一会话的 jsonl 更活跃（mtime 最新）也不能把 deliver 带偏
    （纯 mtime 方案在该场景会锚错文件、判据永假 → 假 FAILED）。"""
    monkeypatch.setattr(watcher, "PROJECTS_ROOT", tmp_path)
    d = tmp_path / PROJECTS_DIR
    mine = d / "mine.jsonl"            # 本会话（post 标记指向这里）
    other = d / "other.jsonl"          # 同 cwd 另一会话，写入更频繁
    write_entry(mine, "history")
    write_entry(other, "other session traffic")
    os.utime(mine, (1, 1))             # mtime 上 other 更新
    session = FakeSession(mine)        # 唤醒词将写入 mine
    cfg = tmp_path / "task.json"
    cfg.write_text(json.dumps({
        "pane": "%9", "wakeup_word": WORD, "state": "PENDING",
    }))
    task = watcher.Task(cfg)
    monkeypatch.setattr(watcher, "send_text", session.send_text)
    monkeypatch.setattr(watcher, "send_key", session.send_key)
    monkeypatch.setattr(watcher, "tmux", session.tmux)
    monkeypatch.setattr(watcher, "capture", lambda pane: "screen")
    ok = watcher.deliver_wakeup(task, "%9", PROJECTS_DIR, "test reason",
                                 transcript_hint=mine)
    assert ok and task.cfg["state"] == "DONE"
    assert HEAD in mine.read_text()
    log = (tmp_path / "watcher.log").read_text()
    assert "(post hook path)" in log


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


# ---- 定位与 hook 扫描直测（v0.3.2）----

def test_projects_dir_name_escapes_non_ascii():
    """v0.3.3 事故回归（2026-09-25 jzx pane %6）：Claude Code 把 cwd 里
    非 [a-zA-Z0-9-] 字符**每个**替换成一个 -（含 /、_、.、中文）——只替换 /
    会算出带中文的目录名 → active_transcript/scan_hook 双盲 → 3 次重注。
    样本一为事故现场，样本二为 headless claude 实测。"""
    assert watcher.projects_dir_name("/mnt/d/000-自制软件/建筑信源") \
        == "-mnt-d-000----------"
    assert watcher.projects_dir_name("/tmp/试_验.目录") == "-tmp-------"
    # ASCII 回归：原 - 保留，不受影响
    assert watcher.projects_dir_name("/home/remedy/workspace/eng-assistant-ops") \
        == "-home-remedy-workspace-eng-assistant-ops"


def test_active_transcript_survives_session_switch(tmp_path, monkeypatch):
    """v0.3.2 事故回归：压缩切走 transcript（新 session id 新文件）后，
    按 session id 定位必失效——active_transcript 按 mtime 跟上切换。"""
    monkeypatch.setattr(watcher, "PROJECTS_ROOT", tmp_path)
    d = tmp_path / PROJECTS_DIR
    old = d / "392726e5.jsonl"
    new = d / "b1935f68.jsonl"
    write_entry(old, "old")
    write_entry(new, "new")
    os.utime(old, (1, 1))
    assert watcher.active_transcript(PROJECTS_DIR) == new
    os.utime(new, (0, 0))                     # 反向：旧文件又变活跃（新一轮切换）
    assert watcher.active_transcript(PROJECTS_DIR) == old


def test_scan_hook_stale_and_dir_filter(tmp_path, monkeypatch):
    """scan_hook 三重语义：陈旧 ts 拒绝（上次压缩遗留）、transcript_path
    父目录过滤（跨会话区分）、文件名 session id 与匹配无关（v0.3.2 事故：
    watcher 无从预知压缩时刻的真实 sid）。"""
    monkeypatch.setattr(watcher, "HOOK_ROOT", tmp_path)
    (tmp_path / "aaa.pre.json").write_text(json.dumps(
        {"event": "PreCompact", "ts": 100.0, "transcript_path": f"/x/{PROJECTS_DIR}/a.jsonl"}))
    assert watcher.scan_hook("pre", 200.0, PROJECTS_DIR) is None   # 陈旧拒绝
    (tmp_path / "bbb.pre.json").write_text(json.dumps(
        {"event": "PreCompact", "ts": 300.0, "transcript_path": f"/x/{PROJECTS_DIR}/b.jsonl"}))
    hit = watcher.scan_hook("pre", 200.0, PROJECTS_DIR)
    assert hit and hit["ts"] == 300.0                              # 文件名 sid 未知也命中
    (tmp_path / "ccc.pre.json").write_text(json.dumps(
        {"event": "PreCompact", "ts": 400.0, "transcript_path": "/x/-other/c.jsonl"}))
    hit = watcher.scan_hook("pre", 200.0, PROJECTS_DIR)
    assert hit and hit["ts"] == 300.0                              # 他 cwd 会话被目录过滤


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
