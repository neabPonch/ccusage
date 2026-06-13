#!/usr/bin/env python3
"""
ccusage — Claude Code usage tracker

Combines two data sources:
  1. ~/.claude/projects/**/*.jsonl  — local token counts per session turn
  2. claude.ai/api/organizations/{org}/usage — live 5-hour + 7-day plan limits

The live limits require your Firefox session cookies (reads from
~/Library/Application Support/Firefox/Profiles/*/cookies.sqlite).
"""

import json
import os
import sys
import time
import argparse
import shutil
import sqlite3
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime, date, timezone, timedelta
from collections import defaultdict

PROJECTS_DIR  = Path.home() / ".claude" / "projects"
SESSIONS_DIR  = Path.home() / ".claude" / "sessions"
STATE_DIR     = Path.home() / ".claude" / "cctracker"
STATE_FILE    = STATE_DIR / "state.json"   # snapshot written by the menubar poller
CONTEXT_LIMIT = 200_000  # default context window (kept for back-compat imports)

# Per-model context windows, matched by substring on the model id. Most current
# Claude models (Opus/Sonnet/Haiku 4.x) are 200k, so the default is correct for
# them — this map exists so 1M-context betas or future models can override
# without touching call sites.
MODEL_CONTEXT_LIMITS = {
    # "claude-...-1m": 1_000_000,
}


def context_limit(model):
    """Context window size for a model id, defaulting to CONTEXT_LIMIT."""
    if model:
        for key, lim in MODEL_CONTEXT_LIMITS.items():
            if key in model:
                return lim
    return CONTEXT_LIMIT

# Sonnet 4.x API pricing (per million tokens) — informational only for sub users
PRICING = {
    "input":          3.00,
    "cache_creation": 3.75,
    "cache_read":     0.30,
    "output":        15.00,
}


# ── Live plan usage (claude.ai API) ──────────────────────────────────────────

def _firefox_cookies():
    """Return dict of claude.ai cookies from Firefox's SQLite store."""
    profile_root = Path.home() / "Library/Application Support/Firefox/Profiles"
    if not profile_root.exists():
        return {}
    cookies = {}
    for profile in sorted(profile_root.iterdir()):
        db = profile / "cookies.sqlite"
        if not db.exists():
            continue
        tmp = tempfile.mktemp(suffix=".sqlite")
        try:
            shutil.copy2(str(db), tmp)
            conn = sqlite3.connect(tmp)
            rows = conn.execute(
                "SELECT name, value FROM moz_cookies WHERE host LIKE '%claude.ai%'"
            ).fetchall()
            conn.close()
            cookies.update({name: value for name, value in rows})
        except Exception:
            pass
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        if cookies:
            break
    return cookies


def fetch_plan_usage():
    """
    Return dict with 'five_hour' and 'seven_day' utilization from claude.ai,
    or None if unavailable.

    Structure per window:
      {"utilization": 81.0, "resets_at": "2026-06-13T16:20:00+00:00"}
    """
    try:
        import cloudscraper
    except ImportError:
        return None

    cookies = _firefox_cookies()
    if not cookies:
        return None

    try:
        scraper = cloudscraper.create_scraper(
            browser={"browser": "firefox", "platform": "darwin", "mobile": False}
        )
        scraper.cookies.update(cookies)
        hdrs = {"Accept": "application/json", "Referer": "https://claude.ai/settings/usage"}

        orgs = scraper.get("https://claude.ai/api/organizations", headers=hdrs, timeout=8).json()
        if not orgs:
            return None
        org_uuid = orgs[0]["uuid"]

        data = scraper.get(
            f"https://claude.ai/api/organizations/{org_uuid}/usage",
            headers=hdrs,
            timeout=8,
        ).json()
        return data
    except Exception:
        return None


# ── Local JSONL helpers ───────────────────────────────────────────────────────

def cost_usd(u):
    return (
        u.get("input_tokens", 0)               / 1e6 * PRICING["input"] +
        u.get("cache_creation_input_tokens", 0) / 1e6 * PRICING["cache_creation"] +
        u.get("cache_read_input_tokens", 0)     / 1e6 * PRICING["cache_read"] +
        u.get("output_tokens", 0)               / 1e6 * PRICING["output"]
    )


