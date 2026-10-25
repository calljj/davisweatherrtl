import pytest

import davis_clientraw.state as state_module
from davis_clientraw.state import StationState


def _set_clock(monkeypatch, t: float) -> None:
    monkeypatch.setattr(state_module.time, "monotonic", lambda: t)


@pytest.fixture
def state(tmp_path):
    return StationState(str(tmp_path / "state.sqlite"), "Europe/London", wind_avg_window_sec=300)


def test_averaging_off_by_default_publishes_raw_readings(tmp_path, monkeypatch):
    s = StationState(str(tmp_path / "state.sqlite"), "Europe/London")
    _set_clock(monkeypatch, 1000.0)
    s.update_current(wind_speed_kt=10.0, wind_dir_deg=90.0)
    _set_clock(monkeypatch, 1002.5)
    s.update_current(wind_speed_kt=20.0, wind_dir_deg=100.0)
    assert s.current["wind_speed_kt"] == 20.0
    assert s.current["wind_dir_deg"] == 100.0
    assert "wind_speed_raw_kt" not in s.current


def test_speed_is_mean_over_window(state, monkeypatch):
    for i, speed in enumerate([10.0, 20.0, 30.0]):
        _set_clock(monkeypatch, 1000.0 + 2.5 * i)
        state.update_current(wind_speed_kt=speed)
    assert state.current["wind_speed_kt"] == pytest.approx(20.0)
    # The raw reading is kept alongside for the status page.
    assert state.current["wind_speed_raw_kt"] == 30.0


def test_gust_is_still_the_raw_peak_not_the_average(state, monkeypatch):
    for i, speed in enumerate([10.0, 10.0, 25.0, 10.0]):
        _set_clock(monkeypatch, 1000.0 + 2.5 * i)
        state.update_current(wind_speed_kt=speed)
    assert state.current["gust_kt"] == 25.0
    assert state.current["wind_speed_kt"] == pytest.approx(13.75)


def test_window_max_is_peak_raw_reading_over_window(state, monkeypatch):
    for i, speed in enumerate([10.0, 30.0, 20.0]):
        _set_clock(monkeypatch, 1000.0 + 2.5 * i)
        state.update_current(wind_speed_kt=speed)
    assert state.current["wind_speed_window_max_kt"] == 30.0
    # ... and it ages out with the window, unlike a day max (last sample
    # was at t=1005, so jump past 1005 + window).
    _set_clock(monkeypatch, 1005.0 + 300 + 1)
    state.update_current(wind_speed_kt=5.0)
    assert state.current["wind_speed_window_max_kt"] == 5.0


def test_readings_older_than_window_fall_out(state, monkeypatch):
    _set_clock(monkeypatch, 1000.0)
    state.update_current(wind_speed_kt=40.0)
    _set_clock(monkeypatch, 1000.0 + 300 + 1)
    state.update_current(wind_speed_kt=10.0)
    assert state.current["wind_speed_kt"] == pytest.approx(10.0)


def test_direction_uses_circular_mean(state, monkeypatch):
    _set_clock(monkeypatch, 1000.0)
    state.update_current(wind_dir_deg=350.0)
    _set_clock(monkeypatch, 1002.5)
    state.update_current(wind_dir_deg=10.0)
    assert state.current["wind_dir_deg"] == pytest.approx(0.0, abs=1e-6)
    assert state.current["wind_dir_raw_deg"] == 10.0


def test_direction_only_update_does_not_break_speed_average(state, monkeypatch):
    # The spike filter drops wind_speed_kt but still passes wind_dir_deg;
    # such a sample must not count towards (or crash) the speed mean.
    _set_clock(monkeypatch, 1000.0)
    state.update_current(wind_speed_kt=12.0, wind_dir_deg=90.0)
    _set_clock(monkeypatch, 1002.5)
    state.update_current(wind_dir_deg=95.0)
    assert state.current["wind_speed_kt"] == pytest.approx(12.0)
    assert state.current["gust_kt"] == 12.0
    assert state.current["wind_dir_deg"] == pytest.approx(92.5)


def test_pressure_only_update_leaves_wind_alone(state, monkeypatch):
    _set_clock(monkeypatch, 1000.0)
    state.update_current(wind_speed_kt=12.0, wind_dir_deg=90.0)
    _set_clock(monkeypatch, 1001.0)
    state.update_current(pressure_hpa=1013.0)
    assert state.current["wind_speed_kt"] == pytest.approx(12.0)
    assert state.current["gust_kt"] == 12.0
