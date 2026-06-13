# ccusage

Terminal + menu bar dashboard for Claude Code usage tracking.

## What this does

Combines two data sources:
- **Local** — `~/.claude/projects/**/*.jsonl` for per-turn token counts, context window %, session history
- **Live** — `claude.ai/api/organizations/{org}/usage` for 5-hour and 7-day plan utilization

## Key files

| File | Purpose |
|---|---|
| `ccusage.py` | Core library + CLI (`ccusage` command) |
| `ccusage_menubar.py` | macOS menu bar app (auto-started via LaunchAgent) |

## Running

```bash
ccusage                  # one-shot CLI snapshot
ccusage -w               # watch mode, refreshes every 30s
ccusage --all            # full history by day
```

Menu bar app:
```bash
# Start
launchctl load ~/Library/LaunchAgents/com.neabponch.ccusage.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.neabponch.ccusage.plist

# Logs
tail -f /tmp/ccusage.log
```

## Dependencies

- Python 3.11 (homebrew: `/opt/homebrew/bin/python3.11`)
- `cloudscraper` — Cloudflare bypass for claude.ai API
- `rumps` — macOS menu bar framework
- Firefox with active claude.ai session — live plan limit data

```bash
/opt/homebrew/bin/python3.11 -m pip install rumps cloudscraper
```

## How the live plan data works

`fetch_plan_usage()` in `ccusage.py`:
1. Reads Firefox cookies from `~/Library/Application Support/Firefox/Profiles/*/cookies.sqlite`
2. Uses `cloudscraper` (Firefox TLS fingerprint) to bypass Cloudflare on `claude.ai`
3. Fetches `/api/organizations/{org_uuid}/usage` which returns `five_hour` and `seven_day` utilization

If Firefox cookies expire, just log back into claude.ai in Firefox.

## Development workflow

**All changes must go through a PR — never commit directly to `main`.**

```bash
git checkout -b your-branch
# make changes
git push -u origin your-branch
gh pr create
```

Use `/pr` to create a properly formatted PR for the current branch.