def load_sessions(since_date=None):
    rows = []
    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            with open(jsonl, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") != "assistant":
                        continue
                    msg = d.get("message", {})
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    ts_str = d.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if since_date and ts.date() < since_date:
                        continue
                    rows.append({
                        "session_id": d.get("sessionId", "unknown"),
                        "project":    jsonl.parent.name,
                        "ts":         ts,
                        "usage":      usage,
                        "model":      msg.get("model", "unknown"),
                    })
        except (OSError, PermissionError):
            continue
    return rows


def sum_usage(rows):
    totals = defaultdict(int)
    for r in rows:
        for k, v in r["usage"].items():
            if isinstance(v, (int, float)):
                totals[k] += v
    return dict(totals)


def active_sessions():
    """Return list of dicts for every open Claude Code session, newest-first."""
    sessions = []
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text())
            if d.get("status") in ("idle", "running", "busy"):
                cwd = d.get("cwd", "")
                sessions.append({
                    "session_id": d.get("sessionId", ""),
                    "status":     d.get("status", "idle"),
                    "cwd":        cwd,
                    "name":       d.get("name") or cwd.replace("/Users/neabponch", "~"),
                    "updated_at": d.get("updatedAt", 0),
                })
        except Exception:
            pass
    return sorted(sessions, key=lambda s: s["updated_at"], reverse=True)


def _session_jsonl(session_id):
    """Locate the JSONL transcript for a session id (exact name, then substring)."""
    for jsonl in PROJECTS_DIR.rglob(f"{session_id}.jsonl"):
        return jsonl
    for jsonl in PROJECTS_DIR.rglob("*.jsonl"):
        if session_id in jsonl.name:
            return jsonl
    return None


def _tail_lines(path, max_bytes):
    """Return decoded lines from the last `max_bytes` of a file (drops the
    possibly-partial first line when the read didn't start at byte 0)."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - max_bytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]
    return lines


def _last_assistant_usage(session_id, max_bytes=256 * 1024):
    """Return (usage_dict, model) for a session's most recent assistant turn.

    Reads only the file tail — the hot path for gating decisions. Falls back to
    a full scan if the tail window happens to contain no assistant usage (e.g. a
    very large trailing user message), so correctness is never worse than before.
    """
    path = _session_jsonl(session_id)
    if not path:
        return None

    def scan(lines):
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get("type") == "assistant":
                msg = d.get("message", {})
                u = msg.get("usage")
                if u:
                    return u, msg.get("model")
        return None

    hit = scan(_tail_lines(path, max_bytes))
    if hit is None:
        try:
            with open(path, encoding="utf-8") as f:
                hit = scan(f.readlines())
        except OSError:
            return None
    return hit


def last_turn_context(session_id):
    """Raw usage dict for a session's last assistant turn (back-compat shape)."""
    hit = _last_assistant_usage(session_id)
    return hit[0] if hit else None


def session_context(session_id):
    """Context-window state for a session, computed from the last assistant turn.

    Returns {used, pct, left, limit, model} or None if no usage is found.
    `used` counts input + cache_creation + cache_read (the tokens occupying the
    window); output tokens are excluded as they don't persist in context.
    """
    hit = _last_assistant_usage(session_id)
    if not hit:
        return None
    u, model = hit
    used = (u.get("input_tokens", 0) +
            u.get("cache_creation_input_tokens", 0) +
            u.get("cache_read_input_tokens", 0))
    limit = context_limit(model)
    return {
        "used":  used,
        "pct":   used / limit * 100,
        "left":  limit - used,
        "limit": limit,
        "model": model,
    }


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def fmt_cost(usd):
    return "<$0.01" if usd < 0.01 else f"${usd:.3f}"


def fmt_resets(iso_str):
    """Return human-friendly time until reset, e.g. '2h 14m'."""
    try:
        resets = datetime.fromisoformat(iso_str)
        delta  = resets - datetime.now(timezone.utc)
        secs   = max(0, int(delta.total_seconds()))
        h, m   = secs // 3600, (secs % 3600) // 60
        if h:
            return f"{h}h {m}m"
        return f"{m}m"
    except Exception:
        return "?"


def bar(pct, width=28, warn_at=80, crit_at=95):
    filled = int(width * min(pct, 100) / 100)
    char = "█"
    return char * filled + "░" * (width - filled)


def pct_color(pct):
    """Return ANSI color prefix for a percentage value."""
    if pct >= 95:
        return "\033[91m"   # red
    if pct >= 80:
        return "\033[93m"   # yellow
    return "\033[92m"       # green


RESET = "\033[0m"


