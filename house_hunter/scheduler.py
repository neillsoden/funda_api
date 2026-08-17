"""Runs house_hunter.run at fixed clock times each day (schedule.times, in
schedule.timezone) - e.g. ["11:00", "21:00"] for a twice-daily check at
11am and 9pm local time. Falls back to a simple repeating interval
(schedule.frequency) if no times are configured. Also runs the independent
NL Apartments scan on its own fixed interval, in a separate thread since it
has nothing to do with the house-search schedule. Intended as the
container's long-running process.
"""

import threading
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from house_hunter.config import load_config
from house_hunter.run import run

_INTERVAL_SECONDS = {
    "hourly": 60 * 60,
    "twice_daily": 12 * 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
}

# 4x/day. Runs here (not triggered by webapp page views) so there's exactly
# one process ever scanning apartments - avoids two containers racing on
# the same cursor/scanned-listing state.
_APARTMENTS_INTERVAL_SECONDS = 6 * 60 * 60


def _seconds_until_next_time(times: list[str], tz_name: str) -> tuple[float, str]:
    """times: e.g. ["11:00", "21:00"]. Returns (seconds_to_wait, description)."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    candidates = []
    for time_str in times:
        hour, minute = (int(part) for part in time_str.strip().split(":"))
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        candidates.append(candidate)
    next_time = min(candidates)
    return (next_time - now).total_seconds(), next_time.strftime("%Y-%m-%d %H:%M %Z")


def _run_apartments_loop() -> None:
    from house_hunter.apartments import scan_until_target
    from house_hunter.state import record_run

    while True:
        try:
            new_matches = scan_until_target()
            record_run("apartments_scan", "ok", sent_count=new_matches, detail=f"{new_matches} new apartment matches")
            print(f"[apartments] scan complete, {new_matches} new matches")
        except Exception:
            print("[apartments] scan failed:")
            traceback.print_exc()
            record_run("apartments_scan", "error", detail=traceback.format_exc(limit=1))
        print(f"[apartments] sleeping {_APARTMENTS_INTERVAL_SECONDS}s until next scan")
        time.sleep(_APARTMENTS_INTERVAL_SECONDS)


def main() -> None:
    threading.Thread(target=_run_apartments_loop, daemon=True).start()

    first_run = True
    while True:
        try:
            run(reason="startup" if first_run else "scheduled")
        except Exception:
            print("house_hunter run failed:")
            traceback.print_exc()
        first_run = False

        schedule = load_config()["schedule"]
        times = [t for t in (schedule.get("times") or []) if t.strip()]
        if times:
            tz_name = schedule.get("timezone") or "Europe/Amsterdam"
            seconds, next_at = _seconds_until_next_time(times, tz_name)
            print(f"Sleeping {seconds:.0f}s until next run at {next_at}")
        else:
            frequency = schedule.get("frequency", "daily")
            seconds = _INTERVAL_SECONDS.get(frequency, _INTERVAL_SECONDS["daily"])
            print(f"Sleeping {seconds}s until next run (frequency={frequency})")

        time.sleep(seconds)


if __name__ == "__main__":
    main()
