"""Direction-dependent wind speed correction.

Some stations sit near an obstruction (a building, treeline, etc.) that
distorts airflow from a specific direction -- the true wind at that
bearing reads artificially low (or occasionally high, from funneling) at
the sensor. This lets a user define one or more direction sectors, each
with a percentage adjustment, applied to both wind speed and gust before
they're stored/uploaded -- so the correction is baked into clientraw.txt
(and everything downstream that reads it, e.g. a kiosk display) rather
than needing separate handling per consumer.
"""
from __future__ import annotations


def _sector_contains(deg: float, from_deg: float, to_deg: float) -> bool:
    if to_deg - from_deg >= 360:
        # A full-circle span (e.g. "0-360" meaning "always") -- handle before
        # normalizing, since 360 % 360 == 0 would otherwise collapse it down
        # to a single-point sector that only matches deg == from_deg.
        return True
    deg %= 360
    from_deg %= 360
    to_deg %= 360
    if from_deg <= to_deg:
        return from_deg <= deg <= to_deg
    # Sector wraps past 360/0, e.g. from=350, to=30.
    return deg >= from_deg or deg <= to_deg


def find_correction_percent(wind_dir_deg: float, sectors: list[dict]) -> float:
    """Returns the percent adjustment for the first sector (in list order)
    containing wind_dir_deg, or 0.0 if none match. First-match rather than
    summing overlaps -- if sectors are meant to overlap, define one
    combined sector with the combined value instead."""
    for sector in sectors:
        if _sector_contains(wind_dir_deg, sector["from_deg"], sector["to_deg"]):
            return sector["percent"]
    return 0.0


def apply_wind_direction_correction(value: float, wind_dir_deg: float, sectors: list[dict]) -> float:
    percent = find_correction_percent(wind_dir_deg, sectors)
    return value * (1 + percent / 100.0)


def parse_wind_correction_sectors(text: str) -> list[dict]:
    """Parses the settings-page textarea format, one sector per line:
    "<from_deg>-<to_deg>:<percent>[:<label>]", e.g. "60-120:10:building to
    the east". Blank lines and lines that don't parse are skipped rather
    than raising, so one bad line doesn't block saving the rest."""
    sectors = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            deg_part, rest = line.split(":", 1)
            parts = rest.split(":", 1)
            percent = float(parts[0].strip())
            label = parts[1].strip() if len(parts) > 1 else ""
            from_str, to_str = deg_part.split("-")
            sectors.append({
                "from_deg": float(from_str.strip()),
                "to_deg": float(to_str.strip()),
                "percent": percent,
                "label": label,
            })
        except (ValueError, IndexError):
            continue
    return sectors


def format_wind_correction_sectors(sectors: list[dict]) -> str:
    lines = []
    for s in sectors:
        line = f"{s['from_deg']:g}-{s['to_deg']:g}:{s['percent']:g}"
        if s.get("label"):
            line += f":{s['label']}"
        lines.append(line)
    return "\n".join(lines)
