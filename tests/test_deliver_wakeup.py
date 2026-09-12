#!/usr/bin/env python3
"""deliver_wakeup 可靠投递协议的单元测试（mock 屏幕 + 按键记录）。

v0.2.5 判据：成功 = 消息流最后一条 ❯ 用户消息含唤醒词 head 且非 /compact
前缀，且 tail 不在输入区（文本在框 ⇔ 尚未提交的守卫）。busy 不再是成功信号。
折叠 paste 态（v0.2.5，2026-09-13 pane %2 生产事故）：busy 期大块注入被折叠为
[Pasted text #N] 占位符、明文不上屏 → 二次注入展开；展开失败则直接提交 blob，
以「提示消失 + 编号递增的 blob 消息行」判成功。

覆盖路径：
  1. happy        注入落地 → Enter → ❯ 消息出现 → 一次成功
  2. eat_enter    第一个 Enter 被 TUI 吞（文本滞留输入框）→ 只补 Enter 成功，
                  绝不重发文本（防双发核心不变量）
  3. lost_text    第 1 轮注入文本被重绘吞掉 → dump 留痕 → 轮前退避后重注成功
  4. residual     回归（v0.2.2 生产事故 03:26/11:14）：压缩后消息流残留
                  /compact 命令行（❯ 用户消息、内嵌唤醒词全文）——判据必须
                  以 /compact 前缀排除之；配合 Enter 被吞仍走 nudge
  5. late_render  Enter 已提交但 ❯ 消息渲染滞后（帧队列精确驱动）→ 不重注
                  不补 Enter，延迟复检期判据出现 → DONE
  6. no_busy      提交后 busy 永不出现——❯ 判据独立于 busy（v0.2.4 关键变更：
                  压缩后模型自动继续也会 busy，busy 不能作成功信号）
  7. all_fail     三轮全失败 → state=FAILED（不假 DONE）
  8. prefill      输入框有残留脏文本 → 注入前 BSpace 清框，提交内容纯净
  9. paste_expand 回归（v0.2.5 生产事故）：注入被折叠 → 二次注入展开明文 →
                  正常 Enter → ❯ 判据成功
  10. paste_submit 展开失败（永远折叠）→ blob 直接 Enter 提交（用户手动
                  验证过的救法）→ collapsed_sent 编号判据成功
  11. delayed     round1 的 Enter 排队、提交渲染晚于轮内全部验证窗口 →
                  round2 轮前复查命中 → 不重注（防双发）
  12-15. 判据直测 sent_signal 三形态 + collapsed_sent 三形态

确定性策略：time.sleep 置 no-op、wait_until 单次求值——capture 消耗帧队列，
路径完全可推演，无真实时间依赖。
"""
import importlib.util
import json
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "watcher", Path(__file__).parent.parent / "mcp" / "watcher.py"
)
watcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watcher)

WORD = "[续任务：批B开工] 去哪读：memory/MEMORY.md 与 project_deploy-8ca3617-20260910.md。怎么续：启动批B workflow 四步。"
HEAD, TAIL = watcher.wakeup_markers(WORD)
FILLER = "\n".join(f"chat history line {i:03d}" for i in range(30))
INPUT_ONE_LINE = 40  # 输入框一行容量（超过即折行：首行溢出到消息流区）


def frame_of_collapsed(blob_seq: int, chat_lines: list[str] | None = None) -> str:
    """折叠 paste 帧形态（仿 2026-09-13 pane %2 FAILED 现场 dump）：
    blob 预览行（上下分隔线）+ 状态行 + paste again to expand 提示 + 空行 +
    空输入框——明文 tail 不上屏，仅提示可判折叠。"""
    chat = list(chat_lines or [])
    chat += ["─" * 40, f"❯ [Pasted text #{blob_seq}]", "─" * 40]
    bottom = ["  status line", "  paste again to expand", "", "❯\xa0"]
    return "\n".join([FILLER] + chat + bottom)


def frame_of(input_text: str = "", chat_lines: list[str] | None = None, busy: bool = False) -> str:
    """构造模拟帧：30 行消息流 + 输入框（折行溢出保真）+ 边框 + 状态行。

    长文本折行（2026-09-12 实测真 TUI 形态）：输入框首行（❯ + head 段）
    被推进消息流区，tail 恒在末行——sent_signal 必须不被框首行的 head 欺骗。
    """
    chat_lines = list(chat_lines or [])
    if busy:
        chat_lines.append("  (esc to interrupt while thinking…)")
    if len(input_text) > INPUT_ONE_LINE:
        chat_lines.append("❯\xa0" + input_text[:INPUT_ONE_LINE])  # 折行首行溢出到消息流
        tail_line = input_text[-30:]                               # 末行无前缀
    else:
        tail_line = ("❯\xa0" + input_text) if input_text else "❯\xa0"  # 单行/空框
    # 真实屏幕底部 4 个实行：输入行 + 边框 + 两条状态行（无尾空行——
    # capture 输出的尾换行是 split artifact，split_screen 会剔除）
    bottom = [tail_line, "│ input border │", "─ status ─", "  ⏵⏵ mode line"]
    return "\n".join([FILLER] + chat_lines + bottom)


