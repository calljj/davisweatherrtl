"""Local Flask configuration/status/preview UI. No auth -- bind to LAN only."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
import threading

from flask import Flask, jsonify, redirect, render_template_string, request, url_for

PAGE_SHELL = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Davis clientraw uploader</title>
{{ extra_head|safe }}
<style>
  body { font-family: sans-serif; max-width: 900px; margin: 2em auto; color: #222; }
  nav a { margin-right: 1em; }
  fieldset { margin-bottom: 1.5em; }
  label { display: block; margin: 0.4em 0 0.1em; font-weight: bold; }
  input, select { width: 100%; max-width: 400px; padding: 0.3em; }
  table { border-collapse: collapse; width: 100%; }
  td, th { border: 1px solid #ccc; padding: 0.3em 0.6em; text-align: left; }
  pre { background: #f4f4f4; padding: 1em; overflow-x: auto; }
  .ok { color: green; } .fail { color: #b00; }
  button { padding: 0.5em 1em; margin-top: 0.5em; cursor: pointer; }
</style>
</head>
<body>
<nav>
  <a href="{{ url_for('config_page') }}">Config</a>
  <a href="{{ url_for('status_page') }}">Status</a>
  <a href="{{ url_for('preview_page') }}">Preview</a>
  <a href="{{ url_for('calibrate_page') }}">Calibrate</a>
</nav>
<hr>
{{ body|safe }}
</body>
</html>
"""


