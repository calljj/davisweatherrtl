from davis_clientraw.tide import format_day_block, parse_events

SAMPLE_RAW = """\
Portsmouth, England - READ flaterco.com/pol.html
50.8008° N, 1.1117° W

2026-07-04  2:38 AM BST   4.28 meters  High Tide
2026-07-04  4:57 AM BST   Sunrise
2026-07-04  7:56 AM BST   1.19 meters  Low Tide
2026-07-04  9:31 AM BST   Moonset
2026-07-04  3:15 PM BST   4.35 meters  High Tide
2026-07-04  8:13 PM BST   1.49 meters  Low Tide
2026-07-04  9:19 PM BST   Sunset
2026-07-04 11:30 PM BST   Moonrise
2026-07-07  8:30 PM BST   Last Quarter
"""


def test_parse_events_groups_by_day():
    days = parse_events(SAMPLE_RAW)
    assert "2026-07-04" in days
    day = days["2026-07-04"]
    assert day["sun"] == {"Sunrise": "0457", "Sunset": "2119"}
    assert day["moon"] == {"Moonset": "0931", "Moonrise": "2330"}
    assert len(day["tides"]) == 4
    assert day["phase"] is None


def test_parse_events_captures_moon_phase():
    days = parse_events(SAMPLE_RAW)
    assert days["2026-07-07"]["phase"] == "Last Quarter Moon"


def test_format_day_block_matches_expected_layout():
    days = parse_events(SAMPLE_RAW)
    block = format_day_block("2026-07-04", days["2026-07-04"])
    expected = (
        "Saturday 07-04   \n"
        "Sunrise 0457, Sunset 2119\n"
        "Moonset 0931, Moonrise 2330\n"
        "  High Tide:  0238   4.3\n"
        "   Low Tide:  0756   1.2\n"
        "  High Tide:  1515   4.3\n"
        "   Low Tide:  2013   1.5"
    )
    assert block == expected


def test_format_day_block_includes_moon_phase_in_header():
    days = parse_events(SAMPLE_RAW)
    days["2026-07-07"]["sun"] = {"Sunrise": "0500", "Sunset": "2118"}
    block = format_day_block("2026-07-07", days["2026-07-07"])
    assert block.startswith("Tuesday 07-07   Last Quarter Moon")
