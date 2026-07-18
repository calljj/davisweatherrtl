# davis-clientraw

Reads decoded Davis Vantage Pro2 ISS packets via an RTL-SDR dongle, maintains
live conditions and history, and uploads the four Weather Display
`clientraw*.txt` files on independent schedules. Includes a local web UI for
configuration, live status, and a guided frequency-calibration tool, plus an
optional independent tide-prediction feature.

## Features

- Decodes Davis ISS telemetry (temp, humidity, wind, rain, solar, UV) straight
  from RF via `rtldavis` -- no Davis console/Envoy required on the receiving
  end, though one can coexist on the same ISS.
- Generates all four Weather Display files (`clientraw.txt`,
  `clientrawextra.txt`, `clientrawhour.txt`, `clientrawdaily.txt`), each on
  its own independent generate/upload schedule so a slow upload never delays
  local file freshness.
- Uploads over SFTP, SCP, or FTP, with automatic reconnection.
- Local web UI (`/config`, `/status`, `/preview`, `/calibrate`) -- no
  authentication, bind to your LAN only.
- **Guided frequency calibration**: cheap RTL-SDR dongles commonly have
  enough crystal error that Davis's nominal EU channel frequencies won't
  decode out of the box. The `/calibrate` page sweeps a narrow band, shows
  you every hit, and lets you pick the one to use -- then writes and rebuilds
  `rtldavis` automatically. See [Frequency calibration](#frequency-calibration)
  below.
- Optional independent tide-prediction feature (XTide-based).
- Runs as a systemd service, survives reboots and dongle disconnects.

## Quick start

```
git clone <this repo> davis-clientraw
cd davis-clientraw
./install.sh          # NOT sudo -- it calls sudo internally where needed
```

The installer builds `librtlsdr` and `rtldavis` from source, sets up a Python
virtualenv, writes a starter `config.json` (if one doesn't already exist),
and installs+enables the systemd service. It does **not** start the service
automatically -- edit `config.json` first (station details, upload
credentials), or start it and use the web UI:

```
sudo systemctl start davis-clientraw
```

Then open `http://<pi-host>:8080/status`. If it shows **NO SIGNAL**, that's
expected on a fresh install -- go straight to
[Frequency calibration](#frequency-calibration).

## Hardware

- Any RTL2832U-based RTL-SDR dongle. Tested with a generic R820T unit and an
  RTL-SDR Blog V4 (R828D) -- both work identically once `librtlsdr` is built
  correctly (see below).
- A Davis Vantage Pro2 (or compatible) ISS, EU 868MHz band. US/NZ frequency
  tables also exist in the vendored `rtldavis` source but are untested by
  this project.
- Antenna: anything resonant near 868MHz and correctly polarized/aimed will
  do; a directional antenna helps at range but isn't required at short range.

### Why librtlsdr matters (read this before "simplifying" the installer)

`rtldavis` needs the **upstream osmocom `librtlsdr`**
(github.com/steve-m/librtlsdr), built from source. Two other options that
look equally reasonable both silently fail:

- **The stock Debian/Raspbian package** (`apt install rtl-sdr`, pulling in
  `librtlsdr0`) -- and any leftover copy of it, since a `dpkg` dependency
  from an unrelated package (e.g. a SoapySDR module) can quietly reinstall
  it later and get linked in preference to a correctly-built one at
  `/usr/local/lib`. Check with `ldd $(which rtldavis) | grep rtlsdr` -- it
  must resolve to `/usr/local/lib/librtlsdr.so.0`, not a `/lib/*-linux-gnu/`
  path.
- **RTL-SDR Blog's own driver fork** -- correct for some of their other
  tools, but not what made reception work here.

The failure mode with the wrong library is the worst kind: no errors are
ever logged, the dongle opens fine, `rtldavis` runs and hops channels
normally -- it just never decodes a single packet, indefinitely, even with a
strong, correctly-tuned, confirmed-present signal. `install.sh` handles this
correctly (removes the stock package, builds from `steve-m/librtlsdr`); if
you're troubleshooting a from-scratch reception problem, verify this first.

### Frequency calibration

Even with the correct library, Davis's nominal EU channel frequencies
(868.077250 / .197250 / .317250 / .437250 / .557250 MHz) are often off by
several kHz to tens of kHz on a given dongle, due to ordinary crystal
tolerance -- and it drifts with temperature, so a value measured once may
need rechecking later. Rather than compute a `-fc`/`-ppm` runtime correction,
this project measures the true Channel 1 frequency directly and bakes the
whole 5-channel table (spaced a fixed 120kHz apart, per the Davis spec)
straight into the `rtldavis` Go source, then rebuilds it in place.

Use the web UI's **`/calibrate`** page:

1. Set a sweep range (defaults to ±20kHz around nominal Channel 1) and start
   it. Each test point dwells ~17-21s; this **pauses normal packet
   reception** for the duration, since the RTL-SDR can only be used by one
   process at a time.
2. Each result streams in live: `OK` hits show the residual frequency error
   (`freqCorr`) at that point; the sweep stops early if it finds an exact
   `freqCorr=0`.
3. Pick any `OK` hit (or type a frequency directly) as your Channel 1
   baseline -- the other 4 channels are derived automatically using Davis's
   fixed 120kHz spacing.
4. **Apply & rebuild**: writes the new table into `govendor/.../protocol.go`
   and rebuilds `bin/rtldavis`. Reception resumes automatically afterward.

The current live channel table is always shown on the `/status` page.

If nothing decodes anywhere in a wide sweep, that points to something other
than frequency calibration -- confirm signal is actually present with
`rtl_power -f 868.0M:868.6M:1k -g 40 -i 1 -e 40 out.csv` and inspect the
result for periodic (~2.5s) bursts near the 5 nominal channels before
assuming a wider search range will help.

## Wireless repeater support (this branch only, experimental)

**This is on the `repeater-decode` branch, not `master` -- it's not merged
into the main product yet.** Status: the core decode is confirmed working
against live traffic; one detail (what the repeater-info bytes actually
mean) is still unsolved.

Davis's classic 8-byte packet format and CRC only describe direct
ISS-to-console transmissions. A repeater relaying that data adds 2 extra
bytes to the over-the-air frame and folds them into a *different* CRC
formula -- a receiver that only implements the classic formula (as this
project did before this branch) will never validate a repeater-relayed
packet, silently, with no error logged. This isn't documented anywhere by
Davis; the closest public reference is the DavisRFM69 project's wiki, which
confirms the extra bytes exist but not how they fold into the CRC.

**Confirmed 2026-07-18** against 30 consecutive real packets from a live
station-2-via-repeater-A capture (10ft range, zero false positives, every
decoded value physically sane): the CRC is computed over
`[header, 5 data bytes, 2 repeater-info bytes]`, with the CRC's own 2 bytes
moved to the *end* of that sequence for validation rather than their
natural transmitted-frame position in the middle. See
`vendor/rtldavis/protocol/protocol.go` (`repeaterHypotheses`) and
`davis_clientraw/davis_decode.py` (`is_valid_repeated_packet`) for the Go
and Python implementations respectively -- both independently confirmed
against the same real captures.

**Not yet solved**: the 2 repeater-info bytes aren't a static per-repeater
identifier like a "repeater A/B/C" letter. Empirically (20/20 samples, no
exceptions) they correlate deterministically with the packet's
`message_type` nibble instead (types 4/10/14 -> one value, types 5/8 ->
another, in one capture). What they actually encode -- and whether that
holds across different repeaters/firmware -- is open.

What's wired up end-to-end on this branch: `rtldavis` captures a wider
window (96 symbols instead of 80, to actually see the extra bytes) and
tries the confirmed CRC formula (plus a few unconfirmed fallbacks) on
anything that fails the classic check; `source_rtldavis.py` parses the
extra `Repeated=`/`RepeaterInfo=` fields from its log output;
`davis_decode.is_valid_repeated_packet` independently re-verifies in
Python; `__main__.py` accepts repeater packets into the same
`clientraw*.txt` pipeline as direct ones; the status page shows "via
repeater" when the most recent packet arrived that way.

## What the ISS provides vs. console-only

From ISS packets: outdoor temp, humidity, wind speed/gust/direction, rain
(tip counter), solar, UV. **Not available**: barometric pressure, indoor
temp/humidity, forecast icon (Davis console-only). Pressure comes from an
optional local BME280 (`pressure.source: "bme280"`) or a configured
placeholder; indoor/forecast fields stay at their template sample values.

## Configuration

Edit `config.json` directly, or use the web UI at `http://<pi-host>:8080/config`
(saves and hot-reloads the running service without a restart). Covers
transport (SFTP/SCP/FTP) credentials, remote paths, per-file upload
intervals, rtldavis invocation, station details (including the Davis station
number, 1-8, matching your console/DIP switch), and units.

**The web UI has no authentication -- bind/expose it on your LAN only.**
`config.json` is chmod 600 after each save since it holds credentials, and
is excluded from this repo via `.gitignore` -- copy `config.example.json` if
you need to regenerate it (`install.sh` does this automatically on first
run).

## Tide predictions (tideprediction.html)

Independent of the Davis ISS pipeline: once every 24h, generates a 5-day
tide prediction and uploads it as `tideprediction.html` to the same
server/transport as the weather files (or a separately-configured folder).
Disabled by default (`tide.enabled: false` in `config.example.json`) since
it needs XTide built separately:

```
sudo apt-get install -y build-essential autoconf automake libtool libpng-dev pkg-config
mkdir -p ~/src && cd ~/src
curl -sLO https://flaterco.com/files/xtide/libtcd-2.2.7-r3.tar.xz
curl -sLO https://flaterco.com/files/xtide/xtide-2.16.tar.xz
tar xf libtcd-2.2.7-r3.tar.xz && tar xf xtide-2.16.tar.xz

cd libtcd-2.2.7 && ./configure --prefix=/usr/local && make && sudo make install
cd ../xtide-2.16
./configure --without-x CPPFLAGS="-I/usr/local/include" LDFLAGS="-L/usr/local/lib"
make && sudo make install
echo "/usr/local/lib" | sudo tee /etc/ld.so.conf.d/usrlocal.conf && sudo ldconfig
```

XTide's current public "free" harmonics package (`harmonics-dwf-*.tar.xz`)
is US/NOS-only. UK stations -- including "Portsmouth, England" (this
project's default) -- come from the UK's Proudman Oceanographic Laboratory
via a 2003 permission letter (see https://flaterco.com/xtide/pol.html), and
that data was last bundled in an older harmonics build distributed with
WXTide32:

```
curl -sLO http://svhorizon.com/wxtide32/wxtdata/wxtide47m.zip
unzip wxtide47m.zip harmonics-2004-06-14.tcd -d tide-data/
```

Verify the station is findable and dates/times look sane before trusting it:

```
export HFILE_PATH=tide-data/harmonics-2004-06-14.tcd
tide -l "Portsmouth, England" -b "$(date +%Y-%m-%d) 00:00" -e "$(date -d +5days +%Y-%m-%d) 00:00" -u m
```

Then set `tide.enabled: true` in `config.json` (or via the web UI) and
restart the service. Moonrise/moonset times can differ by ~1 minute from
other sources due to the harmonics data's vintage (2004) -- that's normal,
not a bug. Different station: change `tide.station` to any name findable in
your harmonics file.

## Running tests

```
venv/bin/python3 -m pytest tests/ -v
```

## License

The `davis_clientraw` Python package is MIT-licensed (see `LICENSE`).

`vendor/rtldavis/` is a separately-licensed component (GPLv3, originally by
Douglas Hall, EU support and further changes by Luc Heijst and other
contributors -- see `vendor/rtldavis/LICENSE` and the file headers) that
runs as an independent subprocess, not linked into the Python code.
