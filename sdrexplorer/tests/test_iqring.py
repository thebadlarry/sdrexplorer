#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Tests for the retroactive IQ snapshot ring (iqring.py). No hardware:

    python3 -m sdrexplorer.tests.test_iqring
"""
import os
import tempfile
import threading
import time

import numpy as np
from gnuradio import blocks, gr

from sdrexplorer import iqring as R


def _feed(ring, x, chunk=8192):
    """Push int16 values through ring.work() the way the scheduler would."""
    for o in range(0, len(x), chunk):
        ring.work([x[o:o + chunk]], [])


def test_wraps_and_keeps_latest():
    ring = R.IQRing()
    ring.configure(1000, 2.0, 100)            # 2 s + 1 s margin of 1 kS/s
    x = np.arange(2 * 5000, dtype=np.int64).astype(np.int16)   # 5 s, wraps the ring
    _feed(ring, x, 777)
    iq, m = ring.snapshot(0)
    assert m["center_hz"] == 100 and m["samp_rate"] == 1000
    assert len(iq) == 2 * 2000                # exactly the configured 2 s
    assert np.array_equal(iq, x[-4000:])
    assert abs(m["trigger_offset_s"] - 2.0) < 1e-9


def test_retune_cuts_old_band():
    ring = R.IQRing()
    ring.configure(1000, 5.0, 100)
    _feed(ring, np.ones(2 * 3000, np.int16))
    ring.mark(200)
    guard = 2 * int(R.RETUNE_GUARD_S * 1000)
    new = np.full(2 * 1000, 7, np.int16)
    _feed(ring, new)
    iq, m = ring.snapshot(0)
    assert m["center_hz"] == 200
    assert len(iq) == len(new) - guard and (iq == 7).all()


def test_pre_s_keeps_only_the_tail():
    ring = R.IQRing()
    ring.configure(1000, 5.0, 0)
    x = np.arange(2 * 4000, dtype=np.int64).astype(np.int16)        # 4 s buffered
    _feed(ring, x)
    iq, m = ring.snapshot(0, pre_s=1.5)
    assert len(iq) == 2 * 1500 and np.array_equal(iq, x[-3000:]), len(iq)
    assert abs(m["duration_s"] - 1.5) < 1e-9 and abs(m["trigger_offset_s"] - 1.5) < 1e-9
    iq2, _ = ring.snapshot(0, pre_s=60)                              # more than buffered: all of it
    assert len(iq2) == 2 * 4000


def test_post_trigger_wait():
    ring = R.IQRing()
    ring.configure(1000, 2.0, 0)
    _feed(ring, np.zeros(2 * 3000, np.int16))
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            ring.work([np.full(200, 5, np.int16)], [])   # 100 samples
            time.sleep(0.01)                             # ~10 kS/s, faster than "real time"

    t = threading.Thread(target=writer)
    t.start()
    try:
        iq, m = ring.snapshot(0.5)
    finally:
        stop.set()
        t.join()
    # the last 0.5 s came after the click
    assert len(iq) == 2 * 2000
    assert (iq[-1000:] == 5).all()
    assert abs(m["trigger_offset_s"] - 1.5) < 0.01


def test_disabled_and_empty():
    ring = R.IQRing()
    ring.configure(1000, 0, 0)
    _feed(ring, np.ones(100, np.int16))
    assert ring.snapshot(0) == (None, None)
    ring.configure(1000, 1, 0)
    assert ring.snapshot(0) == (None, None)


def test_subband_matches_tone():
    fs, f0, n = 2_400_000, 300_000, 2_400_000 // 4
    t = np.arange(n)
    z = 0.5 * np.exp(2j * np.pi * (f0 + 1_000) * t / fs) \
        + 0.5 * np.exp(2j * np.pi * -500_000 * t / fs)   # the other tone is filtered out
    iq = np.empty(2 * n, np.int16)
    iq[0::2] = np.round(z.real * 32767)
    iq[1::2] = np.round(z.imag * 32767)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "s.cs16")
        rate = R.write_subband(p, iq, fs, f0, 25_000, chunk=50_000)
        y = np.fromfile(p, np.int16).astype(np.float32).view(np.complex64) / 32767
    decim, _ = R.subband_plan(fs, 25_000)
    assert rate == fs / decim and abs(len(y) - n / decim) <= 1
    y = y[200:]                                          # past the filter start-up
    F = np.fft.fftshift(np.abs(np.fft.fft(y)))
    f = np.fft.fftshift(np.fft.fftfreq(len(y), 1 / rate))
    assert abs(f[np.argmax(F)] - 1_000) < 2 * rate / len(y)
    assert abs(np.sqrt(np.mean(np.abs(y) ** 2)) - 0.5) < 0.03   # tone kept, other one gone
    ph = np.unwrap(np.angle(y))                         # no phase jumps at chunk joins
    assert np.max(np.abs(np.diff(ph) - 2 * np.pi * 1_000 / rate)) < 0.05


def test_in_flowgraph():
    """The block runs under the real scheduler, fed from the recorder's converter."""
    tb = gr.top_block()
    n = 200_000
    src = blocks.vector_source_c((np.exp(1j * np.arange(n) * 0.01) * 0.5).astype(np.complex64), False)
    conv = blocks.complex_to_interleaved_short(False, 32767.0)
    ring = R.IQRing()
    ring.configure(100_000, 1.0, 42)
    tb.connect(src, conv, ring)
    tb.run()
    iq, m = ring.snapshot(0)
    assert len(iq) == 2 * 100_000 and m["center_hz"] == 42
    z = iq.astype(np.float32).view(np.complex64) / 32767
    assert np.allclose(z, 0.5 * np.exp(1j * np.arange(n - 100_000, n) * 0.01), atol=1e-3)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
