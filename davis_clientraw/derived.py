"""Derived meteorological values: dewpoint, wind chill, heat index, apparent temp."""
from __future__ import annotations

import math


def dewpoint_c(temp_c: float, humidity_pct: float) -> float:
    """Magnus formula, a=17.27 b=237.7."""
    a, b = 17.27, 237.7
    humidity_pct = max(humidity_pct, 0.1)
    gamma = (a * temp_c) / (b + temp_c) + math.log(humidity_pct / 100.0)
    return (b * gamma) / (a - gamma)


def wind_chill_c(temp_c: float, wind_speed_kmh: float) -> float:
    """NWS wind chill (valid for temp<=10C and wind>4.8km/h); returns temp_c otherwise."""
    if temp_c > 10.0 or wind_speed_kmh <= 4.8:
        return temp_c
    v = wind_speed_kmh ** 0.16
    return 13.12 + 0.6215 * temp_c - 11.37 * v + 0.3965 * temp_c * v


def heat_index_c(temp_c: float, humidity_pct: float) -> float:
    """NWS Rothfusz regression heat index; returns temp_c below 27C (80F) threshold."""
    temp_f = temp_c * 9 / 5 + 32
    if temp_f < 80.0:
        return temp_c

    t, r = temp_f, humidity_pct
    hi = (
        -42.379
        + 2.04901523 * t
        + 10.14333127 * r
        - 0.22475541 * t * r
        - 0.00683783 * t * t
        - 0.05481717 * r * r
        + 0.00122874 * t * t * r
        + 0.00085282 * t * r * r
        - 0.00000199 * t * t * r * r
    )
    return (hi - 32) * 5 / 9


def apparent_temp_c(temp_c: float, humidity_pct: float, wind_speed_kmh: float) -> float:
    """Australian Bureau of Meteorology apparent temperature."""
    wind_ms = wind_speed_kmh / 3.6
    vapour_pressure = (humidity_pct / 100.0) * 6.105 * math.exp(17.27 * temp_c / (237.7 + temp_c))
    return temp_c + 0.33 * vapour_pressure - 0.70 * wind_ms - 4.00


def feels_like_c(temp_c: float, humidity_pct: float, wind_speed_kmh: float) -> float:
    """Wind chill in cold conditions, heat index in hot conditions, else actual temp."""
    if temp_c <= 10.0:
        return wind_chill_c(temp_c, wind_speed_kmh)
    if temp_c * 9 / 5 + 32 >= 80.0:
        return heat_index_c(temp_c, humidity_pct)
    return temp_c
