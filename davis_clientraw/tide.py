"""Generates tideprediction.html for a station using the `tide` CLI (XTide).

Requires the `tide` binary (built from flaterco.com/xtide) and a harmonics
.tcd file containing the target station. XTide's currently-downloadable
"free" harmonics package is US/NOS-only; UK stations (e.g. "Portsmouth,
England - READ flaterco.com/pol.html", the exact station referenced by this
project's template) come from an older harmonics build
(harmonics-2004-06-14.tcd, bundled historically with WXTide32) that still
carries the UK Proudman Oceanographic Laboratory-derived constituents under
the terms described at https://flaterco.com/xtide/pol.html.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

EVENT_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}):(\d{2})\s*([AP]M)\s+\S+\s+(.*)$"
)
TIDE_RE = re.compile(r"^([\d.]+)\s+meters\s+(High|Low)\s+Tide$")

PHASE_DISPLAY = {
    "New Moon": "New Moon",
    "Full Moon": "Full Moon",
    "First Quarter": "First Quarter Moon",
    "Last Quarter": "Last Quarter Moon",
}


def _to_24h(hour12: str, minute: str, ampm: str) -> str:
    h = int(hour12) % 12
    if ampm == "PM":
        h += 12
    return f"{h:02d}{minute}"


def run_tide(tide_bin: str, hfile_path: str, station: str, start: datetime, end: datetime) -> str:
    env_cmd = [
        tide_bin,
        "-l", station,
        "-b", start.strftime("%Y-%m-%d %H:%M"),
        "-e", end.strftime("%Y-%m-%d %H:%M"),
        "-u", "m",
    ]
    result = subprocess.run(
        env_cmd,
        env={"HFILE_PATH": hfile_path, "HOME": _home_dir()},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tide command failed: {result.stderr or result.stdout}")
    return result.stdout


def _home_dir() -> str:
    import os
    return os.environ.get("HOME", "/tmp")


def _extract_station_name(raw: str) -> Optional[str]:
    """The station's full official name is the first non-blank line of
    `tide`'s plain-mode output (before the lat/long line and events)."""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped and not EVENT_LINE_RE.match(stripped):
            return stripped
    return None


def parse_events(raw: str) -> dict[str, dict]:
    """Group parsed events by YYYY-MM-DD date string."""
    days: dict[str, dict] = {}
    for line in raw.splitlines():
        m = EVENT_LINE_RE.match(line.strip())
        if not m:
            continue
        date_str, hour12, minute, ampm, rest = m.groups()
        time24 = _to_24h(hour12, minute, ampm)
        day = days.setdefault(
            date_str, {"sun": {}, "moon": {}, "tides": [], "phase": None}
        )

        tide_match = TIDE_RE.match(rest)
        if tide_match:
            value, kind = tide_match.groups()
            day["tides"].append((time24, kind, float(value)))
        elif rest in ("Sunrise", "Sunset"):
            day["sun"][rest] = time24
        elif rest in ("Moonrise", "Moonset"):
            day["moon"][rest] = time24
        elif rest in PHASE_DISPLAY:
            day["phase"] = PHASE_DISPLAY[rest]
        # else: ignore other event types (e.g. Sun's Position, and marks)

    return days


def format_day_block(date_str: str, day: dict) -> str:
    date = datetime.strptime(date_str, "%Y-%m-%d")
    header = f"{date.strftime('%A')} {date.strftime('%m-%d')}   "
    if day["phase"]:
        header += day["phase"]

    lines = [header]

    sun = day["sun"]
    if "Sunrise" in sun or "Sunset" in sun:
        parts = []
        if "Sunrise" in sun:
            parts.append(f"Sunrise {sun['Sunrise']}")
        if "Sunset" in sun:
            parts.append(f"Sunset {sun['Sunset']}")
        lines.append(", ".join(parts))

    moon = day["moon"]
    if moon:
        moon_events = sorted(moon.items(), key=lambda kv: kv[1])
        lines.append(", ".join(f"{name} {t}" for name, t in moon_events))

    for time24, kind, value in sorted(day["tides"], key=lambda t: t[0]):
        lines.append(f"{kind:>6} Tide:  {time24}   {value:.1f}")

    return "\n".join(lines)


def generate_tideprediction_html(
    tide_bin: str, hfile_path: str, station: str, days: int, timezone: str
) -> str:
    tz = ZoneInfo(timezone)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)

    raw = run_tide(tide_bin, hfile_path, station, start, end)
    station_full_name = _extract_station_name(raw) or station
    by_day = parse_events(raw)

    wanted_dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days)]
    blocks = [format_day_block(d, by_day[d]) for d in wanted_dates if d in by_day]

    body = "\n\n".join(blocks)

    return (
        "<HTML><BODY><PRE>\n"
        "<P><center><font size=+2>Local Tide predictions</P></Center></font>\n"
        "\n"
        f"{station_full_name}\n"
        "Units are meters, initial timezone is GMTST\n"
        "\n"
        f"{body}\n"
        "\n"
        "\n"
        "</PRE></BODY></HTML>\n"
    )
