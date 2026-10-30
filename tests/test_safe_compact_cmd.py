#!/usr/bin/env python3
"""safe_compact_cmd 长度防御单测（v0.3.4，2026-10-30 两连挂事故）。

事故形态：Claude Code 2.1.312 TUI 把单块到达的超长输入（阈值实测在
426~860 字符间）折叠为 paste blob，blob 提交不再解析 slash 命令——
旧版 compact_cmd（内嵌唤醒词全文，860~1100+ 字符）注入被当普通消息吞，
PreCompact 不触发，90s×N 重试全挂。

不变量：
  1. 短命令（≤SAFE_CMD_MAX 且 /compact 前缀）原样通过
  2. 超长命令降级为纯 /compact（丢摘要侧重指引，保命令可执行）
  3. 非 /compact 前缀（脏数据/上游劣化）也归一为纯 /compact
  4. 降级结果必在安全界内（≤SAFE_CMD_MAX 且以 /compact 开头）
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "watcher", Path(__file__).parent.parent / "mcp" / "watcher.py"
)
watcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(watcher)


def test_short_cmd_passthrough():
    """短命令原样通过（含一句摘要侧重）。"""
    cmd = "/compact 保留任务状态、已完成步骤与下一步动作"
    assert watcher.safe_compact_cmd(cmd) == cmd


def test_pure_cmd_passthrough():
    assert watcher.safe_compact_cmd("/compact") == "/compact"


def test_oversize_degrades_to_pure():
    """超长（事故形态：内嵌唤醒词全文）降级纯 /compact。"""
    cmd = "/compact 保留线索：" + "长指令" * 300  # 1206 字符，远超 426 实测挂点
    out = watcher.safe_compact_cmd(cmd)
    assert out == "/compact"


def test_boundary_exact_max():
    """恰好 SAFE_CMD_MAX 的命令仍原样通过（边界含端）。"""
    pad = "a" * (watcher.SAFE_CMD_MAX - len("/compact "))
    assert watcher.safe_compact_cmd(f"/compact {pad}") == f"/compact {pad}"
    assert len(pad) > 0


def test_boundary_over_max():
    """超一个字符即降级（边界不含端外）。"""
    pad = "a" * (watcher.SAFE_CMD_MAX - len("/compact ") + 1)
    assert watcher.safe_compact_cmd(f"/compact {pad}") == "/compact"


def test_wrong_prefix_normalized():
    """前缀异常（非 /compact 开头）归一为纯 /compact。"""
    assert watcher.safe_compact_cmd("/status") == "/compact"
    assert watcher.safe_compact_cmd("") == "/compact"
    assert watcher.safe_compact_cmd("compact 保留状态") == "/compact"


def test_degraded_always_safe():
    """任意输入降级后必在安全形态（防御闭环）。"""
    for cmd in ["/compact", "/compact " + "x" * 9999, "garbage", ""]:
        out = watcher.safe_compact_cmd(cmd)
        assert out.startswith("/compact")
        assert len(out) <= watcher.SAFE_CMD_MAX
