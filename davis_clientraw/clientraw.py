"""Load clientraw*.txt templates and generate live files from station state.

Hard rule: every generated file starts as an exact copy of its template's
token list. Only tokens with a confirmed field mapping are overwritten;
everything else keeps the template's original sample value verbatim. This
guarantees field count and the trailing banner marker are always preserved.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from . import wd_field_map as fmap
from .state import StationState

EXPECTED_COUNTS = {
    "clientraw.txt": 178,
    "clientrawhour.txt": 675,
    "clientrawdaily.txt": 443,
    "clientrawextra.txt": 820,
}

EXPECTED_MARKERS = {
    "clientraw.txt": "!!C10.37S152!!",
    "clientrawhour.txt": "!!H10.37S!!",
    "clientrawdaily.txt": "!!D10.37S152!!",
    "clientrawextra.txt": "!!E10.37S!!",
}


def load_template(templates_dir: str, filename: str) -> list[str]:
    path = os.path.join(templates_dir, filename)
    with open(path, "r") as f:
        tokens = f.read().split()
    validate_tokens(filename, tokens)
    return tokens


def validate_tokens(filename: str, tokens: list[str]) -> None:
    expected_count = EXPECTED_COUNTS[filename]
    expected_marker = EXPECTED_MARKERS[filename]
    if len(tokens) != expected_count:
        raise ValueError(
            f"{filename}: expected {expected_count} fields, got {len(tokens)}"
        )
    if tokens[-1] != expected_marker:
        raise ValueError(
            f"{filename}: expected trailing marker {expected_marker!r}, got {tokens[-1]!r}"
        )
    if tokens[0] != "12345":
        raise ValueError(f"{filename}: expected leading '12345', got {tokens[0]!r}")


def _fmt(value: Optional[float], decimals: int = 1) -> Optional[str]:
    if value is None:
        return None
    return f"{value:.{decimals}f}"


def _set(tokens: list[str], positions: dict, name: str, value: Optional[str]) -> None:
    """Overwrite a single-slot field if value is not None."""
    if value is None:
        return
    start, count = positions[name]
    assert count == 1, f"{name} is a {count}-slot block, use _set_block"
    tokens[start] = str(value)


def _set_block(
    tokens: list[str], positions: dict, name: str, values: list[Optional[float]], decimals: int = 1
) -> None:
    """Overwrite an array field slot-by-slot; None entries keep the template value."""
    start, count = positions[name]
    assert len(values) == count, f"{name} expects {count} values, got {len(values)}"
    for i, value in enumerate(values):
        if value is not None:
            tokens[start + i] = _fmt(value, decimals) if isinstance(value, float) else str(value)


def generate_clientraw(state: StationState, config: dict, template_tokens: list[str]) -> str:
    tokens = template_tokens.copy()
    P = fmap.CLIENTRAW_POSITIONS
    c = state.current
    d = state.daily
    tz = ZoneInfo(config["station"]["timezone"])
    now = datetime.now(tz)

    _set(tokens, P, "average_wind_speed", _fmt(c.get("wind_speed_kt")))
    _set(tokens, P, "gust_speed", _fmt(c.get("gust_kt")))
    _set(tokens, P, "wind_direction", _fmt(c.get("wind_dir_deg"), 0))
    _set(tokens, P, "wind_average_direction", _fmt(state.average_wind_dir_deg(), 0))
    _set(tokens, P, "day_gust_speed_max", _fmt(d.get("gust_max_kt")))
    _set(tokens, P, "gust_speed_last_hour_max", _fmt(state.gust_max_last_hour_kt()))
    _set(tokens, P, "temperature", _fmt(c.get("temp_c")))
    _set(tokens, P, "humidity", _fmt(c.get("humidity_pct"), 0))
    _set(tokens, P, "barometer", _fmt(c.get("pressure_hpa")))
    _set(tokens, P, "day_rain", _fmt(d.get("rain_mm")))
    _set(tokens, P, "month_rain", _fmt(state.rain_counters.get("month_mm")))
    _set(tokens, P, "year_rain", _fmt(state.rain_counters.get("year_mm")))
    rain_rate_mm_h = c.get("rain_rate_mm_h")
    if rain_rate_mm_h is not None:
        _set(tokens, P, "rain_rate", _fmt(rain_rate_mm_h / 60.0, 2))
    _set(tokens, P, "rain_rate_max", _fmt(d.get("rain_rate_max_mm_h")))
    _set(tokens, P, "soil_temperature", "100.0")

    yesterday = d.get("_yesterday", {})
    _set(tokens, P, "yesterday_rain", _fmt(yesterday.get("rain_mm")))

    _set(tokens, P, "hour", f"{now.hour:02d}")
    _set(tokens, P, "minute", f"{now.minute:02d}")
    _set(tokens, P, "seconds", f"{now.second:02d}")
    station_name = config["station"]["name"].replace(" ", "_")
    _set(tokens, P, "station_name", station_name)

    # Extended, high-confidence fields.
    temp_c = c.get("temp_c")
    humidity_pct = c.get("humidity_pct")
    wind_speed_kt = c.get("wind_speed_kt")
    if temp_c is not None and humidity_pct is not None:
        from .derived import dewpoint_c, heat_index_c

        _set(tokens, P, "dewpoint_temperature", _fmt(dewpoint_c(temp_c, humidity_pct)))
        _set(tokens, P, "heat_index", _fmt(heat_index_c(temp_c, humidity_pct)))
    if temp_c is not None and wind_speed_kt is not None:
        from .derived import wind_chill_c

        wind_kmh = wind_speed_kt * 1.852
        _set(tokens, P, "wind_chill", _fmt(wind_chill_c(temp_c, wind_kmh)))

    _set(tokens, P, "day_temperature_max", _fmt(d.get("temp_max_c")))
    _set(tokens, P, "day_temperature_min", _fmt(d.get("temp_min_c")))
    _set(tokens, P, "barometer_max", _fmt(d.get("pressure_max_hpa")))
    _set(tokens, P, "barometer_min", _fmt(d.get("pressure_min_hpa")))
    _set(tokens, P, "solar_reading", _fmt(c.get("solar_wm2")))
    _set(tokens, P, "davis_vp_uv", _fmt(c.get("uv_index")))
    _set(tokens, P, "date", f"{now.month}/{now.day}/{now.year}")
    _set(tokens, P, "year", str(now.year))
    _set(tokens, P, "month", str(now.month))
    _set(tokens, P, "day", str(now.day))

    lat = config["station"].get("latitude")
    lon = config["station"].get("longitude")
    if lat is not None:
        _set(tokens, P, "latitude", _fmt(lat, 3))
    if lon is not None:
        _set(tokens, P, "longitude", _fmt(lon, 3))

    recent = state.minute_history[-10:]
    if recent:
        avg10 = sum(m["wind_speed_kt"] for m in recent) / len(recent)
        _set(tokens, P, "10_minutes_average_wind_speed", _fmt(avg10))

    validate_tokens("clientraw.txt", tokens)
    return " ".join(tokens) + "\n"


def generate_clientrawhour(state: StationState, config: dict, template_tokens: list[str]) -> str:
    tokens = template_tokens.copy()
    P = fmap.CLIENTRAWHOUR_POSITIONS
    history = state.minute_history  # oldest first, up to 60

    def right_aligned(key: str, count: int) -> list[Optional[float]]:
        values: list[Optional[float]] = [None] * count
        n = len(history)
        for i in range(min(n, count)):
            values[count - n + i] = history[i][key]
        return values

    _set_block(tokens, P, "wind_speed", right_aligned("wind_speed_kt", 60))
    _set_block(tokens, P, "gust_speed", right_aligned("gust_kt", 60))
    _set_block(tokens, P, "wind_direction", right_aligned("wind_dir_deg", 60), decimals=0)
    _set_block(tokens, P, "temperature", right_aligned("temp_c", 60))
    _set_block(tokens, P, "humidity", right_aligned("humidity_pct", 60), decimals=0)
    _set_block(tokens, P, "barometer", right_aligned("pressure_hpa", 60))
    _set_block(tokens, P, "rain_total", right_aligned("rain_total_mm", 60))
    _set_block(tokens, P, "minutes_solar_data", right_aligned("solar_wm2", 60))

    validate_tokens("clientrawhour.txt", tokens)
    return " ".join(tokens) + "\n"


def generate_clientrawdaily(state: StationState, config: dict, template_tokens: list[str]) -> str:
    tokens = template_tokens.copy()
    P = fmap.CLIENTRAWDAILY_POSITIONS
    tz = ZoneInfo(config["station"]["timezone"])
    now = datetime.now(tz)

    _set(tokens, P, "hour", f"{now.hour:02d}")
    _set(tokens, P, "minute", f"{now.minute:02d}")
    _set(tokens, P, "seconds", f"{now.second:02d}")
    _set(tokens, P, "year_rain_total", _fmt(state.rain_counters.get("year_mm")))

    dom = state.daily_of_month
    _set_block(tokens, P, "day_temperature_max", dom.get("day_temperature_max", [None] * 31))
    _set_block(tokens, P, "day_temperature_min", dom.get("day_temperature_min", [None] * 31))
    _set_block(tokens, P, "month_rain_month", dom.get("month_rain_month", [None] * 31))
    _set_block(tokens, P, "month_humdity", dom.get("month_humidity", [None] * 31), decimals=0)

    validate_tokens("clientrawdaily.txt", tokens)
    return " ".join(tokens) + "\n"


def generate_clientrawextra(state: StationState, config: dict, template_tokens: list[str]) -> str:
    tokens = template_tokens.copy()
    P = fmap.CLIENTRAWEXTRA_POSITIONS
    tz = ZoneInfo(config["station"]["timezone"])
    now = datetime.now(tz)
    d = state.daily
    y = d.get("_yesterday", {})

    _set(tokens, P, "hour", f"{now.hour:02d}")
    _set(tokens, P, "minute", f"{now.minute:02d}")
    _set(tokens, P, "seconds", f"{now.second:02d}")

    _set(tokens, P, "today_temperature_max", _fmt(d.get("temp_max_c")))
    _set(tokens, P, "today_temperature_min", _fmt(d.get("temp_min_c")))
    _set(tokens, P, "today_barometer_max", _fmt(d.get("pressure_max_hpa")))
    _set(tokens, P, "today_barometer_min", _fmt(d.get("pressure_min_hpa")))
    _set(tokens, P, "today_gust_speed_max", _fmt(d.get("gust_max_kt")))

    _set(tokens, P, "yesterday_temperature_max", _fmt(y.get("temp_max_c")))
    _set(tokens, P, "yesterday_temperature_min", _fmt(y.get("temp_min_c")))
    _set(tokens, P, "yesterday_barometer_max", _fmt(y.get("pressure_max_hpa")))
    _set(tokens, P, "yesterday_barometer_min", _fmt(y.get("pressure_min_hpa")))
    _set(tokens, P, "yesterday_gust_speed_max", _fmt(y.get("gust_max_kt")))
    _set(tokens, P, "yesterday_rain", _fmt(y.get("rain_mm")))

    def record_fields(name: str) -> None:
        for scope in ("month", "year", "alltime"):
            rec = state.records.get(f"record_{scope}_{name}")
            start, count = P[f"record_{scope}_{name}"]
            assert count == 6
            if rec is None:
                continue
            tokens[start] = _fmt(rec["value"])
            tokens[start + 1] = str(rec["hour"])
            tokens[start + 2] = str(rec["minute"])
            tokens[start + 3] = str(rec["day"])
            tokens[start + 4] = str(rec["month"])
            tokens[start + 5] = str(rec["year"])

    record_fields("temperature_max")
    record_fields("temperature_min")
    record_fields("high_gust_speed")
    record_fields("barometer_max")
    record_fields("barometer_min")

    validate_tokens("clientrawextra.txt", tokens)
    return " ".join(tokens) + "\n"