class FakeScreen:
    """模拟 Claude Code pane：注入文本进输入框，Enter 提交为 ❯ 用户消息。

    行为参数（模拟各类 TUI 竞态）：
      drop_first_text : 第一次 send_text 的文本被重绘吞掉（不回显）
      eat_first_enter : 第一个 Enter 被吞（文本滞留输入区）
      compact_residual: 消息流常驻一条 ❯ /compact 命令行（内嵌唤醒词全文，
                        复现 v0.2.2 生产事故的屏幕形态）
      prefill         : 输入框预置残留脏文本（测 BSpace 清框）
      no_busy         : 提交后不渲染 busy（测 ❯ 判据独立于 busy）
      paste_collapse  : "expand"=大块注入折叠为 blob，再注入一次展开为明文
                        （paste again to expand）；"never"=永远折叠（测兜底
                        提交）。None=不折叠（正常路径）
    """

    def __init__(self, drop_first_text=False, eat_first_enter=False, compact_residual=False,
                 prefill="", no_busy=False, paste_collapse=None):
        self.drop_first_text = drop_first_text
        self.eat_first_enter = eat_first_enter
        self.no_busy = no_busy
        self.paste_collapse = paste_collapse
        self.text_count = 0
        self.enter_count = 0
        self.input = prefill
        self.busy = False
        self.submitted = []   # 提交的原文（断言清框纯净用）
        self.messages = []    # 已提交的 ❯ 用户消息（渲染进消息流）
        self.collapsed = False
        self.pending = ""     # 折叠 blob 持有的原文
        self.blob_seq = 0     # paste 编号（会话级递增计数器）
        self.residual = (
            [f"❯ /compact 保留任务状态（压缩完成后将收到唤醒词：{WORD}）"]
            if compact_residual else []
        )

    def frame(self) -> str:
        # residual（压缩前的旧消息）在上，新提交的 ❯ 消息渲染在其下方——
        # 真实屏幕时序，last_user_line 由此才能取到最新用户消息
        if self.collapsed:
            return frame_of_collapsed(self.blob_seq, self.residual + self.messages)
        return frame_of(self.input, self.residual + self.messages, self.busy)

    def bs(self, n: int) -> None:
        self.input = self.input[: max(0, len(self.input) - n)]

    def send_text(self, pane, text):
        self.text_count += 1
        if self.drop_first_text and self.text_count == 1:
            return  # 文本被 TUI 重绘吞掉：不回显
        if self.paste_collapse and len(text) > INPUT_ONE_LINE:
            # 大块瞬时文本 → Claude Code 按 paste 处理折叠为 blob（明文不上屏）
            if self.collapsed and self.paste_collapse == "expand":
                # 再粘贴一次 → 展开：blob 转明文（两份拼接，语义无害）
                self.input = self.pending + text
                self.pending = ""
                self.collapsed = False
            else:
                self.collapsed = True
                self.blob_seq += 1
                self.pending = text
            return
        self.input = text

    def send_key(self, pane, key):
        if key == "C-u":
            return  # 真 TUI 无清行语义（实测），fake 同样无操作
        if key == "BSpace":
            self.bs(1)
            return
        if key == "Enter":
            self.enter_count += 1
            if self.eat_first_enter and self.enter_count == 1:
                return  # Enter 被吞：文本滞留输入区
            if self.collapsed:
                # 折叠 blob 直接提交（2026-09-13 pane %2 用户手动 Enter 验证）：
                # 消息流渲染为折叠形态消息行，提示消失
                self.submitted.append(self.pending)
                self.messages.append(f"❯ [Pasted text #{self.blob_seq}]")
                self.pending = ""
                self.collapsed = False
                if not self.no_busy:
                    self.busy = True
                return
            if not self.input:
                return  # 空框 Enter：无操作
            self.submitted.append(self.input)
            self.messages.append(f"❯ {self.input}")
            self.input = ""
            if not self.no_busy:
                self.busy = True