# ── Render ────────────────────────────────────────────────────────────────────

def render(args):
    today = date.today()
    since = today if not args.all else None
    rows  = load_sessions(since_date=since)

    # Deduplicate
    seen, deduped = set(), []
    for r in rows:
        key = (r["session_id"], r["ts"].isoformat())
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    rows = deduped

    open_sessions = active_sessions()

    by_session = defaultdict(list)
    by_day     = defaultdict(list)
    for r in rows:
        by_session[r["session_id"]].append(r)
        by_day[r["ts"].date()].append(r)

    W = 62
    now_str = datetime.now().strftime("%H:%M:%S")
    print(f"\n{'═' * W}")
    print(f"  Claude Code Usage Tracker   [{now_str}]")
    print(f"{'═' * W}")

    # ── Plan limits (live) ─────────────────────────────────────────────────
    if not args.no_live:
        plan = fetch_plan_usage()
    else:
        plan = None

    if plan:
        fh = plan.get("five_hour") or {}
        sd = plan.get("seven_day") or {}
        fh_pct = fh.get("utilization", 0)
        sd_pct = sd.get("utilization", 0)

        print(f"\n  PLAN LIMITS (live)")

        for label, pct, window in [("5-hour window", fh_pct, fh), ("7-day window", sd_pct, sd)]:
            resets_str = f"  resets in {fmt_resets(window['resets_at'])}" if window.get("resets_at") else ""
            b = bar(pct)
            color = pct_color(pct)
            print(f"  {label}")
            print(f"  [{color}{b}{RESET}] {color}{pct:.0f}%{RESET}{resets_str}")

        if fh_pct >= 80:
            resets_in = fmt_resets(fh.get("resets_at", ""))
            print(f"\n  ⚠  5-hour window at {fh_pct:.0f}% — resets in {resets_in}")
    else:
        print(f"\n  PLAN LIMITS  (run: pip3 install cloudscraper  to enable live data)")

    # ── Active sessions ────────────────────────────────────────────────────
    if not open_sessions:
        print(f"\n  No active sessions detected")
    else:
        for i, sess in enumerate(open_sessions):
            sid    = sess["session_id"]
            status = sess["status"]
            title  = sess["name"]
            tag    = "● ACTIVE" if status == "busy" else "○ IDLE"

            print(f"\n  {tag}  {title}")

            sess_rows = by_session.get(sid, [])
            if not sess_rows:
                print(f"  (no turns recorded today)")
                continue

            u     = sum_usage(sess_rows)
            inp   = u.get("input_tokens", 0)
            cc    = u.get("cache_creation_input_tokens", 0)
            cr    = u.get("cache_read_input_tokens", 0)
            out   = u.get("output_tokens", 0)
            total = inp + cc + cr + out
            model = sess_rows[-1]["model"] if sess_rows else "?"
            start = min(r["ts"] for r in sess_rows)
            mins  = int((datetime.now(timezone.utc) - start).total_seconds() // 60)

            ctx = session_context(sid)
            if ctx:
                ctx_used = ctx["used"]
                ctx_pct  = ctx["pct"]
                ctx_left = ctx["left"]
            else:
                ctx_used = ctx_pct = ctx_left = None

            print(f"  Model: {model}  •  {len(sess_rows)} turns  •  {mins}m elapsed")

            if ctx_used is not None:
                color = pct_color(ctx_pct)
                b = bar(ctx_pct)
                print(f"  Context  [{color}{b}{RESET}] {color}{ctx_pct:.1f}%{RESET}  ({fmt_tokens(ctx_left)} left)")

            print(f"  ┌───────────────────────────────────────────┐")
            print(f"  │  Input (fresh)  : {fmt_tokens(inp):>10}             │")
            print(f"  │  Cache created  : {fmt_tokens(cc):>10}             │")
            print(f"  │  Cache read     : {fmt_tokens(cr):>10}             │")
            print(f"  │  Output         : {fmt_tokens(out):>10}             │")
            print(f"  │  Total          : {fmt_tokens(total):>10}             │")
            print(f"  │  API-equiv cost : {fmt_cost(cost_usd(u)):>10}             │")
            print(f"  └───────────────────────────────────────────┘")

    # ── Today's totals ─────────────────────────────────────────────────────
    today_rows = by_day.get(today, [])
    if today_rows:
        u     = sum_usage(today_rows)
        inp   = u.get("input_tokens", 0)
        cc    = u.get("cache_creation_input_tokens", 0)
        cr    = u.get("cache_read_input_tokens", 0)
        out   = u.get("output_tokens", 0)
        total = inp + cc + cr + out
        nsess = len(set(r["session_id"] for r in today_rows))
        print(f"\n  TODAY ({nsess} session{'s' if nsess != 1 else ''})")
        print(f"  {fmt_tokens(inp)} input + {fmt_tokens(cc)}/{fmt_tokens(cr)} cache + {fmt_tokens(out)} output")
        print(f"  Total: {fmt_tokens(total)} tokens  |  API-equiv: {fmt_cost(cost_usd(u))}")

    # ── Daily breakdown ────────────────────────────────────────────────────
    if args.all or args.days > 1:
        sorted_days = sorted(by_day.keys(), reverse=True)[:args.days]
        print(f"\n  DAILY BREAKDOWN")
        print(f"  {'Date':<12} {'Sess':>5} {'Output':>10} {'Total':>12} {'API cost':>10}")
        print(f"  {'-'*52}")
        for d in sorted_days:
            dr    = by_day[d]
            u     = sum_usage(dr)
            out   = u.get("output_tokens", 0)
            total = (u.get("input_tokens", 0) +
                     u.get("cache_creation_input_tokens", 0) +
                     u.get("cache_read_input_tokens", 0) +
                     u.get("output_tokens", 0))
            nsess = len(set(r["session_id"] for r in dr))
            mark  = " ◀" if d == today else ""
            print(f"  {str(d):<12} {nsess:>5} {fmt_tokens(out):>10} {fmt_tokens(total):>12} "
                  f"{fmt_cost(cost_usd(u)):>10}{mark}")

    print(f"\n{'═' * W}\n")


def build_snapshot(session_id=None, include_plan=True):
    """Machine-readable state for agents/orchestrators.

    `plan` is the slow/fragile network signal (None when --no-live or
    unavailable); `sessions` and `session` are cheap local tail reads.
    """
    snap = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "context_limit": CONTEXT_LIMIT,
        "plan": fetch_plan_usage() if include_plan else None,
        "sessions": [
            {**sess, "context": session_context(sess["session_id"])}
            for sess in active_sessions()
        ],
    }
    if session_id:
        snap["session"] = session_context(session_id)
    return snap