def create_app(
    config_path: str,
    get_service_state,
    reload_service,
    send_now_callback,
    start_calibration_sweep,
    get_calibration_status,
    cancel_calibration_sweep,
    apply_calibration,
    get_current_channels,
):
    app = Flask(__name__)

    def render(body: str, extra_head: str = ""):
        return render_template_string(PAGE_SHELL, body=body, extra_head=extra_head)

    def load_config() -> dict:
        with open(config_path) as f:
            return json.load(f)

    def save_config(cfg: dict) -> None:
        with open(config_path + ".tmp", "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(config_path + ".tmp", config_path)
        os.chmod(config_path, 0o600)

    @app.route("/")
    def index():
        return redirect(url_for("status_page"))

    @app.route("/config", methods=["GET", "POST"])
    def config_page():
        cfg = load_config()
        message = ""
        if request.method == "POST":
            form = request.form
            cfg["transport"] = form.get("transport", cfg["transport"])
            cfg["station"]["name"] = form.get("station_name", cfg["station"]["name"])
            cfg["station"]["latitude"] = float(form.get("latitude", cfg["station"]["latitude"]))
            cfg["station"]["longitude"] = float(form.get("longitude", cfg["station"]["longitude"]))
            cfg["station"]["timezone"] = form.get("timezone", cfg["station"]["timezone"])
            # Davis station number (1-8, as printed on the console/DIP switch)
            # drives both the app-level decode filter (station.transmitter_id,
            # stored as the raw 0-7 protocol ID) and the RF-layer -tr bitmask
            # rtldavis tracks (rtldavis.transmitters) -- there's no reason
            # these would ever need to differ for a single-station setup, so
            # one form field sets both rather than asking for the same thing
            # twice in two different encodings.
            station_number_raw = form.get("station_number", "").strip()
            if station_number_raw:
                raw_id = int(station_number_raw) - 1
                cfg["station"]["transmitter_id"] = raw_id
                cfg["rtldavis"]["transmitters"] = 1 << raw_id
            else:
                cfg["station"]["transmitter_id"] = None
                cfg["rtldavis"]["transmitters"] = 255
            cfg["web_port"] = int(form.get("web_port", cfg["web_port"]))
            cfg["units"] = form.get("units", cfg["units"])
            cfg["rain_bucket_mm"] = float(form.get("rain_bucket_mm", cfg["rain_bucket_mm"]))

            cfg["rtldavis"]["bin"] = form.get("rtldavis_bin", cfg["rtldavis"]["bin"])
            cfg["rtldavis"]["region"] = form.get("rtldavis_region", cfg["rtldavis"]["region"])
            cfg["rtldavis"]["ppm"] = int(form.get("rtldavis_ppm", cfg["rtldavis"]["ppm"]))
            cfg["rtldavis"]["maxmissed"] = int(
                form.get("rtldavis_maxmissed", cfg["rtldavis"].get("maxmissed", 4))
            )
            cfg["rtldavis"]["log_undefined"] = "rtldavis_log_undefined" in form
            cfg["rtldavis"]["gain"] = int(form.get("rtldavis_gain", cfg["rtldavis"].get("gain", 0)))
            extra_args_raw = form.get("rtldavis_extra_args", "").strip()
            cfg["rtldavis"]["extra_args"] = extra_args_raw.split() if extra_args_raw else []

            cfg["pressure"]["source"] = form.get("pressure_source", cfg["pressure"]["source"])
            cfg["pressure"]["placeholder_hpa"] = float(
                form.get("pressure_placeholder_hpa", cfg["pressure"]["placeholder_hpa"])
            )
            cfg["pressure"]["bme280_i2c_addr"] = form.get(
                "pressure_bme280_i2c_addr", cfg["pressure"]["bme280_i2c_addr"]
            )

            if "tide" in cfg:
                cfg["tide"]["enabled"] = "tide_enabled" in form
                cfg["tide"]["station"] = form.get("tide_station", cfg["tide"]["station"])
                cfg["tide"]["days"] = int(form.get("tide_days", cfg["tide"]["days"]))
                cfg["tide"]["interval_sec"] = int(
                    form.get("tide_interval_sec", cfg["tide"]["interval_sec"])
                )
                cfg["tide"]["tide_bin"] = form.get("tide_bin", cfg["tide"]["tide_bin"])
                cfg["tide"]["hfile_path"] = form.get("tide_hfile_path", cfg["tide"]["hfile_path"])
                cfg["tide"]["remote_name"] = form.get(
                    "tide_remote_name", cfg["tide"]["remote_name"]
                )
                remote_dir_raw = form.get("tide_remote_dir", "")
                cfg["tide"]["remote_dir"] = remote_dir_raw if remote_dir_raw else None

            for transport in ("sftp", "scp", "ftp"):
                prefix = f"{transport}_"
                block = cfg[transport]
                for key in list(block.keys()):
                    field = f"{prefix}{key}"
                    if field in form:
                        raw = form.get(field)
                        if isinstance(block[key], bool):
                            block[key] = raw == "on"
                        elif isinstance(block[key], int):
                            block[key] = int(raw)
                        else:
                            block[key] = raw

            for file_key, file_cfg in cfg["files"].items():
                interval_field = f"interval_{file_key}"
                if interval_field in form:
                    file_cfg["interval_sec"] = int(form.get(interval_field))

            save_config(cfg)
            reload_service()
            message = '<p class="ok">Saved and reloaded.</p>'

        body = message + render_template_string(
            """
        <form method="post">
        <fieldset>
        <legend>Station</legend>
        <label>Name</label><input name="station_name" value="{{c.station.name}}">
        <label>Latitude</label><input name="latitude" value="{{c.station.latitude}}">
        <label>Longitude</label><input name="longitude" value="{{c.station.longitude}}">
        <label>Timezone</label><input name="timezone" value="{{c.station.timezone}}">
        <label>Davis station number (as shown on the console/DIP switch; blank = accept any ISS)</label>
        <input name="station_number" value="{{ (c.station.transmitter_id + 1) if c.station.transmitter_id is not none else '' }}" placeholder="1-8, or blank">
        <label>Units</label>
        <select name="units">
          {% for u in ['metric','imperial'] %}
          <option value="{{u}}" {{'selected' if c.units==u else ''}}>{{u}}</option>
          {% endfor %}
        </select>
        <label>Rain bucket size (mm)</label><input name="rain_bucket_mm" value="{{c.rain_bucket_mm}}">
        </fieldset>

        <fieldset>
        <legend>rtldavis (RTL-SDR)</legend>
        <label>Binary path</label><input name="rtldavis_bin" value="{{c.rtldavis.bin}}">
        <label>Region</label>
        <select name="rtldavis_region">
          {% for r in ['EU','US','NZ'] %}
          <option value="{{r}}" {{'selected' if c.rtldavis.region==r else ''}}>{{r}}</option>
          {% endfor %}
        </select>
        <label>PPM correction</label><input name="rtldavis_ppm" value="{{c.rtldavis.ppm}}">
        <label>Max missed packets before resync</label>
        <input name="rtldavis_maxmissed" value="{{c.rtldavis.maxmissed}}">
        <label>Tuner gain in tenths of dB (0 = AGC/auto; e.g. 207 = 20.7dB)</label>
        <input name="rtldavis_gain" value="{{c.rtldavis.gain}}">
        <label><input type="checkbox" name="rtldavis_log_undefined" {{'checked' if c.rtldavis.log_undefined else ''}} style="width:auto"> Log undefined signals (diagnostic)</label>
        <label>Extra args (space-separated)</label>
        <input name="rtldavis_extra_args" value="{{c.rtldavis.extra_args|join(' ')}}">
        </fieldset>

        <fieldset>
        <legend>Pressure</legend>
        <label>Source</label>
        <select name="pressure_source">
          {% for p in ['placeholder','bme280'] %}
          <option value="{{p}}" {{'selected' if c.pressure.source==p else ''}}>{{p}}</option>
          {% endfor %}
        </select>
        <label>Placeholder value (hPa)</label><input name="pressure_placeholder_hpa" value="{{c.pressure.placeholder_hpa}}">
        <label>BME280 I2C address</label><input name="pressure_bme280_i2c_addr" value="{{c.pressure.bme280_i2c_addr}}">
        </fieldset>

        {% if c.tide %}
        <fieldset>
        <legend>Tide predictions</legend>
        <label><input type="checkbox" name="tide_enabled" {{'checked' if c.tide.enabled else ''}} style="width:auto"> Enabled</label>
        <label>Station</label><input name="tide_station" value="{{c.tide.station}}">
        <label>Days ahead</label><input name="tide_days" value="{{c.tide.days}}">
        <label>Upload interval (seconds)</label><input name="tide_interval_sec" value="{{c.tide.interval_sec}}">
        <label>tide binary path</label><input name="tide_bin" value="{{c.tide.tide_bin}}">
        <label>Harmonics file path</label><input name="tide_hfile_path" value="{{c.tide.hfile_path}}">
        <label>Remote filename</label><input name="tide_remote_name" value="{{c.tide.remote_name}}">
        <label>Remote folder (blank = same folder as weather files)</label>
        <input name="tide_remote_dir" value="{{c.tide.remote_dir or ''}}" placeholder="leave blank to use weather files' folder">
        </fieldset>
        {% endif %}

        <fieldset>
        <legend>Transport</legend>
        <label>Transport</label>
        <select name="transport">
          {% for t in ['sftp','scp','ftp'] %}
          <option value="{{t}}" {{'selected' if c.transport==t else ''}}>{{t}}</option>
          {% endfor %}
        </select>
        </fieldset>

        <fieldset>
        <legend>SFTP</legend>
        <label>Host</label><input name="sftp_host" value="{{c.sftp.host}}">
        <label>Port</label><input name="sftp_port" value="{{c.sftp.port}}">
        <label>User</label><input name="sftp_user" value="{{c.sftp.user}}">
        <label>Password</label><input type="password" name="sftp_password" value="{{c.sftp.password}}">
        <label>Key file</label><input name="sftp_key_file" value="{{c.sftp.key_file}}">
        <label>Remote dir</label><input name="sftp_remote_dir" value="{{c.sftp.remote_dir}}">
        </fieldset>

        <fieldset>
        <legend>SCP</legend>
        <label>Host</label><input name="scp_host" value="{{c.scp.host}}">
        <label>Port</label><input name="scp_port" value="{{c.scp.port}}">
        <label>User</label><input name="scp_user" value="{{c.scp.user}}">
        <label>Key file</label><input name="scp_key_file" value="{{c.scp.key_file}}">
        <label>Remote dir</label><input name="scp_remote_dir" value="{{c.scp.remote_dir}}">
        </fieldset>

        <fieldset>
        <legend>FTP</legend>
        <label>Host</label><input name="ftp_host" value="{{c.ftp.host}}">
        <label>Port</label><input name="ftp_port" value="{{c.ftp.port}}">
        <label>User</label><input name="ftp_user" value="{{c.ftp.user}}">
        <label>Password</label><input type="password" name="ftp_password" value="{{c.ftp.password}}">
        <label>Remote dir</label><input name="ftp_remote_dir" value="{{c.ftp.remote_dir}}">
        </fieldset>

        <fieldset>
        <legend>Upload intervals (seconds)</legend>
        {% for key, fc in c.files.items() %}
        <label>{{fc.remote_name}}</label><input name="interval_{{key}}" value="{{fc.interval_sec}}">
        {% endfor %}
        </fieldset>

        <button type="submit">Save &amp; reload</button>
        </form>
        <form method="post" action="{{ url_for('test_connection') }}"><button>Test connection</button></form>
        """,
            c=cfg,
        )
        return render(body)

    @app.route("/status")
    def status_page():
        import datetime as dt

        state = get_service_state()
        rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in state.get("current", {}).items()
        )
        upload_rows = "".join(
            f"<tr><td>{k}</td><td>{v.get('status')}</td><td>{v.get('when')}</td></tr>"
            for k, v in state.get("uploads", {}).items()
        )

        last_packet_time = state.get("last_packet_time")
        via_repeater = " via repeater" if state.get("last_packet_repeated") else ""
        if not last_packet_time:
            signal_dot, signal_color, signal_text = "●", "#b00", "NO SIGNAL (no packet ever received)"
        else:
            age_sec = (
                dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last_packet_time)
            ).total_seconds()
            if age_sec <= 30:
                signal_dot, signal_color, signal_text = "●", "green", f"RECEIVING (last packet {age_sec:.0f}s ago{via_repeater})"
            else:
                signal_dot, signal_color, signal_text = "●", "#b00", f"NO SIGNAL (last packet {age_sec:.0f}s ago{via_repeater})"

        afc_hz = state.get("last_freq_corr_hz")
        afc_html = f"{afc_hz:+d} Hz" if afc_hz is not None else "unknown"

        channels = get_current_channels()
        if channels:
            channels_html = ", ".join(f"{c} Hz" for c in channels)
        else:
            channels_html = '<span class="fail">unknown (could not read protocol.go)</span>'

        body = f"""
        <h2>Status: {state.get('running_state', 'unknown')}</h2>
        <p style="font-size:1.2em"><span style="color:{signal_color}">{signal_dot}</span> {signal_text}</p>
        <p>Packets/min: {state.get('packets_per_min', 0)}</p>
        <p>EU channel frequencies: {channels_html} &nbsp; <a href="{url_for('calibrate_page')}">(recalibrate)</a></p>
        <p>AFC correction (last packet): {afc_html}</p>
        <form method="post" action="{url_for('send_now')}"><button>Send now</button></form>
        <h3>Current conditions</h3>
        <table>{rows}</table>
        <h3>Last upload results</h3>
        <table><tr><th>File</th><th>Status</th><th>When</th></tr>{upload_rows}</table>
        """
        return render(body, extra_head='<meta http-equiv="refresh" content="5">')

    @app.route("/preview")
    def preview_page():
        state = get_service_state()
        raw = state.get("clientraw_txt", "")
        parsed_rows = "".join(
            f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in state.get("current", {}).items()
        )
        body = f"""
        <h2>clientraw.txt (raw)</h2>
        <pre>{raw}</pre>
        <h2>Parsed breakdown</h2>
        <table>{parsed_rows}</table>
        """
        return render(body)

    @app.route("/test-connection", methods=["POST"])
    def test_connection():
        from .uploaders import build_uploader

        cfg = load_config()
        try:
            uploader = build_uploader(cfg)
            uploader.connect()
            uploader.close()
            message = '<p class="ok">Connection succeeded.</p>'
        except Exception as exc:
            message = f'<p class="fail">Connection failed: {exc}</p>'
        return redirect(url_for("config_page") + "#result") if False else render(
            message + '<a href="' + url_for("config_page") + '">Back</a>'
        )

    @app.route("/send-now", methods=["POST"])
    def send_now():
        send_now_callback()
        return redirect(url_for("status_page"))

    @app.route("/calibrate")
    def calibrate_page():
        body = """
        <h2>Frequency calibration</h2>
        <p>Sweeps a narrow band around the nominal Channel 1 frequency looking for a
        decodable signal, dwelling ~17-21s per test point. The sweep stops early if it
        hits an exact <code>freqCorr=0</code> (as precisely centered as this tool can
        measure) -- otherwise it runs the full range and <strong>you</strong> pick which
        OK hit to use as the Channel 1 baseline below; the other 4 EU channels are then
        derived from it using Davis's fixed 120kHz channel spacing.</p>
        <p><strong>This pauses normal packet reception for the duration of the sweep</strong>
        -- the RTL-SDR can only be used by one process at a time.</p>

        <fieldset>
        <legend>Sweep range (Hz)</legend>
        <label>Start frequency</label><input id="startfreq" value="868100000">
        <label>End frequency</label><input id="endfreq" value="868140000">
        <label>Step</label><input id="stepfreq" value="1000">
        <label>Gain (tenths of dB; 0 = AGC/auto)</label>
        <input id="gain" value="{{ current_gain }}">
        <button onclick="startSweep()">Start sweep</button>
        <button onclick="cancelSweep()">Cancel</button>
        </fieldset>

        <div id="sweep-status"></div>

        <fieldset>
        <legend>Channel 1 baseline</legend>
        <p>Click "Use this" on any OK row above, or type a frequency in directly.</p>
        <label>Channel 1 frequency (Hz)</label><input id="basefreq" oninput="previewChannels()">
        <div id="channel-preview"></div>
        <form method="post" action="{{ url_for('calibrate_apply') }}" onsubmit="return fillChannels(event)">
        <input type="hidden" id="channels-field" name="channels">
        <label>Comment (recorded in protocol.go)</label>
        <input id="comment" name="comment">
        <button type="submit">Apply &amp; rebuild rtldavis</button>
        </form>
        </fieldset>

        <script>
        const SPACING = {{ spacing }};
        const COUNT = {{ count }};

        function deriveChannels(base) {
            const chans = [];
            for (let i = 0; i < COUNT; i++) { chans.push(base + i * SPACING); }
            return chans;
        }

        function useFreq(freq) {
            document.getElementById('basefreq').value = freq;
            previewChannels();
        }

        function previewChannels() {
            const base = parseInt(document.getElementById('basefreq').value, 10);
            const el = document.getElementById('channel-preview');
            if (!base) { el.innerHTML = ''; return; }
            el.innerHTML = '<p>Derived: ' + deriveChannels(base).join(', ') + '</p>';
        }

        function fillChannels(ev) {
            const base = parseInt(document.getElementById('basefreq').value, 10);
            if (!base) {
                alert('Pick or enter a Channel 1 frequency first.');
                ev.preventDefault();
                return false;
            }
            document.getElementById('channels-field').value = deriveChannels(base).join(',');
            if (!document.getElementById('comment').value) {
                document.getElementById('comment').value =
                    'EU measured ' + new Date().toISOString().slice(0,10).replace(/-/g,'');
            }
            return true;
        }

        function fmtPoint(p) {
            if (p.ok) {
                return `<tr><td>${p.test_number}</td><td>${p.frequency}</td>` +
                       `<td class="ok">OK</td><td>${p.freq_corr}</td>` +
                       `<td><button type="button" onclick="useFreq(${p.frequency})">Use this</button></td></tr>`;
            }
            return `<tr><td>${p.test_number}</td><td>${p.frequency}</td>` +
                   `<td class="fail">NOK</td><td>-</td><td></td></tr>`;
        }

        function render(s) {
            let html = `<p>Running: ${s.running} &nbsp; Finished: ${s.finished}</p>`;
            if (s.error) { html += `<p class="fail">Error: ${s.error}</p>`; }
            html += '<table><tr><th>#</th><th>Frequency</th><th>Result</th><th>freqCorr</th><th></th></tr>';
            html += s.points.map(fmtPoint).join('');
            html += '</table>';
            if (s.finished && !s.points.some(p => p.ok)) {
                html += '<p class="fail">No decodable frequency found in this range.</p>';
            }
            document.getElementById('sweep-status').innerHTML = html;
        }

        function poll() {
            fetch('{{ url_for("calibrate_status") }}').then(r => r.json()).then(s => {
                render(s);
                if (s.running) { setTimeout(poll, 2000); }
            });
        }

        function startSweep() {
            const body = new URLSearchParams({
                startfreq: document.getElementById('startfreq').value,
                endfreq: document.getElementById('endfreq').value,
                stepfreq: document.getElementById('stepfreq').value,
                gain: document.getElementById('gain').value,
            });
            fetch('{{ url_for("calibrate_start") }}', {method: 'POST', body: body})
                .then(() => poll());
        }

        function cancelSweep() {
            fetch('{{ url_for("calibrate_cancel") }}', {method: 'POST'}).then(() => poll());
        }

        poll();
        </script>
        """
        from . import calibrate as _calibrate

        return render(
            render_template_string(
                body,
                spacing=_calibrate.CHANNEL_SPACING_HZ,
                count=_calibrate.CHANNEL_COUNT,
                current_gain=load_config()["rtldavis"].get("gain", 0),
            )
        )

    @app.route("/calibrate/start", methods=["POST"])
    def calibrate_start():
        startfreq = int(request.form["startfreq"])
        endfreq = int(request.form["endfreq"])
        stepfreq = int(request.form["stepfreq"])
        gain_raw = request.form.get("gain", "").strip()
        gain = int(gain_raw) if gain_raw else None
        start_calibration_sweep(startfreq, endfreq, stepfreq, gain)
        return jsonify({"ok": True})

    @app.route("/calibrate/status")
    def calibrate_status():
        return jsonify(get_calibration_status())

    @app.route("/calibrate/cancel", methods=["POST"])
    def calibrate_cancel():
        cancel_calibration_sweep()
        return jsonify({"ok": True})

    @app.route("/calibrate/apply", methods=["POST"])
    def calibrate_apply():
        channels = [int(c) for c in request.form["channels"].split(",")]
        comment = request.form.get("comment", "")
        ok, output = apply_calibration(channels, comment)
        if ok:
            message = '<p class="ok">Applied and rebuilt successfully. New binary is live on next packet-reader restart.</p>'
        else:
            message = f'<p class="fail">Failed:</p><pre>{output}</pre>'
        return render(message + f'<a href="{url_for("calibrate_page")}">Back</a>')

    return app


def run_webui(
    config_path: str,
    get_service_state,
    reload_service,
    send_now_callback,
    start_calibration_sweep,
    get_calibration_status,
    cancel_calibration_sweep,
    apply_calibration,
    get_current_channels,
    host: str,
    port: int,
):
    app = create_app(
        config_path,
        get_service_state,
        reload_service,
        send_now_callback,
        start_calibration_sweep,
        get_calibration_status,
        cancel_calibration_sweep,
        apply_calibration,
        get_current_channels,
    )
    thread = threading.Thread(
        target=lambda: app.run(host=host, port=port, use_reloader=False),
        daemon=True,
    )
    thread.start()
    return thread
