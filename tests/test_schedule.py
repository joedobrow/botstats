"""Checks of a division's league night (db.division_schedule):
python3 tests/test_schedule.py
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok and detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    s = db.division_schedule({"play_day": 2, "play_time": "20:00", "play_tz": "America/New_York"})
    check("reads as a weekly night", s["label"] == "Wednesdays · 8 PM ET", s["label"])
    nxt = datetime.fromtimestamp(s["next_at"], ZoneInfo("America/New_York"))
    check("the next one is a Wednesday at 8, local time",
          (nxt.weekday(), nxt.hour, nxt.minute) == (2, 20, 0), str(nxt))
    check("and it's ahead, within a week",
          0 < s["next_at"] - datetime.now().timestamp() <= 7 * 86400)
    s = db.division_schedule({"play_day": 6, "play_time": "19:30", "play_tz": "Europe/Berlin"})
    check("minutes and other zones", s["label"] == "Sundays · 7:30 PM CET", s["label"])
    check("not set is None", db.division_schedule({"play_day": None}) is None)
    check("an unknown zone is None",
          db.division_schedule({"play_day": 1, "play_time": "20:00", "play_tz": "Mars/Base"}) is None)

    print()
    if failures:
        print(f"{len(failures)} FAILED: " + ", ".join(failures))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
