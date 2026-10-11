"""Readings from AbletonMCP Analyzer devices (m4l_device.py), over UDP.

Each device sends one OSC message ten times a second to 127.0.0.1:9880:
the name of its track, Max's id for it, peaks and RMS per channel, the mean
of L×R, the RMS of mid and side, and the RMS of ten octave bands of the mid
signal. A daemon thread keeps the last minute of frames per device; `summary` turns frames into
dB, correlation, width and an approximate BS.1770 loudness.

Loudness: BS.1770 weights each channel's mean square by the K filter. The
device has no K filter, but it has the spectrum: the K filter's power gain
at each band's centre, averaged over the band powers, scales the channels'
mean squares. On a test mix played through Live 12.4 it read -14.7 LUFS where
the offline BS.1770 measurement of the same file gave -14.65, correlation
0.96 (0.958) and side/mid -16.7 dB (-16.65); the bands matched octave
filters within ~1 dB. Still a monitor, not a delivery meter: render_and_analyze
measures exactly.
"""

from __future__ import annotations

import logging
import math
import os
import socket
import struct
import threading
import time
from collections import deque
from functools import lru_cache

from .m4l_device import ADDRESS, ADDRESS_PREFIX, BAND_START_HZ, BANDS, PORT

logger = logging.getLogger("AbletonMCPServer")

FIELDS = ("peak_l", "peak_r", "rms_l", "rms_r", "mean_lr", "rms_mid", "rms_side")
BAND_HZ = [BAND_START_HZ * 2 ** i for i in range(BANDS)]
KEEP_SECONDS = 60


def parse_osc(packet: bytes) -> tuple[str, list] | None:
    """(address, arguments) of one OSC message, or None if it is not one."""
    def string(pos: int) -> tuple[str, int]:
        end = packet.index(b"\x00", pos)
        return packet[pos:end].decode("utf-8", "replace"), (end + 4) & ~3

    try:
        address, pos = string(0)
        tags, pos = string(pos)
        if not address.startswith("/") or not tags.startswith(","):
            return None
        args: list = []
        for tag in tags[1:]:
            if tag == "i":
                args.append(struct.unpack(">i", packet[pos:pos + 4])[0])
                pos += 4
            elif tag == "f":
                args.append(struct.unpack(">f", packet[pos:pos + 4])[0])
                pos += 4
            elif tag == "d":
                args.append(struct.unpack(">d", packet[pos:pos + 8])[0])
                pos += 8
            elif tag == "s":
                value, pos = string(pos)
                args.append(value)
            elif tag in "TF":
                args.append(tag == "T")
            else:
                return None
        return address, args
    except (ValueError, struct.error, IndexError):
        return None


class Listener:
    """Collects frames from every analyzer, per device id."""

    def __init__(self, port: int = PORT) -> None:
        self.port = port
        self.frames: dict[int, deque] = {}
        self.names: dict[int, str] = {}  # device → its track's name, as last sent
        self.outdated_at = -1e9  # when a device from an older build last spoke
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.packets = 0

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("127.0.0.1", self.port))
        except OSError as e:
            sock.close()
            raise RuntimeError(f"Cannot listen on UDP port {self.port} for the analyzer ({e}); "
                               "is another AbletonMCP server running?") from e
        sock.settimeout(0.5)
        self.thread = threading.Thread(target=self._run, args=(sock,), daemon=True,
                                       name="analyzer-listener")
        self.thread.start()

    def _run(self, sock: socket.socket) -> None:
        while True:
            try:
                packet, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as e:
                self.error = str(e)
                return
            self.feed(packet, time.monotonic())

    def feed(self, packet: bytes, now: float) -> None:
        message = parse_osc(packet)
        if message is None or not message[0].startswith(ADDRESS_PREFIX):
            return
        if message[0] != ADDRESS or len(message[1]) != 2 + len(FIELDS) + BANDS:
            self.outdated_at = now  # a copy of an older build of the device
            return
        track, device_id, *values = message[1]
        with self.lock:
            self.packets += 1
            self.names[int(device_id)] = str(track)
            frames = self.frames.setdefault(int(device_id), deque(maxlen=KEEP_SECONDS * 20))
            frames.append((now, [float(v) for v in values]))

    def since(self, start: float) -> dict[str, list[list[float]]]:
        """Frames newer than `start`, by track name (two analyzers on tracks of
        the same name are told apart by their device id)."""
        with self.lock:
            fresh = {device: [v for t, v in frames if t >= start]
                     for device, frames in self.frames.items()}
            names = dict(self.names)
        fresh = {device: frames for device, frames in fresh.items() if frames}
        counts: dict[str, int] = {}
        for device in fresh:
            counts[names[device]] = counts.get(names[device], 0) + 1
        return {(names[d] if counts[names[d]] == 1 else f"{names[d]} (device {d})"): frames
                for d, frames in fresh.items()}

    def last_seen(self) -> dict[str, float]:
        """When each track's analyzer last sent something."""
        with self.lock:
            return {self.names[device]: frames[-1][0]
                    for device, frames in self.frames.items() if frames}


