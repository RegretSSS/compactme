# compactme

**Let the model compact itself — and wake itself back up.**

A Claude Code plugin (MCP server + hooks) that lets the model schedule its own
`/compact` and, after compaction finishes, receive a **wakeup word** carrying
task-reconstruction clues, injected back into the *same* session.

[中文文档 (Chinese README)](README.zh-CN.md)

## Why

Long sessions degrade — attention dilutes as context grows. `/compact` reclaims
the window, but a compacted context just sits idle: nothing tells the fresh
context to pick the task back up. compactme closes that loop:

1. The model calls the `compactme` tool as the **last tool call of its turn**
   and writes a closing summary for the user.
2. A detached watcher injects `/compact` into the pane's input box.
3. When hooks + transcript confirm compaction **finished**, the watcher injects
   a wakeup word the model composed earlier — packed with clues
   (*where to read: file paths / TaskList, what to read, what to do next*).
4. The compacted session resumes the task on its own.

A wakeup word without reconstruction clues is worthless, so the tool
**hard-rejects** wakeup words that carry no clue (no path, no TaskList marker,
…).

## Quick Start

**Prerequisites:** Linux or WSL · tmux · Python ≥ 3.10 · [fastmcp]

```bash
pip install --user fastmcp     # or use pipx/venv — just make sure this works:
python3 -c "import fastmcp"    # must exit 0 (the MCP server runs on `python3`)
```

```bash
# 1. Start tmux, then start Claude Code INSIDE it (not in a bare terminal)
tmux new -s claude
cd your-project/ && claude

# 2. Inside Claude Code:
#    /plugin marketplace add RegretSSS/compactme
#    /plugin install compactme@compactme
# 3. Restart Claude Code (or reload the plugin) so the
#    PreCompact/PostCompact hooks take effect.
```

Sanity check — ask Claude: *"call context_stats"*. It should report your
context water level. If it says `$TMUX_PANE` not found, Claude is not running
inside tmux.

Then just work as usual. When context gets heavy (≥ 400k tokens = hard
threshold, ≥ 200k at a natural breakpoint = soft), the model can compact
itself — you'll see it post a closing summary, run the tool, go quiet while
compaction runs, then resume from the wakeup word. You can also prompt it:
*"context is getting heavy — compact yourself and continue."*

Local development install:

```bash
git clone https://github.com/RegretSSS/compactme
claude plugin validate /path/to/compactme
# then in Claude Code: /plugin add /path/to/compactme
```

## The tmux & pane setup (read this if it doesn't work)

compactme's only injection surface is your pane's input box, via
`tmux send-keys`. Everything hinges on Claude Code running **inside a tmux
pane**:

```
tmux pane ($TMUX_PANE)
  └─ shell
      └─ claude  ← session process; /proc/<pid>/cwd = session dir
          └─ ~/.claude/projects/<session-dir-slug>/<newest>.jsonl
             = active transcript
```

- **Start Claude inside tmux.** `tmux new -s claude`, then run `claude` in the
  pane. A bare terminal, the desktop app, or a terminal multiplexer other than
  tmux gives the watcher no injection surface (the tool fails honestly with
  `$TMUX_PANE not found`).
- **`cd` into your project before launching `claude`.** The watcher locates
  the session by walking the pane's process tree for the `claude` process and
  reading its `/proc/<pid>/cwd` → `~/.claude/projects/<slug>/` → newest
  `.jsonl`. Changing your shell's directory *after* launch doesn't matter —
  the anchor is the claude process's cwd, not the shell's.
- **One pane, one session.** Tasks, watchers and injection targets are all
  isolated per pane, so parallel sessions in different panes/directories are
  fine. Two concurrent sessions *in the same cwd* compacting at the same time
  can cross-match hook markers (worst case: compaction confirmed slightly
  early — delivery verification is independent).
- **SSH is fine** as long as tmux and claude run on the same (remote) machine.
  Avoid nested tmux-in-tmux for the session pane.
- **`context_stats` bonus:** if your statusline shows `🧠 125.7k/1M 12.6%`, the
  tool parses it (naturally per-conversation); otherwise it falls back to the
  transcript's usage field.

Runtime artifacts live in `/tmp/compactme/<pane>/` (`task.json`,
`watcher.log`) — safe to delete anytime; nothing else touches your system.

## How it works

```
compactme() ──writes task.json (per pane)──▶ watcher (detached, one per pane)
    │ LOCATE      pane process tree → claude process → /proc/<pid>/cwd
    │             → projects subdir → mtime-newest .jsonl (active transcript;
    │               follows the file switch compaction causes — session_id is
    │               informational only, it CHANGES across compaction)
    │ WAIT_SETTLE give the model time to finish its closing summary
    │ INJECTING   clear box → /compact + Enter → wait for PreCompact hook
    │             marker (scan-matched by timestamp + transcript dir; 90s
    │             window covers busy-queued input; ≤2 natural retries)
    │ COMPACTING  wait for PostCompact marker; fallback = transcript
    │             isCompactSummary. Never re-injects /compact.
    │ DELIVERING  clear box → wakeup word + Enter (×3 backoff) → confirmed by
    │             a NEW transcript user entry containing the word head
    └ hooks: PreCompact/PostCompact → compact_hook.py writes marker files
      (millisecond-fast, never blocks the session)

hooks + transcript dual channel, no screen scraping — four production
incidents in v0.2.x were ALL screen-shape problems (paste folding, line
wrapping, window drift, dynamic layout). Transcripts are written 10s+ before
TUI rendering and record the full text of folded pastes: immune to every
screen mine.
```

## Tools

- `compactme(compact_instructions, wakeup_word, settle_seconds=6.0)` —
  dispatch the watcher. Must be the **last** tool call of the turn; the model
  then writes a closing summary and ends its turn. Refuses wakeup words
  without task-reconstruction clues.
- `context_stats()` — context water level: statusline `🧠` first, transcript
  usage as fallback.

## Limitations

- **Linux / WSL only** — the watcher reads `/proc/<pid>/environ` and
  `/proc/<pid>/cwd`; macOS is unsupported.
- **tmux only** — injection needs `$TMUX_PANE`; the desktop app and bare
  terminals have no injection surface.
- Hook markers require the plugin (and its `hooks/hooks.json`) to be loaded;
  if hooks aren't active yet, it degrades to the transcript
  `isCompactSummary` fallback — which goes blind when compaction switches
  transcript files (the hook channel covers that case).
- With multiple concurrent sessions, the `context_stats` transcript fallback
  may pick the wrong file (it takes the mtime-newest); prefer
  `CLAUDE_CODE_SESSION_ID` when present.
- Compaction still summarizes away detail — the wakeup word's clue quality
  is what bounds task-resumption quality. The tool enforces a minimum, not
  perfection.

## Tests

```bash
python3 -m pytest tests/test_deliver_wakeup.py -q   # 17 tests: happy/delayed/
                                                    # all-fail/regressions…
bash tests/e2e.sh                                   # 7-mode e2e against a
                                                    # fake TUI (normal/swallow/
                                                    # eat_enter/lost_text/
                                                    # paste_collapse/busy_defer/
                                                    # switch_sid)
```

## License

[MIT](LICENSE)

[fastmcp]: https://pypi.org/project/fastmcp/
