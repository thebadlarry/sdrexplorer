# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Spectrum frames (module API): the band's FFT, shared, as Python callbacks.

The core already computes a log-power FFT of the whole band for the waterfall. A module that wants it (an
occupancy logger, a survey, a detector) subscribes here instead of adding its own FFT to the flowgraph:

    from sdrexplorer.api import spectrum

    def on_frame(f):            # an api.spectrum.SpectrumFrame, on the Qt thread: keep it cheap
        busy = f.db > f.db.mean() + 10
        ...
    spectrum.subscribe(on_frame, rate_hz=5)      # at most 5 frames/s (default: every frame, <= 20/s)
    spectrum.unsubscribe(on_frame)

Frames come while the radio receives, also when the waterfall is hidden (Memories page) or paused by a scan
sweep; none while transmitting. ``db`` is the display FFT (averaged, DC bin blanked, fft-shifted: bin 0 is the
lowest frequency); ``t`` is api.station.now() (the replayed file's time during replay).
"""
from __future__ import annotations

import sys
import time
import traceback
from dataclasses import dataclass

import numpy as np

MAX_RATE_HZ = 20.0


@dataclass
class SpectrumFrame:
    t: float                    # seconds since the epoch (station time)
    center_hz: int              # band center
    samp_rate: float            # capture rate = the frame's span
    db: np.ndarray              # float32 [NFFT] log power (dB, relative), lowest frequency first

    @property
    def nfft(self):
        return int(self.db.size)

    @property
    def bins_hz(self):
        """Absolute center frequency of every bin."""
        n = self.db.size
        return self.center_hz + (np.arange(n) / n - 0.5) * self.samp_rate

    def bin_of(self, freq_hz):
        n = self.db.size
        return int(np.clip(round(((freq_hz - self.center_hz) / self.samp_rate + 0.5) * n), 0, n - 1))


_subs = []                      # [fn, min_interval_s, last_t]


def subscribe(fn, rate_hz=None):
    unsubscribe(fn)
    r = min(float(rate_hz), MAX_RATE_HZ) if rate_hz else MAX_RATE_HZ
    _subs.append([fn, 1.0 / r if r > 0 else 0.0, 0.0])


def unsubscribe(fn):
    _subs[:] = [s for s in _subs if s[0] != fn]


def subscribers():
    return len(_subs)


def deliver(frame):
    """The core: hand one frame to every subscriber whose rate allows it (one that raises is reported)."""
    now = time.monotonic()
    for s in list(_subs):
        fn, gap, last = s
        if now - last < gap * 0.9:
            continue
        s[2] = now
        try:
            fn(frame)
        except Exception:
            print(f"[sdrexplorer] spectrum subscriber {getattr(fn, '__qualname__', fn)} failed:", file=sys.stderr)
            traceback.print_exc()


def reset():
    """Tests."""
    _subs.clear()
