from davis_clientraw.__main__ import WIND_SPEED_SPIKE_THRESHOLD_KT, _is_wind_speed_spike


def test_no_prior_reading_never_a_spike():
    assert _is_wind_speed_spike(999.0, None) is False


def test_small_jump_not_a_spike():
    assert _is_wind_speed_spike(20.0, 15.0) is False


def test_jump_exactly_at_threshold_not_a_spike():
    assert _is_wind_speed_spike(15.0 + WIND_SPEED_SPIKE_THRESHOLD_KT, 15.0) is False


def test_jump_just_over_threshold_is_a_spike():
    assert _is_wind_speed_spike(15.0 + WIND_SPEED_SPIKE_THRESHOLD_KT + 0.1, 15.0) is True


def test_real_world_spurious_spike_example():
    # The kind of reading this filter exists for -- e.g. 15 kt one moment,
    # 140+ kt the next, clearly a decode glitch rather than a real gust.
    assert _is_wind_speed_spike(140.0, 15.0) is True


def test_large_drop_is_also_a_spike():
    # A sudden implausible *drop* is just as suspect as a spike upward --
    # the check is symmetric.
    assert _is_wind_speed_spike(2.0, 130.0) is True


def test_custom_threshold_respected():
    assert _is_wind_speed_spike(25.0, 15.0, threshold_kt=5.0) is True
    assert _is_wind_speed_spike(18.0, 15.0, threshold_kt=5.0) is False
