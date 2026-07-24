"""Collector + scheduler + web UI entrypoint: `python3 -m davis_clientraw`."""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from . import calibrate, clientraw, tide
from .davis_decode import decode_packet, is_valid_packet, is_valid_repeated_packet
from .source_rtldavis import ReceivedPacket, RtldavisSource
from .state import StationState
from .uploaders import ReconnectingUploader
from .webui import run_webui

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("davis_clientraw")

MPH_TO_KT = 0.868976
KT_TO_MPH = 1.0 / MPH_TO_KT


def _seconds_until_time_of_day(time_of_day: str, tz_name: str) -> float:
    """Seconds from now until the next occurrence of HH:MM in the given
    timezone -- today's occurrence if it's still ahead, otherwise
    tomorrow's. Used to anchor a daily task to a specific wall-clock time
    instead of a fixed interval from whenever the service happened to last
    start (which drifts to a different time every restart)."""
    hour, minute = (int(p) for p in time_of_day.split(":"))
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


class Application:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.config = self._load_config()
        self.state = StationState(
            self.config["state_db"], self.config["station"]["timezone"]
        )
        self.templates = {
            name: clientraw.load_template(self.config["templates_dir"], name)
            for name in clientraw.EXPECTED_COUNTS
        }
        self._stop = threading.Event()
        self._source = None
        self._threads: list[threading.Thread] = []
        # Set while a calibration sweep needs exclusive access to the
        # RTL-SDR device; the packet reader loop stops (re)starting its own
        # rtldavis process while this is set.
        self._reception_paused = threading.Event()
        self._calibrator: calibrate.Calibrator | None = None
        self._last_rain_count: int | None = None
        self._packet_times: list[float] = []
        self._service_state = {
            "running_state": "starting",
            "last_packet_time": None,
            "last_packet_repeated": None,
            "last_freq_corr_hz": None,
            "packets_per_min": 0,
            "uploads": {},
            "current": {},
            "clientraw_txt": "",
        }
        self._lock = threading.Lock()

    def _load_config(self) -> dict:
        with open(self.config_path) as f:
            return json.load(f)

    def reload_config(self) -> None:
        with self._lock:
            self.config = self._load_config()
        logger.info("config reloaded")

    # ------------------------------------------------------------------
    # Packet handling
    # ------------------------------------------------------------------
    def _handle_packet(self, received: ReceivedPacket) -> None:
        if received.repeated:
            # Repeater-relayed packets use a different CRC than the classic
            # direct-from-ISS formula -- see davis_decode.is_valid_repeated_packet.
            if received.repeater_info is None or not is_valid_repeated_packet(
                received.data, received.repeater_info
            ):
                logger.debug(
                    "dropped repeater packet with bad CRC: %02X info=%s",
                    bytes(received.data), received.repeater_info,
                )
                return
        elif not is_valid_packet(received.data):
            logger.debug("dropped packet with bad CRC: %02X", bytes(received.data))
            return

        expected_id = self.config["station"].get("transmitter_id")
        decoded = decode_packet(received.data)
        if expected_id is not None and decoded.transmitter_id != expected_id:
            return

        self._packet_times.append(time.time())
        cutoff = time.time() - 60
        self._packet_times = [t for t in self._packet_times if t > cutoff]

        fields: dict = {
            "wind_speed_kt": decoded.wind_speed_mph * MPH_TO_KT,
            "wind_dir_deg": decoded.wind_dir_deg,
        }
        if decoded.temp_f is not None:
            fields["temp_c"] = (decoded.temp_f - 32) * 5 / 9
        if decoded.humidity_pct is not None:
            fields["humidity_pct"] = decoded.humidity_pct
        if decoded.wind_gust_mph is not None:
            fields["gust_kt"] = decoded.wind_gust_mph * MPH_TO_KT
        if decoded.rain_rate_mm_h is not None:
            fields["rain_rate_mm_h"] = decoded.rain_rate_mm_h
        if decoded.solar_wm2 is not None:
            fields["solar_wm2"] = decoded.solar_wm2
        if decoded.uv_index is not None:
            fields["uv_index"] = decoded.uv_index

        self.state.update_current(**fields)

        if decoded.rain_count is not None:
            bucket_mm = self.config["rain_bucket_mm"]
            if self._last_rain_count is not None:
                delta = (decoded.rain_count - self._last_rain_count) & 0x7F
                if delta:
                    self.state.add_rain_mm(delta * bucket_mm)
            self._last_rain_count = decoded.rain_count

        with self._lock:
            self._service_state["last_packet_time"] = datetime.now(dt_timezone.utc).isoformat()
            self._service_state["packets_per_min"] = len(self._packet_times)
            self._service_state["last_packet_repeated"] = received.repeated
            self._service_state["last_freq_corr_hz"] = received.freq_corr_hz

    def _read_bme280_pressure(self) -> float | None:
        try:
            import smbus2
            import bme280  # type: ignore

            addr = int(self.config["pressure"]["bme280_i2c_addr"], 16)
            bus = smbus2.SMBus(1)
            calib = bme280.load_calibration_params(bus, addr)
            data = bme280.sample(bus, addr, calib)
            return data.pressure
        except Exception as exc:
            logger.warning("BME280 read failed: %s", exc)
            return None

    def _packet_reader_loop(self) -> None:
        rtldavis_cfg = self.config["rtldavis"]
        self._source = RtldavisSource(
            rtldavis_cfg["bin"],
            rtldavis_cfg["region"],
            rtldavis_cfg["ppm"],
            rtldavis_cfg["extra_args"],
            transmitters=rtldavis_cfg.get("transmitters", 255),
            maxmissed=rtldavis_cfg.get("maxmissed", 4),
            log_undefined=rtldavis_cfg.get("log_undefined", False),
            gain=rtldavis_cfg.get("gain", 0),
        )
        while not self._stop.is_set():
            if self._reception_paused.is_set():
                self._stop.wait(1)
                continue
            try:
                self._source.start()
                for packet in self._source.packets():
                    if self._stop.is_set() or self._reception_paused.is_set():
                        break
                    self._handle_packet(packet)
                if self._stop.is_set():
                    break
                if not self._reception_paused.is_set():
                    logger.warning("rtldavis source ended, restarting in 5s")
            except Exception:
                logger.exception("packet reader crashed, restarting in 5s")
            finally:
                self._source.stop()
            if not self._reception_paused.is_set():
                self._stop.wait(5)

    def _minute_tick_loop(self) -> None:
        while not self._stop.wait(60):
            self.state.append_minute_sample()

    def _pressure_update_loop(self) -> None:
        """Pressure isn't part of ISS telemetry, so it updates on its own
        cadence rather than piggybacking on packet reception."""
        while not self._stop.is_set():
            pressure_cfg = self.config["pressure"]
            if pressure_cfg["source"] == "bme280":
                reading = self._read_bme280_pressure()
                if reading is not None:
                    self.state.update_current(pressure_hpa=reading)
            else:
                self.state.update_current(pressure_hpa=pressure_cfg["placeholder_hpa"])
            self._stop.wait(30)

    # ------------------------------------------------------------------
    # File generation + upload scheduling
    # ------------------------------------------------------------------
    def _generate(self, file_key: str) -> str:
        generators = {
            "clientraw": clientraw.generate_clientraw,
            "clientrawhour": clientraw.generate_clientrawhour,
            "clientrawdaily": clientraw.generate_clientrawdaily,
            "clientrawextra": clientraw.generate_clientrawextra,
        }
        template_name = f"{file_key}.txt"
        return generators[file_key](self.state, self.config, self.templates[template_name])

    def _generate_and_write_local(self, file_key: str) -> str:
        file_cfg = self.config["files"][file_key]
        remote_name = file_cfg["remote_name"]
        content = self._generate(file_key)

        local_path = os.path.join("/tmp", remote_name)
        tmp_path = local_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(content)
        os.replace(tmp_path, local_path)

        if file_key == "clientraw":
            with self._lock:
                self._service_state["clientraw_txt"] = content

        return local_path

    def _upload_once(self, file_key: str, local_path: str) -> None:
        remote_name = self.config["files"][file_key]["remote_name"]
        uploader = self._uploaders[file_key]
        remote_dir = self.config[self.config["transport"]]["remote_dir"]
        ok = uploader.upload(local_path, remote_dir, remote_name)
        with self._lock:
            self._service_state["uploads"][file_key] = {
                "status": "ok" if ok else "failed",
                "when": datetime.now(dt_timezone.utc).isoformat(),
            }

    def _generation_loop(self, file_key: str) -> None:
        """Regenerates the local file on schedule. Decoupled from uploading,
        so a slow/unreachable upload target never delays fresh local files --
        only the reconnecting upload worker below is allowed to block."""
        interval = self.config["files"][file_key]["interval_sec"]
        while not self._stop.is_set():
            try:
                self._generate_and_write_local(file_key)
            except Exception:
                logger.exception("failed to generate %s", file_key)
            self._stop.wait(interval)

    def _upload_worker(self, file_key: str) -> None:
        """Continuously uploads whatever the latest locally-generated file is.
        A slow/failing connection only throttles uploads, never generation."""
        remote_name = self.config["files"][file_key]["remote_name"]
        local_path = os.path.join("/tmp", remote_name)
        last_uploaded_mtime = None
        while not self._stop.is_set():
            try:
                mtime = os.path.getmtime(local_path)
            except FileNotFoundError:
                self._stop.wait(0.5)
                continue

            if mtime == last_uploaded_mtime:
                self._stop.wait(0.5)
                continue

            self._upload_once(file_key, local_path)
            last_uploaded_mtime = mtime

    def send_now(self) -> None:
        def generate_then_upload(file_key: str) -> None:
            local_path = self._generate_and_write_local(file_key)
            self._upload_once(file_key, local_path)

        for file_key in self.config["files"]:
            threading.Thread(target=generate_then_upload, args=(file_key,), daemon=True).start()

        if self.config.get("tide", {}).get("enabled"):
            def generate_then_upload_tide() -> None:
                local_path = self._generate_tide_html()
                self._tide_upload_once(local_path)

            threading.Thread(target=generate_then_upload_tide, daemon=True).start()

    # ------------------------------------------------------------------
    # Tide predictions (independent of the Davis ISS pipeline)
    # ------------------------------------------------------------------
    def _resolve_hfile_path(self) -> str:
        hfile_path = self.config["tide"]["hfile_path"]
        if os.path.isabs(hfile_path):
            return hfile_path
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_dir, hfile_path)

    def _generate_tide_html(self) -> str:
        tide_cfg = self.config["tide"]
        content = tide.generate_tideprediction_html(
            tide_cfg["tide_bin"],
            self._resolve_hfile_path(),
            tide_cfg["station"],
            tide_cfg["days"],
            self.config["station"]["timezone"],
        )
        local_path = os.path.join("/tmp", tide_cfg["remote_name"])
        tmp_path = local_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(content)
        os.replace(tmp_path, local_path)
        return local_path

    def _tide_upload_once(self, local_path: str) -> None:
        tide_cfg = self.config["tide"]
        remote_dir = tide_cfg.get("remote_dir")
        if remote_dir is None:
            remote_dir = self.config[self.config["transport"]]["remote_dir"]
        ok = self._tide_uploader.upload(local_path, remote_dir, tide_cfg["remote_name"])
        with self._lock:
            self._service_state["uploads"]["tide"] = {
                "status": "ok" if ok else "failed",
                "when": datetime.now(dt_timezone.utc).isoformat(),
            }

    def _tide_generation_loop(self) -> None:
        if not self.config.get("tide", {}).get("enabled"):
            return
        while not self._stop.is_set():
            try:
                self._generate_tide_html()
            except Exception:
                logger.exception("failed to generate tideprediction.html")

            time_of_day = self.config["tide"].get("time_of_day")
            if time_of_day:
                wait_sec = _seconds_until_time_of_day(
                    time_of_day, self.config["station"]["timezone"]
                )
            else:
                wait_sec = self.config["tide"]["interval_sec"]
            self._stop.wait(wait_sec)

    def _tide_upload_worker(self) -> None:
        if not self.config.get("tide", {}).get("enabled"):
            return
        local_path = os.path.join("/tmp", self.config["tide"]["remote_name"])
        last_uploaded_mtime = None
        while not self._stop.is_set():
            try:
                mtime = os.path.getmtime(local_path)
            except FileNotFoundError:
                self._stop.wait(0.5)
                continue

            if mtime == last_uploaded_mtime:
                self._stop.wait(0.5)
                continue

            self._tide_upload_once(local_path)
            last_uploaded_mtime = mtime

    # ------------------------------------------------------------------
    # Frequency calibration (GUI-triggered)
    # ------------------------------------------------------------------
    def start_calibration_sweep(
        self, startfreq: int, endfreq: int, stepfreq: int, gain: int | None = None
    ) -> None:
        """Pauses normal packet reception (it and the sweep can't share the
        RTL-SDR device) and starts a calibration sweep in the background.
        gain overrides the configured rtldavis.gain for this sweep only --
        config.json is not modified."""
        rtldavis_cfg = self.config["rtldavis"]
        self._reception_paused.set()
        if self._source is not None:
            self._source.stop()
        self._calibrator = calibrate.Calibrator(
            rtldavis_cfg["bin"],
            rtldavis_cfg["region"],
            rtldavis_cfg.get("transmitters", 255),
            gain if gain is not None else rtldavis_cfg.get("gain", 0),
        )
        self._calibrator.start(startfreq, endfreq, stepfreq)

    def get_current_channels(self) -> list[int] | None:
        """Whatever channel frequencies are actually baked into the
        currently-built rtldavis binary for the configured region, read
        straight from its source of truth (protocol.go) -- not from
        config.json, which doesn't track this at all. NZ has no
        calibration-UI support, so returns None there (same as any other
        read failure)."""
        region = self.config["rtldavis"].get("region", "EU")
        source_dir = self.config["rtldavis"].get("source_dir")
        if not source_dir or region not in ("EU", "US"):
            return None
        protocol_go_path = os.path.join(source_dir, "protocol", "protocol.go")
        try:
            if region == "US":
                return calibrate.read_us_protocol_go_channels(protocol_go_path)
            return calibrate.read_protocol_go_channels(protocol_go_path)
        except Exception:
            logger.exception("failed to read current channel table")
            return None

    def get_calibration_status(self) -> dict:
        if self._calibrator is None:
            return {
                "running": False, "finished": False, "error": None,
                "points": [], "best": None, "derived_channels": None,
            }
        return self._calibrator.status()

    def cancel_calibration_sweep(self) -> None:
        if self._calibrator is not None:
            self._calibrator.stop()
        self._reception_paused.clear()

    def apply_calibration(self, channels: list[int], comment: str) -> tuple[bool, str]:
        """Writes the derived channel table into rtldavis's Go source,
        rebuilds it in place at the configured bin path (no sudo needed --
        that path lives under the project directory, not /usr/local/bin),
        and resumes normal packet reception either way. Which region's
        block gets written is decided by the configured rtldavis.region --
        NZ is rejected outright rather than falling through to the EU
        writer, since a channel count for the wrong region would otherwise
        silently corrupt the EU table (EU_CHANNELS_RE matches regardless of
        how many values it's given).

        Explicitly pauses reception and stops the running rtldavis process
        itself (not just the pause flag) -- this used to only work
        correctly when called right after start_calibration_sweep, which
        did that stop as a side effect. Called on its own (e.g. directly
        via POST /calibrate/apply), the old process kept running the whole
        time and the rebuilt binary silently never took effect until
        something else happened to restart the service."""
        self._reception_paused.set()
        if self._source is not None:
            self._source.stop()

        rtldavis_cfg = self.config["rtldavis"]
        region = rtldavis_cfg.get("region", "EU")
        source_dir = rtldavis_cfg.get("source_dir")
        gopath = rtldavis_cfg.get("gopath")
        if not source_dir or not gopath:
            self._reception_paused.clear()
            return False, "config.json rtldavis.source_dir / gopath is not set"
        if region not in ("EU", "US"):
            self._reception_paused.clear()
            return False, f"calibration is not supported for region {region!r}"

        protocol_go_path = os.path.join(source_dir, "protocol", "protocol.go")
        try:
            if region == "US":
                calibrate.write_us_protocol_go(protocol_go_path, channels, comment)
            else:
                calibrate.write_protocol_go(protocol_go_path, channels, comment)
        except Exception as exc:
            self._reception_paused.clear()
            return False, f"failed to write protocol.go: {exc}"

        ok, output = calibrate.rebuild(source_dir, rtldavis_cfg["bin"], gopath)
        self._reception_paused.clear()
        return ok, output

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._uploaders = {
            file_key: ReconnectingUploader(lambda: self.config) for file_key in self.config["files"]
        }
        self._tide_uploader = ReconnectingUploader(lambda: self.config)

        self._threads.append(threading.Thread(target=self._packet_reader_loop, daemon=True))
        self._threads.append(threading.Thread(target=self._minute_tick_loop, daemon=True))
        self._threads.append(threading.Thread(target=self._pressure_update_loop, daemon=True))
        for file_key in self.config["files"]:
            self._threads.append(
                threading.Thread(target=self._generation_loop, args=(file_key,), daemon=True)
            )
            self._threads.append(
                threading.Thread(target=self._upload_worker, args=(file_key,), daemon=True)
            )
        self._threads.append(threading.Thread(target=self._tide_generation_loop, daemon=True))
        self._threads.append(threading.Thread(target=self._tide_upload_worker, daemon=True))
        for t in self._threads:
            t.start()

        run_webui(
            self.config_path,
            self._get_service_state,
            self.reload_config,
            self.send_now,
            self.start_calibration_sweep,
            self.get_calibration_status,
            self.cancel_calibration_sweep,
            self.apply_calibration,
            self.get_current_channels,
            "0.0.0.0",
            self.config["web_port"],
        )

        with self._lock:
            self._service_state["running_state"] = "running"
        logger.info("davis-clientraw started")

    def _get_service_state(self) -> dict:
        # "current" always reflects the live StationState, not a snapshot --
        # pressure (and anything else updated outside _handle_packet) would
        # otherwise never show up here while no ISS packets have arrived yet.
        with self._lock:
            snapshot = dict(self._service_state)
        snapshot["current"] = dict(self.state.current)
        return snapshot

    def stop(self) -> None:
        with self._lock:
            self._service_state["running_state"] = "stopping"
        self._stop.set()
        if self._source is not None:
            self._source.stop()
        for uploader in getattr(self, "_uploaders", {}).values():
            uploader.close()
        tide_uploader = getattr(self, "_tide_uploader", None)
        if tide_uploader is not None:
            tide_uploader.close()


def main() -> None:
    project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.environ.get("DAVIS_CLIENTRAW_CONFIG", os.path.join(project_dir, "config.json"))

    app = Application(config_path)

    def handle_signal(signum, frame):
        logger.info("received signal %s, shutting down", signum)
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    app.start()
    signal.pause()


if __name__ == "__main__":
    main()
