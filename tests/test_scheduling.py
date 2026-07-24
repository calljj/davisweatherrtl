from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from davis_clientraw.__main__ import _seconds_until_time_of_day


def test_target_later_today():
    # Fixed "now" via monkeypatching would need extra plumbing; instead
    # derive the expected answer the same way the function does, using a
    # target guaranteed to be in the future within the same day.
    tz = ZoneInfo("Europe/London")
    now = datetime.now(tz)
    later = (now.hour + 1) % 24
    wait_sec = _seconds_until_time_of_day(f"{later:02d}:00", "Europe/London")
    assert 0 < wait_sec <= 25 * 3600


def test_target_in_past_rolls_to_tomorrow():
    # A target of "now.hour - 1, minute 0" is always already past today (by
    # construction, since minute 0 can't be later than the current minute
    # once you're an hour back), so this always exercises the "roll to
    # tomorrow" branch. Compute the expected gap the same way the function
    # does rather than guessing a range -- since target's minute/second is
    # fixed at 0, the gap is always under 24h, and can dip well below 23h
    # if "now" itself is well past its own hour mark.
    tz = ZoneInfo("Europe/London")
    now = datetime.now(tz)
    earlier = (now.hour - 1) % 24
    expected_target = now.replace(hour=earlier, minute=0, second=0, microsecond=0)
    expected_target += timedelta(days=1)
    expected_wait = (expected_target - now).total_seconds()

    wait_sec = _seconds_until_time_of_day(f"{earlier:02d}:00", "Europe/London")
    assert abs(wait_sec - expected_wait) < 2  # small epsilon for time elapsed between calls
    assert 0 < wait_sec < 24 * 3600


def test_returns_seconds_not_days():
    wait_sec = _seconds_until_time_of_day("03:00", "Europe/London")
    assert isinstance(wait_sec, float)
    assert 0 < wait_sec <= 24 * 3600
