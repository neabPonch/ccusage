# ccusage

Terminal dashboard for Claude Code usage — session tokens, context window %, and live plan limit tracking.

## What it shows

- **Live plan limits** — 5-hour rolling window and 7-day window (% used, time to reset), fetched from claude.ai
- **Context window** — % of the 200K token window used in the current session
- **Current session** — turns, model, per-turn token breakdown, API-equivalent cost
- **Daily totals** — across all sessions, with historical breakdown

```
══════════════════════════════════════════════════════════════
  Claude Code Usage Tracker   [10:32:52]
══════════════════════════════════════════════════════════════

  PLAN LIMITS (live)
  5-hour window
  [████████████████████████░░░░] 85%  resets in 1h 47m
  7-day window
  [██░░░░░░░░░░░░░░░░░░░░░░░░░░] 9%  resets in 98h 27m

  ⚠  5-hour window at 85% — resets in 1h 47m

  CURRENT SESSION  (131m, 376 turns)
  Context window (200K)
  [████████░░░░░░░░░░░░░░░░░░░░] 32.0%  (136.0k remaining)
  ...
```

## Install

```bash
# 1. Clone and install dependency
git clone https://github.com/neabPonch/ccusage
cd ccusage
pip3 install cloudscraper

# 2. Link to PATH
ln -sf "$PWD/ccusage.py" ~/.local/bin/ccusage
chmod +x ccusage.py
```

## Usage

```bash
ccusage              # one-shot snapshot
ccusage -w           # watch mode, refreshes every 30s
ccusage -w -i 10     # watch mode, every 10s
ccusage --all        # full usage history by day
ccusage --no-live    # skip the claude.ai API fetch (offline mode)
```

## How it works

**Local token data** is read from `~/.claude/projects/**/*.jsonl` — Claude Code logs every API turn with full token counts locally.

**Live plan limits** are fetched from `https://claude.ai/api/organizations/{org}/usage` using your Firefox session cookies (`~/Library/Application Support/Firefox/Profiles/*/cookies.sqlite`). Requires `cloudscraper` to handle Cloudflare.

> Safari users: cookies aren't readable without Full Disk Access. Firefox is required for live plan data.

## Requirements

- Python 3.9+
- `cloudscraper` (`pip3 install cloudscraper`) — for live plan limits
- Firefox with an active claude.ai session — for live plan limits
- Claude Code with local JSONL logging (default) — for token data
