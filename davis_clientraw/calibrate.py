"""Frequency calibration: sweep for the true Channel-1 center frequency and
derive the rest of the 5-channel EU table from it, then write the result
into rtldavis's Go source and rebuild.

Davis's 5 EU channels are evenly spaced 120kHz apart (confirmed against two
independent on-site measurements: 868113000/868233000/868353000/868473000/
868593000). Once the true Channel-1 frequency is known, the other 4 follow
directly -- no need to sweep the whole band.

The sweep itself reuses rtldavis's own -startfreq/-endfreq/-stepfreq test
mode, which dwells one full "Init channels" period (~17-21s) per test point
and logs either "TESTFREQ N: Frequency F: NOK" or "TESTFREQ N: Frequency F
(freqCorr=E): OK, ...". Among OK hits, the one with freqCorr closest to zero
is the most precisely centered -- freqCorr is the residual frequency error
measured on that decode, so a hit at the exact right frequency should show
little to no residual error, while an OK hit that's merely "close enough"
for the demodulator's tolerance will show a larger one.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

TESTFREQ_OK_RE = re.compile(r"TESTFREQ (\d+): Frequency (\d+) \(freqCorr=(-?\d+)\): OK")
TESTFREQ_NOK_RE = re.compile(r"TESTFREQ (\d+): Frequency (\d+): NOK")

CHANNEL_SPACING_HZ = 120000
CHANNEL_COUNT = 5

EU_CHANNELS_RE = re.compile(
    r'(if tf == "EU" \{\s*\n\s*p\.channels = \[\]int\{\s*\n\s*)'
    r'[0-9,\s]+,\s*//[^\n]*\n'
    r'(\s*\})'
)

# US is a different reception model to EU: 51 channels in a pseudo-random hop
# pattern instead of a fixed 5-channel table, so there's no fixed spacing to
# derive the rest of the table from a single measured point the way EU's
# CHANNEL_SPACING_HZ does. Instead: the nominal table below is the same one
# baked into protocol.go, and calibration applies one uniform offset (the
# measured Channel-0 error) across all 51 nominal values -- physically
# equivalent to what EU's fixed-spacing math does, since a dongle's crystal
# error is effectively constant across this narrow a band.
US_NOMINAL_CHANNELS = [
    902419338, 902921088, 903422839, 903924589, 904426340, 904928090,
    905429841, 905931591, 906433342, 906935092, 907436843, 907938593,
    908440344, 908942094, 909443845, 909945595, 910447346, 910949096,
    911450847, 911952597, 912454348, 912956099, 913457849, 913959599,
    914461350, 914963100, 915464850, 915966601, 916468351, 916970102,
    917471852, 917973603, 918475353, 918977104, 919478854, 919980605,
    920482355, 920984106, 921485856, 921987607, 922489357, 922991108,
    923492858, 923994609, 924496359, 924998110, 925499860, 926001611,
    926503361, 927005112, 927506862,
]
US_CHANNEL_COUNT = len(US_NOMINAL_CHANNELS)

US_CHANNELS_RE = re.compile(
    r'(// davis-clientraw-us-channels[^\n]*\n(?:[^\n]*\n)*?\s*)'
    r'[0-9,\s]+,\s*//[^\n]*\n'
    r'(\s*\})'
)


@dataclass
class SweepPoint:
    test_number: int
    frequency: int
    ok: bool
    freq_corr: Optional[int] = None


@dataclass
class SweepState:
    running: bool = False
    finished: bool = False
    error: Optional[str] = None
    points: list = field(default_factory=list)


class Calibrator:
    """Runs an rtldavis frequency sweep in a background thread and tracks
    progress so the web UI can poll it. Only one sweep may run at a time;
    the caller is responsible for making sure nothing else (the normal
    packet-reader source) is holding the RTL-SDR device concurrently."""

    def __init__(self, bin_path: str, region: str, transmitters: int, gain: int):
        self.bin_path = bin_path
        self.region = region
        self.transmitters = transmitters
        self.gain = gain
        self._lock = threading.Lock()
        self._state = SweepState()
        self._process: Optional[subprocess.Popen] = None

    def start(self, startfreq: int, endfreq: int, stepfreq: int) -> None:
        with self._lock:
            if self._state.running:
                raise RuntimeError("a sweep is already running")
            self._state = SweepState(running=True)
        threading.Thread(
            target=self._run, args=(startfreq, endfreq, stepfreq), daemon=True
        ).start()

    def stop(self) -> None:
        if self._process is not None:
            self._process.terminate()

    def status(self) -> dict:
        with self._lock:
            s = self._state
            return {
                "running": s.running,
                "finished": s.finished,
                "error": s.error,
                "points": [vars(p) for p in s.points],
            }

    def _run(self, startfreq: int, endfreq: int, stepfreq: int) -> None:
        cmd = [
            self.bin_path,
            "-tf", self.region,
            "-tr", str(self.transmitters),
            "-startfreq", str(startfreq),
            "-endfreq", str(endfreq),
            "-stepfreq", str(stepfreq),
            "-v",
        ]
        if self.gain:
            cmd += ["-gain", str(self.gain)]
        logger.info("starting calibration sweep: %s", " ".join(cmd))
        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                universal_newlines=True,
            )
            assert self._process.stdout is not None
            for line in self._process.stdout:
                self._handle_line(line.rstrip("\n"))
            self._process.wait(timeout=10)
        except Exception as exc:
            logger.exception("calibration sweep failed")
            with self._lock:
                self._state.error = str(exc)
        finally:
            with self._lock:
                self._state.running = False
                self._state.finished = True
            self._process = None

    def _handle_line(self, line: str) -> None:
        m = TESTFREQ_OK_RE.search(line)
        if m:
            point = SweepPoint(
                test_number=int(m.group(1)),
                frequency=int(m.group(2)),
                ok=True,
                freq_corr=int(m.group(3)),
            )
            with self._lock:
                self._state.points.append(point)
            if point.freq_corr == 0:
                # A literal zero residual is as precisely centered as this
                # tool can measure -- no point scanning the rest of the
                # range. Stop the sweep here; the user still picks which
                # point to apply from the GUI, same as any other result.
                logger.info(
                    "exact freqCorr=0 hit at %d Hz, stopping sweep early", point.frequency
                )
                self.stop()
            return
        m = TESTFREQ_NOK_RE.search(line)
        if m:
            point = SweepPoint(
                test_number=int(m.group(1)), frequency=int(m.group(2)), ok=False
            )
            with self._lock:
                self._state.points.append(point)


def write_protocol_go(protocol_go_path: str, channels: list[int], comment: str) -> None:
    """Replaces the EU channel table in protocol.go with the given
    frequencies. Only touches the EU block; US/NZ tables are untouched."""
    with open(protocol_go_path) as f:
        content = f.read()

    freq_line = ", ".join(str(c) for c in channels) + f", // {comment}"
    new_content, n = EU_CHANNELS_RE.subn(rf"\g<1>{freq_line}\n\g<2>", content)
    if n != 1:
        raise ValueError(
            f"expected exactly 1 match for the EU channel table in {protocol_go_path}, found {n}"
        )

    tmp_path = protocol_go_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(new_content)
    os.replace(tmp_path, protocol_go_path)


def read_protocol_go_channels(protocol_go_path: str) -> list[int]:
    """Returns whatever EU channel frequencies are currently baked into
    protocol.go -- the ground truth for what the last-built binary actually
    listens on, regardless of how it got there (manual edit or a prior
    calibration apply)."""
    with open(protocol_go_path) as f:
        content = f.read()

    m = EU_CHANNELS_RE.search(content)
    if not m:
        raise ValueError(f"could not find the EU channel table in {protocol_go_path}")

    freq_line = content[m.end(1):m.start(2)]
    return [int(n) for n in re.findall(r"\d+", freq_line.split("//")[0])]


def write_us_protocol_go(protocol_go_path: str, channels: list[int], comment: str) -> None:
    """Replaces the US channel table in protocol.go with the given
    frequencies. Only touches the US block (marked by the
    'davis-clientraw-us-channels' comment); EU/NZ tables are untouched.
    Entirely separate from write_protocol_go/EU_CHANNELS_RE above -- by
    design, so a bug here can't affect the already-proven EU path."""
    if len(channels) != US_CHANNEL_COUNT:
        raise ValueError(f"expected {US_CHANNEL_COUNT} US channels, got {len(channels)}")

    with open(protocol_go_path) as f:
        content = f.read()

    freq_line = ", ".join(str(c) for c in channels) + f", // {comment}"
    new_content, n = US_CHANNELS_RE.subn(rf"\g<1>{freq_line}\n\g<2>", content)
    if n != 1:
        raise ValueError(
            f"expected exactly 1 match for the US channel table in {protocol_go_path}, found {n}"
        )

    tmp_path = protocol_go_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(new_content)
    os.replace(tmp_path, protocol_go_path)


