"""The 'no Davis station number set' warning.

Leaving station.transmitter_id unset makes the config default
rtldavis.transmitters to 255 (all 8 transmitter IDs). rtldavis only leaves
its startup init phase once it has seen a packet from every transmitter it
was told to track, so with a single real ISS it stays in init forever and
never delivers a packet -- while frequency calibration still passes
perfectly, since a sweep parks on one frequency and never exercises
hop-tracking. That combination is very hard to diagnose from the outside,
hence the explicit warning on both pages.
"""
import json

import pytest

from davis_clientraw import webui


def _write_config(tmp_path, transmitter_id):
    cfg = {
        "station": {
            "name": "Test Station",
            "latitude": 51.0,
            "longitude": -4.0,
            "timezone": "Europe/London",
            "transmitter_id": transmitter_id,
        },
        "units": "metric",
        "rain_bucket_mm": 0.2,
        "wind_correction": {"sectors": [], "direction_offset_deg": 0},
        "rtldavis": {
            "bin": "/nonexistent/rtldavis",
            "region": "EU",
            "ppm": 0,
            "transmitters": 255 if transmitter_id is None else 1 << transmitter_id,
            "maxmissed": 4,
            "gain": 0,
            "log_undefined": False,
            "extra_args": [],
            "source_dir": "",
            "gopath": "",
        },
        "pressure": {"source": "placeholder", "placeholder_hpa": 1013.2,
                     "bme280_i2c_addr": "0x76"},
        "transport": "sftp",
        "sftp": {"host": "", "port": 22, "user": "", "password": "",
                 "key_file": "", "remote_dir": ""},
        "scp": {"host": "", "port": 22, "user": "", "key_file": "", "remote_dir": ""},
        "ftp": {"host": "", "port": 21, "user": "", "password": "", "ftps": False,
                "remote_dir": ""},
        "files": {"clientraw": {"remote_name": "clientraw.txt", "interval_sec": 2}},
        "web_port": 8080,
        "templates_dir": "templates",
        "state_db": "state.sqlite",
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return str(path)


def _client(config_path):
    app = webui.create_app(
        config_path,
        get_service_state=lambda: {"running_state": "running", "current": {}, "uploads": {}},
        reload_service=lambda: None,
        send_now_callback=lambda: None,
        start_calibration_sweep=lambda *a, **k: None,
        get_calibration_status=lambda: {"running": False, "finished": False, "error": None,
                                        "points": [], "best": None, "derived_channels": None},
        cancel_calibration_sweep=lambda: None,
        apply_calibration=lambda *a, **k: (True, ""),
        get_current_channels=lambda: [868120000],
    )
    app.config["TESTING"] = True
    return app.test_client()


@pytest.mark.parametrize("page", ["/status", "/config"])
def test_warning_shown_when_station_number_unset(tmp_path, page):
    client = _client(_write_config(tmp_path, None))
    html = client.get(page).get_data(as_text=True)
    assert "station number" in html.lower()
    assert "never start" in html.lower()


@pytest.mark.parametrize("page", ["/status", "/config"])
def test_warning_absent_when_station_number_set(tmp_path, page):
    client = _client(_write_config(tmp_path, 5))
    html = client.get(page).get_data(as_text=True)
    assert "never start" not in html.lower()


def test_config_prefills_station_number_as_one_based(tmp_path):
    # Raw transmitter_id 5 is shown to the user as Davis station number 6.
    client = _client(_write_config(tmp_path, 5))
    html = client.get("/config").get_data(as_text=True)
    assert 'name="station_number" value="6"' in html
