#!/usr/bin/env python3
"""Minimal Claude Code statusline: 🧠 context water level only.

Prints e.g. ``🧠 125.7k/200.0k 62.9%`` — the exact format compactme's
``context_stats()`` parses first (from the pane's statusline frame),
before falling back to transcript scanning. No dependencies beyond
Python 3 (the plugin already requires it).

Setup — ~/.claude/settings.json:

    {
      "statusLine": {
        "type": "command",
        "command": "python3 /abs/path/to/compactme/statusline/context_statusline.py"
      }
    }

Context window defaults to 200k; for a 1M-window model set the env:

    "command": "COMPACTME_CONTEXT_WINDOW=1000000 python3 /abs/.../context_statusline.py"
"""
import json
import os
import sys

CONTEXT_WINDOW = int(os.environ.get("COMPACTME_CONTEXT_WINDOW", "200000"))
TAIL_BYTES = 1 << 20  # last assistant usage lives near the transcript's tail


def fmt(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def context_tokens(transcript_path):
    """Last assistant usage in the transcript = current context size.

    Mirrors the token accounting Claude Code itself uses: input +
    cache-creation + cache-read. Reads only the tail so the statusline
    stays fast on multi-MB transcripts."""
    if not transcript_path or not os.path.exists(transcript_path):
        return 0
    size = os.path.getsize(transcript_path)
    with open(transcript_path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().splitlines()
    if size > TAIL_BYTES and lines:  # first line may be truncated mid-record
        lines = lines[1:]
    for raw in reversed(lines):
        if b'"type":"assistant"' not in raw:
            continue
        try:
            usage = (json.loads(raw).get("message") or {}).get("usage") or {}
        except ValueError:
            continue
        if usage:
            return (usage.get("input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0))
    return 0


def main():
    try:
        session = json.load(sys.stdin)
    except ValueError:
        session = {}
    used = context_tokens(session.get("transcript_path") or "")
    pct = used / CONTEXT_WINDOW * 100 if CONTEXT_WINDOW else 0.0
    print(f"🧠 {fmt(used)}/{fmt(CONTEXT_WINDOW)} {pct:.1f}%")


if __name__ == "__main__":
    main()
