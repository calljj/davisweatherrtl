"""Packet sources: abstract interface plus the rtldavis (RTL-SDR) implementation.

rtldavis (github.com/lheijst/rtldavis, EU-capable fork) logs via Go's
standard `log` package with log.SetFlags(log.Lmicroseconds) and no output
redirection, so ALL of its output -- diagnostics and data packets alike --
goes to stderr, not stdout (confirmed by reading main.go and by running the
built binary standalone on this Pi). A received Davis packet is logged as:

    HH:MM:SS.ffffff <16 hex chars = 8 raw packet bytes> <counters...> msg.ID=<N>

e.g. "11:23:45.123456 8004700F990091AB 12 34 56 78 9 msg.ID=1"

RtldavisSource spawns the binary with stderr merged into stdout and matches
that line shape to extract the 8 raw packet bytes.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
from abc import ABC, abstractmethod
from queue import Empty, Queue
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

DATA_LINE_RE = re.compile(r"^\d\d:\d\d:\d\d\.\d{6}\s+([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})\s+\d")


class PacketSource(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def packets(self) -> Iterator[list[int]]:
        """Yield raw 8-byte Davis packets as they arrive. Blocks between packets."""
        ...

    @abstractmethod
    def stop(self) -> None: ...


class RtldavisSource(PacketSource):
    def __init__(
        self,
        bin_path: str,
        region: str = "EU",
        ppm: int = 0,
        extra_args: Optional[list[str]] = None,
        transmitters: int = 255,
        maxmissed: int = 4,
        log_undefined: bool = False,
        gain: int = 0,
    ):
        self.bin_path = bin_path
        self.region = region
        self.ppm = ppm
        self.extra_args = extra_args or []
        # Tuner gain in tenths of a dB (e.g. 207 = 20.7dB); 0 = AGC (auto
        # gain). Only specific steps are supported by the R828D tuner --
        # see `rtldavis -tf EU -v` startup log for the exact list.
        self.gain = gain
        # -tr is a bitmask of which Davis transmitter IDs to track at the RF
        # layer (bit0=ID0 ... bit7=ID7); rtldavis itself defaults to 1 (ID0
        # only). 255 listens for all 8 IDs since the physical ISS's DIP
        # switch setting isn't known in advance -- the app-level
        # station.transmitter_id filter (in __main__.py) narrows it down
        # once the real ID has been confirmed via the web UI's signal
        # indicator.
        self.transmitters = transmitters
        # rtldavis defaults to 51 (meant for testing); real deployments
        # should resync faster after missed packets.
        self.maxmissed = maxmissed
        self.log_undefined = log_undefined
        self._process: Optional[subprocess.Popen] = None
        self._queue: "Queue[str]" = Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_requested = False

    def start(self) -> None:
        cmd = [self.bin_path, "-tf", self.region, "-tr", str(self.transmitters), "-maxmissed", str(self.maxmissed)]
        if self.ppm:
            cmd += ["-ppm", str(self.ppm)]
        if self.gain:
            cmd += ["-gain", str(self.gain)]
        if self.log_undefined:
            cmd += ["-u"]
        cmd += self.extra_args

        logger.info("starting rtldavis: %s", " ".join(cmd))
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
            universal_newlines=True,
        )
        self._stop_requested = False
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            if self._stop_requested:
                break
            self._queue.put(line.rstrip("\n"))
        logger.warning("rtldavis process output stream ended")

    def packets(self) -> Iterator[list[int]]:
        while not self._stop_requested:
            try:
                line = self._queue.get(timeout=1.0)
            except Empty:
                if self._process is not None and self._process.poll() is not None:
                    logger.error("rtldavis process exited with code %s", self._process.returncode)
                    return
                continue

            match = DATA_LINE_RE.match(line)
            if not match:
                if "undefined:" in line:
                    # Only logged when log_undefined=True. Proves the RF
                    # chain is receiving *something* on the Davis protocol's
                    # timing/preamble even if it doesn't match a tracked ID --
                    # useful to distinguish "no antenna/range/frequency issue"
                    # from "wrong transmitter ID" while troubleshooting.
                    logger.info("rtldavis undefined signal: %s", line)
                else:
                    logger.debug("rtldavis (non-data): %s", line)
                continue

            packet = [int(match.group(i), 16) for i in range(1, 9)]
            yield packet

    def stop(self) -> None:
        self._stop_requested = True
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2)