@pytest.fixture(autouse=True)
def fast_time(monkeypatch):
    monkeypatch.setattr(watcher.time, "sleep", lambda s: None)
    monkeypatch.setattr(watcher, "wait_until", lambda pred, timeout, interval=0.2: pred())


class Recorder:
    """包装 send/send_key/tmux，统计注入与 Enter 次数，BSpace 轰炸转屏幕。"""

    def __init__(self, screen, frames: list[str] | None = None):
        self.screen = screen
        self.frames = frames or []
        self.text_calls = 0
        self.enter_calls = 0

    def send_text(self, pane, text):
        self.text_calls += 1
        self.screen.send_text(pane, text)

    def send_key(self, pane, key):
        if key == "Enter":
            self.enter_calls += 1
        self.screen.send_key(pane, key)

    def tmux(self, *args):
        # clear_input 的 BSpace 轰炸走 tmux 直调（真实环境单条命令发 1200 个）
        if args[0] == "send-keys" and "BSpace" in args:
            self.screen.bs(len(args) - 3)
        return ""

    def capture(self, pane):
        if self.frames:
            return self.frames.pop(0)  # 帧队列优先（精确驱动滞后类路径）
        return self.screen.frame()


def run_deliver(monkeypatch, tmp_path, screen, frames=None):
    rec = Recorder(screen, frames)
    cfg = tmp_path / "task.json"
    cfg.write_text(json.dumps({"pane": "%9", "wakeup_word": WORD, "state": "PENDING"}))
    task = watcher.Task(cfg)
    monkeypatch.setattr(watcher, "send_text", rec.send_text)
    monkeypatch.setattr(watcher, "send_key", rec.send_key)
    monkeypatch.setattr(watcher, "tmux", rec.tmux)
    monkeypatch.setattr(watcher, "capture", rec.capture)
    ok = watcher.deliver_wakeup(task, "test reason")
    return ok, task.cfg, rec


def test_happy_path(monkeypatch, tmp_path):
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, FakeScreen())
    assert ok and cfg["state"] == "DONE"
    assert rec.text_calls == 1 and rec.enter_calls == 1


def test_eat_enter_nudge_no_resend(monkeypatch, tmp_path):
    """第一个 Enter 被吞 → 只补 Enter，绝不重发文本。"""
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, FakeScreen(eat_first_enter=True))
    assert ok and cfg["state"] == "DONE"
    assert rec.text_calls == 1            # 文本只注入一次（防双发）
    assert rec.enter_calls == 2           # 补了一次 Enter
    assert "nudge 1" in (tmp_path / "watcher.log").read_text()


def test_lost_text_full_reinject_with_dump(monkeypatch, tmp_path):
    """注入文本被吞 → dump 现场留痕 → 轮前退避后重注成功。"""
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, FakeScreen(drop_first_text=True))
    assert ok and cfg["state"] == "DONE"
    assert rec.text_calls == 2            # 两轮全量注入
    log = (tmp_path / "watcher.log").read_text()
    assert "landed=False, bottom 10 lines dump:" in log  # 失败现场已留痕


def test_residual_compact_cmd_no_false_sent(monkeypatch, tmp_path):
    """回归（v0.2.2 生产事故）：压缩后消息流残留 ❯ /compact 命令行（内嵌
    唤醒词全文）+ 第一个 Enter 被吞——残留不得触发已发送误判漏发，必须走
    nudge 补发成功。"""
    screen = FakeScreen(eat_first_enter=True, compact_residual=True)
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, screen)
    assert ok and cfg["state"] == "DONE"
    assert rec.text_calls == 1
    assert rec.enter_calls == 2
    log = (tmp_path / "watcher.log").read_text()
    assert "nudge 1" in log


def test_late_render_no_reinject(monkeypatch, tmp_path):
    """Enter 已提交但 ❯ 消息渲染滞后：sent 验证期/nudge 首查都是中间帧
    （tail 不在输入区、无新 ❯ 消息、残留行被 /compact 排除）→ 不 nudge、
    不重注，延迟复检期判据出现 → DONE。

    帧队列按 deliver_wakeup 的 capture 顺序精确驱动：
      ⓪ 入口基线扫描 ① 轮前复查（不命中） ② prev_max 扫描
      ③ 落地确认（文本在输入区，折行首行溢出到消息流）
      ④ Enter 后 sent 验证（中间帧）
      ⑤ nudge 首查（中间帧 → tail 不在输入区 → break 出 nudge）
      ⑥ 延迟复检（❯ 新消息已渲染）
    """
    screen = FakeScreen(compact_residual=True)
    frames = [
        frame_of(),                                          # ⓪ 入口基线扫描
        frame_of(chat_lines=["  ⎿ Compacted (ctrl+o"]),     # ① 轮前复查：不命中
        frame_of(),                                          # ② prev_max 扫描
        frame_of(input_text=WORD),                           # ③ 落地确认
        frame_of(chat_lines=["  ⎿ Compacted (ctrl+o"]),     # ④ sent 验证：中间帧
        frame_of(chat_lines=["  ⎿ Compacted (ctrl+o"]),     # ⑤ nudge 首查：中间帧
        frame_of(chat_lines=["❯ " + WORD]),                 # ⑥ 新消息已渲染
    ]
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, screen, frames=frames)
    assert ok and cfg["state"] == "DONE"
    assert "late render" in cfg["end_reason"]
    assert rec.text_calls == 1            # 绝不重注（防误判双发）
    assert rec.enter_calls == 1           # 也不补 Enter（tail 不在输入区）


