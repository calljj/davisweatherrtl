#!/usr/bin/env bash
# davis-clientraw installer.
#
# Run as your normal user, NOT root/sudo -- it calls sudo internally only
# for the specific steps that actually need it (apt packages, kernel module
# blacklist, librtlsdr install, systemd service). Everything else (the Go
# build, the Python virtualenv) lives entirely under this project directory,
# owned by you, so re-running this script is always safe.
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user, not root/sudo -- it calls sudo internally" >&2
    echo "only for the specific steps that need it." >&2
    exit 1
fi

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOVENDOR="$PROJECT_DIR/govendor"
RTLDAVIS_SRC="$GOVENDOR/src/github.com/mdickers47/rtldavis"
BIN_DIR="$PROJECT_DIR/bin"

echo "==> davis-clientraw installer"
echo "    project dir: $PROJECT_DIR"
echo

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y \
    python3 python3-venv python3-pip \
    git cmake build-essential pkg-config libusb-1.0-0-dev \
    golang-go

# ---------------------------------------------------------------------------
# 2. RTL-SDR driver
#
# rtldavis needs the *upstream osmocom* librtlsdr (github.com/steve-m/librtlsdr),
# built from source. The stock Debian package (librtlsdr0) and some vendor
# forks are a known silent-failure mode here: no errors are ever logged,
# rtldavis just never decodes a single packet even with a strong, correctly
# tuned signal. This cost real debugging time to track down -- don't swap it
# out for "apt install rtl-sdr" to save a step.
# ---------------------------------------------------------------------------
echo "==> Blacklisting the DVB-T kernel driver (it claims the dongle before rtldavis can open it)"
sudo tee /etc/modprobe.d/blacklist-rtl.conf > /dev/null <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
sudo rmmod dvb_usb_rtl28xxu 2>/dev/null || true

echo "==> Removing the stock librtlsdr package if present (wrong library -- silent failure)"
sudo apt-get remove -y librtlsdr0 rtl-sdr 2>/dev/null || true

echo "==> Building librtlsdr from steve-m/librtlsdr (upstream osmocom)"
LIBRTLSDR_SRC="$HOME/src/librtlsdr"
mkdir -p "$(dirname "$LIBRTLSDR_SRC")"
if [[ -d "$LIBRTLSDR_SRC" ]]; then
    ( cd "$LIBRTLSDR_SRC" && git pull --ff-only ) || true
else
    git clone https://github.com/steve-m/librtlsdr.git "$LIBRTLSDR_SRC"
fi
mkdir -p "$LIBRTLSDR_SRC/build"
( cd "$LIBRTLSDR_SRC/build" && cmake .. -DINSTALL_UDEV_RULES=ON && make -j"$(nproc)" )
sudo make -C "$LIBRTLSDR_SRC/build" install
echo "/usr/local/lib" | sudo tee /etc/ld.so.conf.d/usrlocal.conf > /dev/null
sudo ldconfig

# ---------------------------------------------------------------------------
# 3. rtldavis -- vendored, self-contained Go source (vendor/rtldavis/),
#    starting at Davis's nominal EU channel frequencies. Use the web UI's
#    /calibrate page after first boot to measure your own dongle's real
#    offset and rewrite this table automatically -- see README.md.
# ---------------------------------------------------------------------------
echo "==> Building rtldavis"
mkdir -p "$(dirname "$RTLDAVIS_SRC")"
rm -rf "$RTLDAVIS_SRC"
cp -r "$PROJECT_DIR/vendor/rtldavis" "$RTLDAVIS_SRC"
mkdir -p "$BIN_DIR"
( cd "$RTLDAVIS_SRC" && GO111MODULE=off GOPATH="$GOVENDOR" go build -o "$BIN_DIR/rtldavis" . )
echo "    built: $BIN_DIR/rtldavis"
ldd "$BIN_DIR/rtldavis" | grep rtlsdr || true

# ---------------------------------------------------------------------------
# 4. Python environment
# ---------------------------------------------------------------------------
echo "==> Setting up Python virtualenv"
python3 -m venv "$PROJECT_DIR/venv"
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip --quiet
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 5. Config
# ---------------------------------------------------------------------------
if [[ ! -f "$PROJECT_DIR/config.json" ]]; then
    echo "==> Writing config.json from config.example.json (edit before starting the service)"
    sed \
        -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__GOPATH__|$GOVENDOR|g" \
        -e "s|__HOME__|$HOME|g" \
        "$PROJECT_DIR/config.example.json" > "$PROJECT_DIR/config.json"
    chmod 600 "$PROJECT_DIR/config.json"
else
    echo "==> config.json already exists, leaving it alone"
fi

# ---------------------------------------------------------------------------
# 6. systemd service
# ---------------------------------------------------------------------------
echo "==> Installing systemd service"
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"
sed \
    -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    -e "s|__USER__|$SERVICE_USER|g" \
    -e "s|__GROUP__|$SERVICE_GROUP|g" \
    "$PROJECT_DIR/davis-clientraw.service" | sudo tee /etc/systemd/system/davis-clientraw.service > /dev/null
sudo systemctl daemon-reload
sudo systemctl enable davis-clientraw

echo
echo "==> Done."
echo "    1. Edit $PROJECT_DIR/config.json (station details, upload credentials) --"
echo "       or start the service now and use the web UI at http://<host>:8080/config"
echo "    2. sudo systemctl start davis-clientraw"
echo "    3. Open http://<host>:8080/status -- if it shows NO SIGNAL, open"
echo "       http://<host>:8080/calibrate to measure your dongle's real frequency offset"
echo "    4. journalctl -u davis-clientraw -f to watch logs"
