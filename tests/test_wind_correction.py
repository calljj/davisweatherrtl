from davis_clientraw.wind_correction import (
    apply_direction_offset,
    apply_wind_direction_correction,
    find_correction_percent,
    format_wind_correction_sectors,
    parse_wind_correction_sectors,
)

BUILDING_EAST = [{"from_deg": 60, "to_deg": 120, "percent": 10, "label": "building to the east"}]


def test_find_correction_percent_inside_sector():
    assert find_correction_percent(90, BUILDING_EAST) == 10


def test_find_correction_percent_at_sector_edges():
    assert find_correction_percent(60, BUILDING_EAST) == 10
    assert find_correction_percent(120, BUILDING_EAST) == 10


def test_find_correction_percent_outside_sector():
    assert find_correction_percent(200, BUILDING_EAST) == 0


def test_find_correction_percent_no_sectors():
    assert find_correction_percent(90, []) == 0


def test_sector_wraps_past_360():
    sectors = [{"from_deg": 350, "to_deg": 30, "percent": -15, "label": "treeline north"}]
    assert find_correction_percent(0, sectors) == -15
    assert find_correction_percent(355, sectors) == -15
    assert find_correction_percent(10, sectors) == -15
    assert find_correction_percent(180, sectors) == 0


def test_first_matching_sector_wins_on_overlap():
    sectors = [
        {"from_deg": 0, "to_deg": 180, "percent": 5, "label": "first"},
        {"from_deg": 90, "to_deg": 270, "percent": 20, "label": "second"},
    ]
    assert find_correction_percent(100, sectors) == 5


def test_apply_wind_direction_correction_increases_value():
    assert apply_wind_direction_correction(10.0, 90, BUILDING_EAST) == 11.0


def test_apply_wind_direction_correction_no_match_unchanged():
    assert apply_wind_direction_correction(10.0, 200, BUILDING_EAST) == 10.0


def test_apply_wind_direction_correction_negative_percent():
    sectors = [{"from_deg": 60, "to_deg": 120, "percent": -10, "label": ""}]
    assert round(apply_wind_direction_correction(10.0, 90, sectors), 2) == 9.0


def test_full_circle_sector_always_matches():
    # 0-360 collapsing to a single point after naive modulo normalization
    # (360 % 360 == 0) was a real bug caught by this test.
    sectors = [{"from_deg": 0, "to_deg": 360, "percent": -10, "label": "always"}]
    assert find_correction_percent(0, sectors) == -10
    assert find_correction_percent(90, sectors) == -10
    assert find_correction_percent(359, sectors) == -10


def test_parse_sectors_basic():
    sectors = parse_wind_correction_sectors("60-120:10:building to the east")
    assert sectors == [
        {"from_deg": 60.0, "to_deg": 120.0, "percent": 10.0, "label": "building to the east"}
    ]


def test_parse_sectors_without_label():
    sectors = parse_wind_correction_sectors("60-120:10")
    assert sectors == [{"from_deg": 60.0, "to_deg": 120.0, "percent": 10.0, "label": ""}]


def test_parse_sectors_negative_percent():
    sectors = parse_wind_correction_sectors("350-30:-15:treeline")
    assert sectors[0]["percent"] == -15.0


def test_parse_sectors_multiple_lines():
    text = "60-120:10:building\n350-30:-5:trees"
    sectors = parse_wind_correction_sectors(text)
    assert len(sectors) == 2
    assert sectors[0]["label"] == "building"
    assert sectors[1]["label"] == "trees"


def test_parse_sectors_skips_blank_and_malformed_lines():
    text = "60-120:10:building\n\nnot a valid line\n350-30:-5:trees"
    sectors = parse_wind_correction_sectors(text)
    assert len(sectors) == 2


def test_parse_and_format_round_trip():
    original = "60-120:10:building to the east"
    sectors = parse_wind_correction_sectors(original)
    assert format_wind_correction_sectors(sectors) == original


def test_format_multiple_sectors():
    sectors = [
        {"from_deg": 60, "to_deg": 120, "percent": 10, "label": "building"},
        {"from_deg": 350, "to_deg": 30, "percent": -5, "label": ""},
    ]
    assert format_wind_correction_sectors(sectors) == "60-120:10:building\n350-30:-5"


def test_apply_direction_offset_positive():
    assert apply_direction_offset(90, 15) == 105


def test_apply_direction_offset_negative():
    assert apply_direction_offset(90, -15) == 75


def test_apply_direction_offset_wraps_above_360():
    assert apply_direction_offset(350, 20) == 10


def test_apply_direction_offset_wraps_below_zero():
    assert apply_direction_offset(10, -20) == 350


def test_apply_direction_offset_zero_is_noop():
    assert apply_direction_offset(123, 0) == 123