def test_sent_without_busy(monkeypatch, tmp_path):
    """提交后 busy 永不出现——❯ 判据独立于 busy（v0.2.4：压缩后模型自动
    继续也会 busy，busy 不能作成功信号，否则假 DONE 漏发）。"""
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, FakeScreen(no_busy=True))
    assert ok and cfg["state"] == "DONE"
    assert rec.text_calls == 1 and rec.enter_calls == 1


def test_all_fail_reports_failed(monkeypatch, tmp_path):
    """文本永远不落地 → 三轮后 FAILED（不假 DONE）。"""
    class DeadScreen(FakeScreen):
        def send_text(self, pane, text):
            self.text_count += 1  # 一切注入都石沉大海
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, DeadScreen())
    assert not ok and cfg["state"] == "FAILED"
    assert rec.text_calls == 3            # 恰好三轮（退避 0/3/8s），不无限重试


def test_clears_prefill_before_inject(monkeypatch, tmp_path):
    """输入框有残留脏文本 → 注入前 BSpace 清框，提交内容纯净无拼接。

    C-u 在 Claude Code 无清行语义（实测），旧版用 C-u 清框在框非空时会
    把唤醒词拼接在残留后面发出脏消息。"""
    screen = FakeScreen(prefill="上次失败滞留的脏文本残留")
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, screen)
    assert ok and cfg["state"] == "DONE"
    assert screen.submitted == [WORD]     # 提交的恰是纯唤醒词


def test_paste_collapse_expand_path(monkeypatch, tmp_path):
    """回归（v0.2.5 pane %2 生产事故）：busy 期注入被折叠为 [Pasted text #N]
    blob（明文不上屏）→ 二次注入模拟「再粘贴一次」展开为明文 → 正常
    Enter → ❯ 判据成功。"""
    screen = FakeScreen(paste_collapse="expand")
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, screen)
    assert ok and cfg["state"] == "DONE"
    assert rec.text_calls == 2            # 注入 + 展开性重发
    assert rec.enter_calls == 1
    log = (tmp_path / "watcher.log").read_text()
    assert "paste collapsed, re-send to expand" in log
    assert screen.submitted == [WORD + WORD]  # 展开为两份拼接（语义无害）


def test_paste_collapse_submit_fallback(monkeypatch, tmp_path):
    """展开失败（永远折叠）→ 折叠 blob 直接 Enter 提交（用户 2026-09-13 手动
    Enter 验证过的救法）→ collapsed_sent 编号判据成功。"""
    screen = FakeScreen(paste_collapse="never")
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, screen)
    assert ok and cfg["state"] == "DONE"
    assert rec.text_calls == 2            # 注入 + 展开尝试（未生效）
    assert rec.enter_calls == 1
    log = (tmp_path / "watcher.log").read_text()
    assert "paste collapsed, re-send to expand" in log
    assert screen.submitted == [WORD]     # blob 持有的完整唤醒词被提交


def test_delayed_submit_recovered_pre_round(monkeypatch, tmp_path):
    """round1 的 Enter 被 busy 排队：提交渲染晚于轮内全部验证窗口；
    round2 退避后轮前复查命中 → 不重注（防双发）。"""
    screen = FakeScreen()
    frames = [
        frame_of(),                                      # ⓪ 入口基线扫描
        frame_of(),                                      # ① r1 轮前复查：不命中
        frame_of(),                                      # ② r1 prev_max 扫描
        frame_of(input_text=WORD),                       # ③ r1 落地确认
        frame_of(),                                      # ④ r1 sent 验证：无新消息
        frame_of(),                                      # ⑤ r1 nudge 首查 → break
        frame_of(),                                      # ⑥ r1 late recheck：False
        frame_of(chat_lines=["❯ " + WORD]),             # ⑦ r2 轮前复查：命中
    ]
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, screen, frames=frames)
    assert ok and cfg["state"] == "DONE"
    assert "delayed confirm" in cfg["end_reason"]
    assert rec.text_calls == 1            # 未重注（上轮已成功，只是渲染晚）
    assert rec.enter_calls == 1


