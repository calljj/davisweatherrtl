import pytest

import davis_clientraw.state as state_module
from davis_clientraw.state import GUST_WINDOW_SEC, StationState


@pytest.fixture
def state(tmp_path):
    return StationState(str(tmp_path / "state.sqlite"), "Europe/London")


def _push_direction_samples(state, directions):
    for deg in directions:
        state.update_current(wind_dir_deg=deg)
        state.append_minute_sample()


def test_average_wind_dir_no_history_returns_none(state):
    assert state.average_wind_dir_deg() is None


def test_average_wind_dir_single_sample(state):
    _push_direction_samples(state, [90])
    assert state.average_wind_dir_deg() == pytest.approx(90)


def test_average_wind_dir_simple_mean_away_from_wraparound(state):
    _push_direction_samples(state, [80, 100])
    assert state.average_wind_dir_deg() == pytest.approx(90)


def test_average_wind_dir_handles_0_360_wraparound(state):
    # A naive arithmetic mean of 350 and 10 gives 180 (due south) -- the
    # true circular mean is 0 (due north).
    _push_direction_samples(state, [350, 10])
    assert state.average_wind_dir_deg() == pytest.approx(0, abs=1e-6)


def test_average_wind_dir_only_uses_last_window_minutes(state):
    # 9 samples at 90 deg, then a run of 270 deg -- only the most recent
    # `window_minutes` should count, so a small window should be pulled
    # entirely towards 270 despite the older 90-deg samples still sitting
    # in minute_history.
    _push_direction_samples(state, [90] * 9 + [270] * 5)
    assert state.average_wind_dir_deg(window_minutes=5) == pytest.approx(270)


def test_average_wind_dir_uses_all_history_if_shorter_than_window(state):
    _push_direction_samples(state, [45, 45, 45])
    assert state.average_wind_dir_deg(window_minutes=10) == pytest.approx(45)


def test_average_wind_dir_opposing_samples_cancel_to_none(state):
    _push_direction_samples(state, [0, 180])
    assert state.average_wind_dir_deg() is None


def _set_clock(monkeypatch, t: float) -> None:
    monkeypatch.setattr(state_module.time, "monotonic", lambda: t)


def test_gust_kt_matches_a_single_reading(state, monkeypatch):
    _set_clock(monkeypatch, 1000.0)
    state.update_current(wind_speed_kt=10.0)
    assert state.current["gust_kt"] == 10.0


def test_gust_kt_is_the_peak_of_recent_readings(state, monkeypatch):
    _set_clock(monkeypatch, 1000.0)
    state.update_current(wind_speed_kt=5.0)
    _set_clock(monkeypatch, 1002.0)
    state.update_current(wind_speed_kt=12.0)
    _set_clock(monkeypatch, 1004.0)
    state.update_current(wind_speed_kt=8.0)
    # "current" wind speed is whatever was set most recently ...
    assert state.current["wind_speed_kt"] == 8.0
    # ... but gust still reflects the 12.0 peak seen a couple of packets ago.
    assert state.current["gust_kt"] == 12.0


def test_gust_kt_drops_readings_older_than_window(state, monkeypatch):
    _set_clock(monkeypatch, 1000.0)
    state.update_current(wind_speed_kt=20.0)
    _set_clock(monkeypatch, 1000.0 + GUST_WINDOW_SEC + 1)
    state.update_current(wind_speed_kt=6.0)
    # The 20.0 peak has aged out of the window -- gust should now just be
    # the one remaining recent reading, not still holding the stale peak.
    assert state.current["gust_kt"] == 6.0


def test_gust_kt_untouched_when_wind_speed_not_in_update(state, monkeypatch):
    _set_clock(monkeypatch, 1000.0)
    state.update_current(wind_speed_kt=15.0)
    assert state.current["gust_kt"] == 15.0
    _set_clock(monkeypatch, 1001.0)
    state.update_current(pressure_hpa=1013.0)
    # An update with no wind_speed_kt at all (e.g. the pressure poller's own
    # cadence) shouldn't touch or reset the last-known gust.
    assert state.current["gust_kt"] == 15.0


def test_gust_max_last_hour_no_history_returns_none(state):
    assert state.gust_max_last_hour_kt() is None


def test_gust_max_last_hour_is_peak_across_minute_samples(state, monkeypatch):
    for i, speed in enumerate([10.0, 25.0, 15.0]):
        _set_clock(monkeypatch, 1000.0 + i)
        state.update_current(wind_speed_kt=speed)
        state.append_minute_sample()
    assert state.gust_max_last_hour_kt() == 25.0
