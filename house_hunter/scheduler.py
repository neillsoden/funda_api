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
    """Interval is read from config fresh each cycle (nl_apartments.
    scan_interval_minutes, editable on the Preferences page) rather than a
    fixed constant, so changing it in the webapp takes effect on the next
    scan without a redeploy."""
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

        minutes = load_config().get("nl_apartments", {}).get("scan_interval_minutes") or 360
        seconds = max(60, int(minutes) * 60)  # floor at 1 min - avoid a zero/garbage config value hammering Funda
        print(f"[apartments] sleeping {seconds}s until next scan")
        time.sleep(seconds)


def _run_rentals_loop() -> None:
    """Same pattern as _run_apartments_loop() - see that docstring. Reads
    nl_rentals.scan_interval_minutes."""
    from house_hunter.rentals import scan_until_target
    from house_hunter.state import record_run

    while True:
        try:
            new_matches = scan_until_target()
            record_run("rentals_scan", "ok", sent_count=new_matches, detail=f"{new_matches} new rental matches")
            print(f"[rentals] scan complete, {new_matches} new matches")
        except Exception:
            print("[rentals] scan failed:")
            traceback.print_exc()
            record_run("rentals_scan", "error", detail=traceback.format_exc(limit=1))

        minutes = load_config().get("nl_rentals", {}).get("scan_interval_minutes") or 360
        seconds = max(60, int(minutes) * 60)
        print(f"[rentals] sleeping {seconds}s until next scan")
        time.sleep(seconds)


def main() -> None:
    threading.Thread(target=_run_apartments_loop, daemon=True).start()
    threading.Thread(target=_run_rentals_loop, daemon=True).start()

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