def read_us_protocol_go_channels(protocol_go_path: str) -> list[int]:
    """Returns whatever US channel frequencies are currently baked into
    protocol.go. Mirrors read_protocol_go_channels but for the US block."""
    with open(protocol_go_path) as f:
        content = f.read()

    m = US_CHANNELS_RE.search(content)
    if not m:
        raise ValueError(f"could not find the US channel table in {protocol_go_path}")

    freq_line = content[m.end(1):m.start(2)]
    return [int(n) for n in re.findall(r"\d+", freq_line.split("//")[0])]


def us_offset_to_channels(offset_hz: int) -> list[int]:
    """Applies one uniform frequency offset (the measured error at nominal
    Channel 0) across all 51 nominal US channels. See the US_NOMINAL_CHANNELS
    comment above for why a single offset is physically valid here, same as
    EU's fixed-spacing derivation."""
    return [c + offset_hz for c in US_NOMINAL_CHANNELS]


def rebuild(source_dir: str, bin_dest: str, gopath: str) -> tuple[bool, str]:
    """Rebuilds rtldavis from source and installs to bin_dest. No sudo is
    needed as long as bin_dest is writable by the running user -- this is
    why the project keeps its own build output under bin/ instead of
    /usr/local/bin."""
    env = dict(os.environ)
    env["GO111MODULE"] = "off"
    env["GOPATH"] = gopath
    result = subprocess.run(
        ["go", "build", "-o", bin_dest, "."],
        cwd=source_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = result.stdout + result.stderr
    return result.returncode == 0, output