def test_entry_compact_blob_no_false_done(monkeypatch, tmp_path):
    """入口屏幕已有折叠提交的 compact blob 消息行（paste_collapse 全链路：
    compact_cmd 同样被折叠提交）→ round1 轮前复查不得把它误判为唤醒词已
    送达（假 DONE，e2e 实战抓出）——须走正常折叠展开路径投递。"""
    screen = FakeScreen(paste_collapse="expand")
    screen.messages.append("❯ [Pasted text #1]")   # compact blob 已提交
    ok, cfg, rec = run_deliver(monkeypatch, tmp_path, screen)
    assert ok and cfg["state"] == "DONE"
    assert rec.text_calls == 2            # 走折叠展开路径，不是复查短路
    assert "delayed confirm" not in cfg["end_reason"]


# ---- sent_signal 判据直测（不跑完整投递流程）----

def test_sent_signal_ignores_compact_residual():
    """v0.2.2 事故形态直测：残留 ❯ /compact 行内嵌唤醒词全文（含 head），
    且 tail 不在输入区——裸 head 检测必误判，/compact 前缀排除必须挡住。"""
    frame = frame_of(chat_lines=[f"❯ /compact 保留任务状态（唤醒词：{WORD}）"])
    assert not watcher.sent_signal(frame, HEAD, TAIL)


def test_sent_signal_accepts_new_message():
    frame = frame_of(chat_lines=["  ⎿ Compacted (ctrl+o", f"❯ {WORD}"])
    assert watcher.sent_signal(frame, HEAD, TAIL)


def test_sent_signal_blocked_by_tail_in_input():
    """折行守卫：文本仍在输入框（尚未提交）时，即使消息流有含 head 的
    ❯ 行（输入框首行溢出/残留）也绝不判成功。"""
    frame = frame_of(input_text=WORD, chat_lines=[f"❯ {WORD}"])
    assert not watcher.sent_signal(frame, HEAD, TAIL)


def test_in_input_single_line_box():
    """短唤醒词不折行（实测 175 显示宽在 179 宽 pane 单行）：❯+nbsp 与
    tail 同行仍算在框；提交后的消息行（❯+普通空格）不算。"""
    frame = frame_of(input_text="[短] 测试 tail")            # 单行输入框
    assert watcher.in_input(frame, "试 tail")
    frame2 = frame_of(input_text="[短] 测试 tail")           # nbsp 前缀命中
    assert watcher.in_input(frame2, "[短]")


def test_collapsed_sent_forms():
    """collapsed_sent 三形态：预览态（提示在屏）不算提交；提交态（提示消失 +
    编号递增的 blob 消息行）算；历史 blob（编号 ≤ 基线）不算。"""
    preview = frame_of_collapsed(blob_seq=7)
    assert not watcher.collapsed_sent(preview, 6)      # 预览态：提示在屏
    submitted = frame_of(chat_lines=["❯ [Pasted text #7]"])
    assert watcher.collapsed_sent(submitted, 6)        # 新编号 blob 消息行
    assert not watcher.collapsed_sent(submitted, 7)    # 历史 blob：编号不递增


def test_deliver_state_collapsed():
    """折叠态分流：明文不上屏但提示在屏 → 'collapsed'（tail 判据永假，
    必须走专门路径）。"""
    assert watcher.deliver_state(frame_of_collapsed(blob_seq=1), TAIL) == "collapsed"
    assert watcher.deliver_state(frame_of(input_text=WORD), TAIL) == "text"
    assert watcher.deliver_state(frame_of(), TAIL) is None


def test_window_shift_message_row_not_in_input():
    """窗口错位（消息流内容少时——压缩刚完成只有摘要几行——新消息落进底部
    N 行窗口）：消息行含 tail 但前缀是 ❯+普通空格 → 不算在框 → sent 判据
    不被拦截（e2e 实战教训：按区域+裸子串判定 → FAILED+三轮重注双发）。"""
    # 手动构造 5 行帧：❯ 消息行恰在倒数第 4 行（输入区窗口内）
    frame = "\n".join(["  ⎿ Compacted (ctrl+o", f"❯ {WORD}", "│ border │", "─ status ─", "  mode"])
    assert not watcher.in_input(frame, TAIL)
    assert watcher.sent_signal(frame, HEAD, TAIL)
