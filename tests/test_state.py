import pytest

from davis_clientraw.state import StationState


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
