"""Persistent station state: current conditions, rolling history, accumulators, records."""
from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

# How far back the "current gust" (field 2 of clientraw.txt) looks for its
# peak. The ISS's own onboard gust calculation is a 10-minute peak-hold that
# only arrives in occasional sub-packets, so it lags well behind the ~2s
# wind speed samples and can look "stuck" between updates -- this instead
# takes the peak of our own fast wind-speed samples, so it moves as soon as
# a new peak is actually seen rather than waiting on the hardware's own timer.
GUST_WINDOW_SEC = 60.0


def _now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


@dataclass
class Record:
    value: float
    hour: int = 0
    minute: int = 0
    day: int = 0
    month: int = 0
    year: int = 0

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "hour": self.hour,
            "minute": self.minute,
            "day": self.day,
            "month": self.month,
            "year": self.year,
        }

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["Record"]:
        if not d:
            return None
        return cls(**d)


class StationState:
    """Thread-safe in-memory station state, snapshotted to SQLite."""

    def __init__(self, db_path: str, timezone: str):
        self.timezone = timezone
        self._lock = threading.RLock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)"
        )
        self._db.commit()

        self.current: dict[str, Any] = {}
        self.minute_history: list[dict[str, Any]] = []  # rolling 60, oldest first
        self.daily: dict[str, Any] = {}
        self.records: dict[str, Optional[dict]] = {}
        self.rain_counters: dict[str, Any] = {}
        # Day-of-month arrays (index 0 = day 1) for clientrawdaily.txt, None = no data yet.
        self.daily_of_month: dict[str, list[Optional[float]]] = {}
        # (monotonic_time, wind_speed_kt) samples within GUST_WINDOW_SEC --
        # transient, in-memory only (a restart losing up to a minute of
        # window is harmless, unlike the persisted state above).
        self._speed_samples: list[tuple[float, float]] = []

        self._load()
        self._roll_day_if_needed()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _load(self) -> None:
        rows = dict(self._db.execute("SELECT key, value FROM state"))
        self.current = json.loads(rows.get("current", "{}"))
        self.minute_history = json.loads(rows.get("minute_history", "[]"))
        self.daily = json.loads(rows.get("daily", "{}"))
        self.records = json.loads(rows.get("records", "{}"))
        self.rain_counters = json.loads(rows.get("rain_counters", "{}"))
        self.daily_of_month = json.loads(rows.get("daily_of_month", "{}"))
        if not self.daily_of_month:
            self.daily_of_month = {
                "day_temperature_max": [None] * 31,
                "day_temperature_min": [None] * 31,
                "month_rain_month": [None] * 31,
                "month_humidity": [None] * 31,
            }

    def save(self) -> None:
        with self._lock:
            data = {
                "current": self.current,
                "minute_history": self.minute_history,
                "daily": self.daily,
                "records": self.records,
                "rain_counters": self.rain_counters,
                "daily_of_month": self.daily_of_month,
            }
            for key, value in data.items():
                self._db.execute(
                    "INSERT INTO state (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)),
                )
            self._db.commit()

    # ------------------------------------------------------------------
    # Day/month/year boundary handling
    # ------------------------------------------------------------------
    def _roll_day_if_needed(self) -> None:
        now = _now(self.timezone)
        today_key = now.strftime("%Y-%m-%d")
        last_day = self.daily.get("_date")

        if last_day == today_key:
            return

        with self._lock:
            if last_day is not None:
                self.daily["_yesterday"] = {
                    k: v for k, v in self.daily.items() if not k.startswith("_")
                }
                prev_date = datetime.strptime(last_day, "%Y-%m-%d")
                if now.month != prev_date.month or now.year != prev_date.year:
                    for key in self.daily_of_month:
                        self.daily_of_month[key] = [None] * 31
                day_idx = prev_date.day - 1
                self.daily_of_month["day_temperature_max"][day_idx] = self.daily.get("temp_max_c")
                self.daily_of_month["day_temperature_min"][day_idx] = self.daily.get("temp_min_c")
                self.daily_of_month["month_rain_month"][day_idx] = self.daily.get("rain_mm")
                self.daily_of_month["month_humidity"][day_idx] = self._humidity_avg

            month_key = now.strftime("%Y-%m")
            if self.rain_counters.get("_month") != month_key:
                self.rain_counters["month_mm"] = 0.0
                self.rain_counters["_month"] = month_key

            year_key = str(now.year)
            if self.rain_counters.get("_year") != year_key:
                self.rain_counters["year_mm"] = 0.0
                self.rain_counters["_year"] = year_key

            self.daily = {
                "_date": today_key,
                "temp_max_c": None,
                "temp_min_c": None,
                "gust_max_kt": None,
                "pressure_max_hpa": None,
                "pressure_min_hpa": None,
                "humidity_sum_pct": 0.0,
                "humidity_samples": 0,
                "rain_mm": 0.0,
                "_yesterday": self.daily.get("_yesterday", {}),
            }
            self.save()

    @property
    def _humidity_avg(self) -> Optional[float]:
        samples = self.daily.get("humidity_samples", 0)
        if not samples:
            return None
        return self.daily["humidity_sum_pct"] / samples

    # ------------------------------------------------------------------
    # Live updates
    # ------------------------------------------------------------------
    def update_current(self, **fields: Any) -> None:
        with self._lock:
            self._roll_day_if_needed()

            wind_speed_kt = fields.get("wind_speed_kt")
            if wind_speed_kt is not None:
                now = time.monotonic()
                self._speed_samples.append((now, wind_speed_kt))
                self._speed_samples = [
                    (t, v) for t, v in self._speed_samples if now - t <= GUST_WINDOW_SEC
                ]
                fields["gust_kt"] = max(v for _, v in self._speed_samples)

            self.current.update(fields)

            temp_c = fields.get("temp_c")
            if temp_c is not None:
                if self.daily["temp_max_c"] is None or temp_c > self.daily["temp_max_c"]:
                    self.daily["temp_max_c"] = temp_c
                if self.daily["temp_min_c"] is None or temp_c < self.daily["temp_min_c"]:
                    self.daily["temp_min_c"] = temp_c
                self._update_record("temperature_max", temp_c, higher_is_record=True)
                self._update_record("temperature_min", temp_c, higher_is_record=False)

            gust_kt = fields.get("gust_kt")
            if gust_kt is not None:
                if self.daily["gust_max_kt"] is None or gust_kt > self.daily["gust_max_kt"]:
                    self.daily["gust_max_kt"] = gust_kt
                self._update_record("high_gust_speed", gust_kt, higher_is_record=True)

            pressure_hpa = fields.get("pressure_hpa")
            if pressure_hpa is not None:
                if self.daily["pressure_max_hpa"] is None or pressure_hpa > self.daily["pressure_max_hpa"]:
                    self.daily["pressure_max_hpa"] = pressure_hpa
                if self.daily["pressure_min_hpa"] is None or pressure_hpa < self.daily["pressure_min_hpa"]:
                    self.daily["pressure_min_hpa"] = pressure_hpa
                self._update_record("barometer_max", pressure_hpa, higher_is_record=True)
                self._update_record("barometer_min", pressure_hpa, higher_is_record=False)

            humidity_pct = fields.get("humidity_pct")
            if humidity_pct is not None:
                self.daily["humidity_sum_pct"] = self.daily.get("humidity_sum_pct", 0.0) + humidity_pct
                self.daily["humidity_samples"] = self.daily.get("humidity_samples", 0) + 1

    def add_rain_mm(self, amount_mm: float) -> None:
        if amount_mm <= 0:
            return
        with self._lock:
            self._roll_day_if_needed()
            self.daily["rain_mm"] = self.daily.get("rain_mm", 0.0) + amount_mm
            self.rain_counters["month_mm"] = self.rain_counters.get("month_mm", 0.0) + amount_mm
            self.rain_counters["year_mm"] = self.rain_counters.get("year_mm", 0.0) + amount_mm

    def _update_record(self, name: str, value: float, higher_is_record: bool) -> None:
        now = _now(self.timezone)
        stamp = {
            "value": value,
            "hour": now.hour,
            "minute": now.minute,
            "day": now.day,
            "month": now.month,
            "year": now.year,
        }
        for scope, key_suffix in (
            ("month", f"record_month_{name}"),
            ("year", f"record_year_{name}"),
            ("alltime", f"record_alltime_{name}"),
        ):
            existing = self.records.get(key_suffix)
            if (
                existing is None
                or (higher_is_record and value > existing["value"])
                or (not higher_is_record and value < existing["value"])
            ):
                self.records[key_suffix] = stamp

    def average_wind_dir_deg(self, window_minutes: int = 10) -> Optional[float]:
        """Circular mean of wind direction over the last `window_minutes`
        one-a-minute samples (falls back to whatever's available if there's
        less history than that, e.g. just after startup). A plain
        arithmetic mean breaks near due north -- averaging 350 and 10 would
        naively give 180 (due south) instead of 0 -- so this averages the
        sin/cos components instead and converts back at the end, which
        handles that wraparound correctly."""
        samples = self.minute_history[-window_minutes:]
        if not samples:
            return None
        sin_sum = sum(math.sin(math.radians(s["wind_dir_deg"])) for s in samples)
        cos_sum = sum(math.cos(math.radians(s["wind_dir_deg"])) for s in samples)
        if math.hypot(sin_sum, cos_sum) < 1e-9:
            # Samples cancel out (near-)exactly -- e.g. an even split
            # between opposing directions -- so the mean direction is
            # genuinely undefined rather than an arbitrary artifact of
            # floating-point noise.
            return None
        # Rounded before the modulo so floating-point noise at the 0/360
        # boundary (e.g. averaging 350 and 10) can't land on 360.0 instead
        # of the equivalent, cleaner 0.0.
        return round(math.degrees(math.atan2(sin_sum, cos_sum)), 6) % 360

    def gust_max_last_hour_kt(self) -> Optional[float]:
        """Peak gust across minute_history, which is itself capped at the
        last 60 one-a-minute samples -- so this is naturally "last hour",
        no separate windowing needed."""
        if not self.minute_history:
            return None
        return max(s.get("gust_kt", 0.0) for s in self.minute_history)

    def append_minute_sample(self) -> None:
        with self._lock:
            sample = {
                "wind_speed_kt": self.current.get("wind_speed_kt", 0.0),
                "wind_dir_deg": self.current.get("wind_dir_deg", 0.0),
                "gust_kt": self.current.get("gust_kt", 0.0),
                "temp_c": self.current.get("temp_c", 0.0),
                "humidity_pct": self.current.get("humidity_pct", 0.0),
                "pressure_hpa": self.current.get("pressure_hpa", 0.0),
                "rain_total_mm": self.daily.get("rain_mm", 0.0),
                "solar_wm2": self.current.get("solar_wm2", 0.0),
                "uv_index": self.current.get("uv_index", 0.0),
            }
            self.minute_history.append(sample)
            self.minute_history = self.minute_history[-60:]
            self.save()
