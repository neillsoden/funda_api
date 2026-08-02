"""Run house_hunter.run on a repeating interval, driven by config.json's
schedule.frequency. Intended as the container's long-running process.
"""

import time
import traceback

from house_hunter.config import load_config
from house_hunter.run import run

_INTERVAL_SECONDS = {
    "hourly": 60 * 60,
    "twice_daily": 12 * 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
}


def main() -> None:
    while True:
        try:
            run()
        except Exception:
            print("house_hunter run failed:")
            traceback.print_exc()

        frequency = load_config()["schedule"].get("frequency", "daily")
        interval = _INTERVAL_SECONDS.get(frequency, _INTERVAL_SECONDS["daily"])
        print(f"Sleeping {interval}s until next run (frequency={frequency})")
        time.sleep(interval)


if __name__ == "__main__":
    main()
