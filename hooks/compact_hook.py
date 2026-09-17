#!/usr/bin/env python3
"""compactme 的 PreCompact/PostCompact hook —— 压缩生命周期的稳定信号源。

v0.2.x watcher 靠 tmux capture-pane 抓屏幕特征串判「压缩开始/完成」，四次
生产事故全是屏幕形态问题（折叠/折行/窗口/布局）。本 hook 由 Claude Code
在压缩生命周期确切口点触发（官方事件，matcher 空 = manual/auto 全收），
读 stdin JSON 写标记文件，watcher 轮询标记——不依赖任何屏幕形态。

标记文件：/tmp/compactme/hooks/<session_id>.pre.json / .post.json
内容：{"event", "ts", "transcript_path", "trigger"}

永不失败、永不阻塞主流程（异常静默吞掉——hook 报错会打扰用户会话）。
"""
import json
import sys
import time
from pathlib import Path

HOOK_ROOT = Path("/tmp/compactme/hooks")
SUFFIX = {"PreCompact": "pre", "PostCompact": "post"}


def main() -> None:
    data = json.load(sys.stdin)
    event = data.get("hook_event_name", "")
    suffix = SUFFIX.get(event)
    if not suffix:
        return
    session_id = data.get("session_id", "unknown")
    HOOK_ROOT.mkdir(parents=True, exist_ok=True)
    out = HOOK_ROOT / f"{session_id}.{suffix}.json"
    payload = {
        "event": event,
        "ts": time.time(),
        "transcript_path": data.get("transcript_path", ""),
        "trigger": data.get("trigger", ""),
    }
    out.write_text(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