def write_snapshot(snap=None, path=STATE_FILE):
    """Atomically write a snapshot to `path` (temp file + rename).

    Intended to be called on the menubar poll loop so the slow/fragile plan
    fetch happens in one place; agents read this file instead of each scanning
    transcripts or hitting claude.ai. Returns the snapshot written.
    """
    if snap is None:
        snap = build_snapshot()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f, default=str)
        os.replace(tmp, path)   # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return snap


def read_snapshot(path=STATE_FILE):
    """Read the last snapshot written by the poller, or None if unavailable.

    Callers should check `generated_at` for staleness (poller dead/slow).
    """
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Claude Code usage tracker")
    parser.add_argument("-w", "--watch",    action="store_true", help="Refresh continuously")
    parser.add_argument("-i", "--interval", type=int, default=30, help="Watch interval seconds (default 30)")
    parser.add_argument("-d", "--days",     type=int, default=7,  help="Days of history (default 7)")
    parser.add_argument("-a", "--all",      action="store_true",  help="Show all-time history")
    parser.add_argument("--no-live",        action="store_true",  help="Skip live plan usage fetch")
    parser.add_argument("--json",           action="store_true",  help="Emit machine-readable JSON snapshot and exit")
    parser.add_argument("--session",        metavar="ID",         help="With --json, include context state for this session id")
    parser.add_argument("--write",          action="store_true",  help=f"Write snapshot to {STATE_FILE} and exit")
    args = parser.parse_args()

    if args.json:
        snap = build_snapshot(session_id=args.session, include_plan=not args.no_live)
        print(json.dumps(snap, default=str))
        return

    if args.write:
        snap = write_snapshot(build_snapshot(session_id=args.session, include_plan=not args.no_live))
        print(f"wrote {STATE_FILE}  ({len(snap['sessions'])} sessions, "
              f"plan={'yes' if snap['plan'] else 'no'})")
        return

    if args.watch:
        try:
            while True:
                os.system("clear")
                render(args)
                print(f"  Refreshing every {args.interval}s — Ctrl+C to stop\n")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        render(args)


if __name__ == "__main__":
    main()