_listener: Listener | None = None


def listener() -> Listener:
    global _listener
    if _listener is None:
        _listener = Listener(int(os.environ.get("ABLETON_MCP_ANALYZER_PORT", PORT)))
    _listener.start()
    return _listener


@lru_cache(maxsize=1)
def k_weights() -> list[float]:
    """Power gain of the BS.1770 K filter at each band's centre (48 kHz design)."""
    from scipy.signal import freqz

    shelf = ([1.53512485958697, -2.69169618940638, 1.19839281085285],
             [1.0, -1.69065929318241, 0.73248077421585])
    highpass = ([1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])
    gains = []
    for hz in BAND_HZ:
        _, h1 = freqz(*shelf, worN=[hz], fs=48000)
        _, h2 = freqz(*highpass, worN=[hz], fs=48000)
        gains.append(float(abs(h1[0] * h2[0]) ** 2))
    return gains


def _db(power: float, floor: float = -120.0) -> float:
    return round(max(floor, 10 * math.log10(power)) if power > 0 else floor, 1)


def summary(frames: list[list[float]]) -> dict:
    """Levels, loudness, stereo and spectrum of one device's frames (dB / LUFS)."""
    import numpy as np

    data = np.asarray(frames, dtype=float)
    peak_l, peak_r, rms_l, rms_r, mean_lr, rms_mid, rms_side = data[:, :len(FIELDS)].T
    bands = data[:, len(FIELDS):] ** 2
    weights = np.asarray(k_weights())
    band_total = bands.sum(axis=1)
    factor = np.where(band_total > 0, (bands * weights).sum(axis=1) / np.maximum(band_total, 1e-30),
                      1.0)
    power = (rms_l ** 2 + rms_r ** 2) * factor  # K-weighted, summed over channels

    def lufs(values) -> float | None:
        mean = float(np.mean(values)) if len(values) else 0.0
        return round(-0.691 + 10 * math.log10(mean), 1) if mean > 1e-12 else None

    momentary = [lufs(power[i:i + 4]) for i in range(0, max(1, len(power) - 3))]
    momentary = [m for m in momentary if m is not None]
    product = rms_l * rms_r
    live = product > 1e-10
    correlations = mean_lr[live] / product[live]
    result = {
        "frames": len(data),
        "peak_dbfs": _db(float(np.max(np.maximum(peak_l, peak_r))) ** 2),
        "rms_dbfs": _db(float(np.mean((rms_l ** 2 + rms_r ** 2) / 2))),
        "loudness_lufs": lufs(power),
        "max_momentary_lufs": max(momentary) if momentary else None,
        "correlation": round(float(np.clip(np.average(correlations, weights=product[live]), -1, 1)), 2)
        if live.any() else None,
        "lowest_correlation": round(float(np.clip(correlations.min(), -1, 1)), 2)
        if live.any() else None,
        "side_to_mid_db": round(_db(float(np.mean(rms_side ** 2)))
                                - _db(float(np.mean(rms_mid ** 2))), 1)
        if np.mean(rms_mid ** 2) > 0 else None,
        "bands_db": {_band_label(hz): _db(float(np.mean(bands[:, i])))
                     for i, hz in enumerate(BAND_HZ)},
    }
    result["observations"] = observations(result)
    return result


def _band_label(hz: float) -> str:
    return f"{hz / 1000:g} kHz" if hz >= 1000 else f"{hz:g} Hz"


def observations(result: dict) -> list[str]:
    notes = []
    if result["loudness_lufs"] is None:
        return ["Silent: nothing played through this analyzer while listening."]
    correlation = result["correlation"]
    if correlation is not None and correlation < 0:
        notes.append(f"Correlation {correlation}: left and right largely cancel in mono "
                     "(a phase or polarity problem).")
    elif correlation is not None and correlation < 0.3:
        notes.append(f"Correlation {correlation}: very wide; check how it sounds in mono.")
    if result["peak_dbfs"] > -0.1:
        notes.append(f"Peaks at {result['peak_dbfs']} dBFS: clipping or no headroom.")
    bands = result["bands_db"]
    values = list(bands.values())
    loudest = max(values)
    low_mid = max(bands["250 Hz"], bands["500 Hz"])
    if low_mid >= loudest - 1 and low_mid > max(bands["62.5 Hz"], bands["125 Hz"]) + 3:
        notes.append("250–500 Hz is the strongest region: can sound muddy or boxy.")
    if loudest - max(bands["8 kHz"], bands["16 kHz"]) > 30:
        notes.append("Very little above 8 kHz: dull or dark.")
    return notes
