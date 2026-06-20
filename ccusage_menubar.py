#!/opt/homebrew/bin/python3.11
"""
ccusage_menubar — Claude Code usage in your macOS menu bar

Shows: 5h: 85% 🟡  in the menu bar
Click: expands to show all windows + context usage
"""

import rumps
import threading
import subprocess
import sys
import os
import fcntl
from pathlib import Path
from datetime import datetime, timezone

# ── Single-instance guard ─────────────────────────────────────────────────────
_LOCK_FILE = Path("/tmp/ccusage_menubar.lock")
_lock_fh = open(_LOCK_FILE, "w")
try:
    fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(0)  # another instance is already running

# Add repo dir to path so we can import ccusage helpers
sys.path.insert(0, str(Path(__file__).parent))
from ccusage import build_snapshot, write_snapshot, CONTEXT_LIMIT

REFRESH_SECS = 60  # how often to poll


def pct_emoji(pct):
    if pct is None:   return "⚪"
    if pct >= 95:     return "🔴"
    if pct >= 80:     return "🟡"
    return "🟢"


def mini_bar(pct, width=10):
    if pct is None:
        return "─" * width
    filled = int(width * min(pct, 100) / 100)
    return "▓" * filled + "░" * (width - filled)


def fmt_resets(iso_str):
    try:
        resets = datetime.fromisoformat(iso_str)
        secs   = max(0, int((resets - datetime.now(timezone.utc)).total_seconds()))
        h, m   = secs // 3600, (secs % 3600) // 60
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return "?"


class CcusageApp(rumps.App):
    def __init__(self):
        super().__init__("ccusage", quit_button=None)
        self._pending = {}   # data written by background thread
        self._lock = threading.Lock()
        self._fetching = False

        # Kick off first fetch
        self._start_fetch()

    # ── Background fetch ──────────────────────────────────────────────────────

    def _start_fetch(self):
        if self._fetching:
            return
        self._fetching = True
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def _fetch_worker(self):
        # Build the snapshot once and persist it for agents/orchestrators; the UI
        # below is derived from the same data so the plan fetch happens only once.
        #
        # Everything runs inside try/finally so that ANY failure (network blip,
        # Cloudflare challenge, expired cookie, locked cookies.sqlite, JSON error)
        # still resets `_fetching`. Otherwise the flag stays True forever and
        # `_start_fetch`'s guard silently kills all future polling — the process
        # stays alive but the menu freezes on stale data.
        try:
            snap = build_snapshot(include_plan=True)
            try:
                write_snapshot(snap)
            except Exception:
                pass  # UI must keep working even if the state file can't be written

            sess_ctxs = [
                {"name": s["name"], "status": s["status"], "ctx_pct": s["context"]["pct"]}
                for s in snap["sessions"] if s.get("context")
            ]

            with self._lock:
                self._pending = {
                    "plan": snap["plan"],
                    "sess_ctxs": sess_ctxs,
                    "generated_at": snap.get("generated_at"),
                    "ready": True,
                }
        except Exception as e:
            print(f"[ccusage] fetch failed: {e}", file=sys.stderr)
        finally:
            self._fetching = False

    # ── Timer: runs on main thread, applies pending data ─────────────────────

    @rumps.timer(5)   # check every 5s; only redraws when new data arrives
    def _tick(self, _):
        with self._lock:
            if not self._pending.get("ready"):
                return
            data = dict(self._pending)
            self._pending = {}

        self._apply(data)

    @rumps.timer(REFRESH_SECS)
    def _auto_refresh(self, _):
        self._start_fetch()

    # ── Apply data to UI (main thread) ────────────────────────────────────────

    def _apply(self, data):
        plan      = data.get("plan") or {}
        sess_ctxs = data.get("sess_ctxs") or []

        fh = plan.get("five_hour") or {}
        sd = plan.get("seven_day") or {}
        fh_pct = fh.get("utilization")
        sd_pct = sd.get("utilization")

        # How old is this data? If polling ever stalls, surface it instead of
        # letting a frozen number masquerade as a live reading.
        age_secs = None
        try:
            gen = datetime.fromisoformat(data["generated_at"])
            age_secs = (datetime.now(timezone.utc) - gen).total_seconds()
        except Exception:
            pass
        stale = age_secs is not None and age_secs > 3 * REFRESH_SECS

        # Menu bar title — show 5h% and active session count
        n_active = sum(1 for s in sess_ctxs if s["status"] == "busy")
        sess_tag = f"  {n_active}●" if n_active else ""
        stale_tag = "⚠ " if stale else ""
        if fh_pct is not None:
            self.title = f"{stale_tag}5h:{fh_pct:.0f}%{sess_tag} {pct_emoji(fh_pct)}"
        else:
            self.title = f"{stale_tag}ccusage{sess_tag} ⚪"

        # Rebuild menu
        self.menu.clear()

        if fh_pct is not None:
            resets = fmt_resets(fh["resets_at"]) if fh.get("resets_at") else ""
            self.menu.add(rumps.MenuItem(
                f"5-hour  {mini_bar(fh_pct)}  {fh_pct:.0f}%  ↺ {resets}"
            ))
        if sd_pct is not None:
            self.menu.add(rumps.MenuItem(
                f"7-day   {mini_bar(sd_pct)}  {sd_pct:.0f}%"
            ))

        if sess_ctxs:
            self.menu.add(None)
            for s in sess_ctxs:
                dot  = "●" if s["status"] == "busy" else "○"
                name = s["name"].split("/")[-1] or s["name"]  # last path component
                left_k = (CONTEXT_LIMIT - int(s["ctx_pct"] / 100 * CONTEXT_LIMIT)) // 1000
                self.menu.add(rumps.MenuItem(
                    f"{dot} {name}  {mini_bar(s['ctx_pct'])}  {s['ctx_pct']:.0f}%  ({left_k}k)"
                ))

        if fh_pct is None and not sess_ctxs:
            self.menu.add(rumps.MenuItem("No data — log into claude.ai in Firefox"))

        self.menu.add(None)
        if age_secs is not None:
            mins = int(age_secs // 60)
            when = "just now" if mins == 0 else f"{mins}m ago"
            self.menu.add(rumps.MenuItem(f"{'⚠ stale — ' if stale else ''}updated {when}"))
        self.menu.add(rumps.MenuItem("Open full dashboard", callback=self.open_dashboard))
        self.menu.add(rumps.MenuItem("Refresh now",         callback=self.refresh_now))
        self.menu.add(None)
        self.menu.add(rumps.MenuItem("Quit ccusage",        callback=rumps.quit_application))

    # ── Actions ───────────────────────────────────────────────────────────────

    def open_dashboard(self, _):
        script = Path(__file__).parent / "ccusage.py"
        subprocess.Popen([
            "osascript", "-e",
            f'tell application "Terminal" to do script '
            f'"/opt/homebrew/bin/python3.11 {script} --all; echo; read -rsp \'Press enter to close…\' _"'
        ])

    def refresh_now(self, _):
        self._start_fetch()


if __name__ == "__main__":
    CcusageApp().run()
