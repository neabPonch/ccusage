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
from pathlib import Path
from datetime import datetime, timezone

# Add repo dir to path so we can import ccusage helpers
sys.path.insert(0, str(Path(__file__).parent))
from ccusage import fetch_plan_usage, current_session_id, last_turn_context, CONTEXT_LIMIT

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
        plan = fetch_plan_usage()

        ctx_pct = None
        sid = current_session_id()
        if sid:
            last_u = last_turn_context(sid)
            if last_u:
                ctx_used = (last_u.get("input_tokens", 0) +
                            last_u.get("cache_creation_input_tokens", 0) +
                            last_u.get("cache_read_input_tokens", 0))
                ctx_pct = ctx_used / CONTEXT_LIMIT * 100

        with self._lock:
            self._pending = {"plan": plan, "ctx_pct": ctx_pct, "ready": True}
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
        plan    = data.get("plan") or {}
        ctx_pct = data.get("ctx_pct")

        fh = plan.get("five_hour") or {}
        sd = plan.get("seven_day") or {}
        fh_pct = fh.get("utilization")
        sd_pct = sd.get("utilization")

        # Menu bar title
        if fh_pct is not None:
            self.title = f"5h:{fh_pct:.0f}% {pct_emoji(fh_pct)}"
        else:
            self.title = "ccusage ⚪"

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
        if ctx_pct is not None:
            left_k = (CONTEXT_LIMIT - int(ctx_pct / 100 * CONTEXT_LIMIT)) // 1000
            self.menu.add(rumps.MenuItem(
                f"Context {mini_bar(ctx_pct)}  {ctx_pct:.0f}%  ({left_k}k left)"
            ))

        if fh_pct is None and sd_pct is None:
            self.menu.add(rumps.MenuItem("No data — log into claude.ai in Firefox"))

        self.menu.add(None)
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
            f'"python3 {script} --all; echo; read -rsp \'Press enter to close…\' _"'
        ])

    def refresh_now(self, _):
        self._start_fetch()


if __name__ == "__main__":
    CcusageApp().run()
