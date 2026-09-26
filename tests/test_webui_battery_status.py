"""ISS battery and repeater rows on /status.

The ISS transmits a low-battery flag in every packet (bit 3 of byte 0),
decoded all along but previously discarded. A weakening ISS battery shows
up as missing packets, which is easily mistaken for interference or an
aerial fault -- so it is worth surfacing.

Davis repeaters transmit no telemetry of their own in this protocol (they
only relay ISS packets), so there is no repeater battery to report. What
is measurable, and what actually distinguishes a weak ISS-to-repeater hop
from a weak repeater-to-receiver hop, is the relayed/direct split.
"""
import json

import pytest

from davis_clientraw import webui


def _config(tmp_path):
    cfg = {
        "station": {"name": "T", "latitude": 51.0, "longitude": -4.0,
                    "timezone": "Europe/London", "transmitter_id": 1},
        "units": "metric", "rain_bucket_mm": 0.2,
        "wind_correction": {"sectors": [], "direction_offset_deg": 0},
        "rtldavis": {"bin": "/x", "region": "EU", "ppm": 0, "transmitters": 2,
                     "maxmissed": 51, "gain": 207, "log_undefined": False,
                     "extra_args": [], "source_dir": "", "gopath": ""},
        "pressure": {"source": "placeholder", "placeholder_hpa": 1013.2,
                     "bme280_i2c_addr": "0x76"},
        "transport": "sftp",
        "sftp": {"host": "", "port": 22, "user": "", "password": "",
                 "key_file": "", "remote_dir": ""},
        "scp": {"host": "", "port": 22, "user": "", "key_file": "", "remote_dir": ""},
        "ftp": {"host": "", "port": 21, "user": "", "password": "", "ftps": False,
                "remote_dir": ""},
        "files": {"clientraw": {"remote_name": "clientraw.txt", "interval_sec": 2}},
        "web_port": 8080, "templates_dir": "templates", "state_db": "state.sqlite",
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return str(p)


def _status_html(tmp_path, **state):
    base = {"running_state": "running", "current": {}, "uploads": {}}
    base.update(state)
    app = webui.create_app(
        _config(tmp_path),
        get_service_state=lambda: base,
        reload_service=lambda: None,
        send_now_callback=lambda: None,
        start_calibration_sweep=lambda *a, **k: None,
        get_calibration_status=lambda: {"running": False, "finished": False,
                                        "error": None, "points": [], "best": None,
                                        "derived_channels": None},
        cancel_calibration_sweep=lambda: None,
        apply_calibration=lambda *a, **k: (True, ""),
        get_current_channels=lambda: [868120000],
    )
    app.config["TESTING"] = True
    return app.test_client().get("/status").get_data(as_text=True)


def test_iss_battery_unknown_before_any_packet(tmp_path):
    html = _status_html(tmp_path)
    assert "ISS battery:" in html
    assert "no packet decoded yet" in html


def test_iss_battery_ok(tmp_path):
    html = _status_html(tmp_path, iss_battery_low=False,
                        iss_battery_at="2026-09-26T12:00:00+00:00")
    assert 'ISS battery: <span class="ok">OK</span>' in html
    assert "LOW" not in html.split("ISS battery:")[1].split("</p>")[0]


def test_iss_battery_low_is_flagged(tmp_path):
    html = _status_html(tmp_path, iss_battery_low=True,
                        iss_battery_at="2026-09-26T12:00:00+00:00")
    seg = html.split("ISS battery:")[1].split("</p>")[0]
    assert 'class="fail"' in seg and "LOW" in seg


def test_repeater_split_shown(tmp_path):
    html = _status_html(tmp_path, packets_per_min=20, repeated_packets_per_min=18)
    seg = html.split("Repeater:")[1].split("</p>")[0]
    assert "18 of 20" in seg
    assert "2 direct" in seg


def test_repeater_no_packets(tmp_path):
    html = _status_html(tmp_path, packets_per_min=0, repeated_packets_per_min=0)
    assert "no packets in the last minute" in html


def test_repeater_info_bytes_shown_as_undecoded(tmp_path):
    html = _status_html(tmp_path, packets_per_min=1, repeated_packets_per_min=1,
                        last_repeater_info=[0x85, 0x01])
    seg = html.split("Repeater:")[1].split("</p>")[0]
    assert "0x85 0x01" in seg
    # Their meaning is genuinely unsolved; the page must not imply otherwise.
    assert "not yet decoded" in seg


def test_no_repeater_battery_claimed(tmp_path):
    """Davis repeaters send no telemetry, so the page must not invent one."""
    html = _status_html(tmp_path, packets_per_min=5, repeated_packets_per_min=5)
    assert "repeater battery" not in html.lower()
