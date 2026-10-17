# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Retroactive IQ snapshots: a rolling buffer of the last few seconds of the
raw band, so a transmission that has already happened can still be saved.

IQRing is a sink fed from the radio's existing int16 converter (the same
interleaved cs16 the recorder writes), so the buffer costs one memcpy per
work() call and no extra conversion. Retunes mark() the ring: only samples
captured since the last retune share one band center, so a snapshot never
mixes two tunings. snapshot() copies the chosen span out in chunks (the
flowgraph keeps running meanwhile) and write_band() / write_subband() turn
it into a .cs16 file that the existing replay, module and training tools read
like any other capture.
"""
import threading
import time

import numpy as np
from gnuradio import gr

COPY_CHUNK = 1 << 21          # int16 values per chunk copied out of the ring (4 MB)
RETUNE_GUARD_S = 0.02         # samples still in flight from the old tuning after a retune
MARGIN_S = 1.0                # extra ring the writer may fill while a snapshot copies out


class IQRing(gr.sync_block):
    """Ring of the last N seconds of interleaved int16 IQ (I, Q, I, Q, ...)."""

    def __init__(self):
        gr.sync_block.__init__(self, name="iq_ring", in_sig=[np.int16], out_sig=None)
        self._lock = threading.Lock()
        self._buf = np.zeros(0, np.int16)
        self.samp_rate = 0.0
        self.seconds = 0.0
        self._total = 0            # int16 values ever written (2 per complex sample)
        self._valid_from = 0       # first value of the current tuning
        self.center_hz = 0

    # -- configuration (any thread) -----------------------------------------
    def configure(self, samp_rate, seconds, center_hz):
        """(Re)allocate for ``seconds`` of ``samp_rate``; drops what was buffered.
        seconds <= 0 frees the buffer."""
        n = int(max(0.0, seconds) * samp_rate) * 2
        if n:
            n += int(MARGIN_S * samp_rate) * 2
        with self._lock:
            # np.zeros is lazily backed: pages are only committed as the ring fills
            self._buf = np.zeros(n, np.int16)
            self.samp_rate = float(samp_rate)
            self.seconds = float(seconds) if n else 0.0
            self._total = 0
            self._valid_from = 0
            self.center_hz = int(center_hz)

    def mark(self, center_hz):
        """The band center just changed: what came before is a different band."""
        with self._lock:
            self._valid_from = self._total + 2 * int(RETUNE_GUARD_S * self.samp_rate)
            self.center_hz = int(center_hz)

    def buffered_seconds(self):
        with self._lock:
            return self._span(self._total, self._valid_from) / 2 / self.samp_rate \
                if self.samp_rate else 0.0

    def _span(self, end, valid_from):
        keep = min(int(self.seconds * self.samp_rate) * 2, len(self._buf))
        return max(0, end - max(valid_from, end - keep))

    # -- flowgraph thread -----------------------------------------------------
    def work(self, input_items, output_items):
        x = input_items[0]
        n_in = len(x)
        with self._lock:
            b = self._buf
            cap = len(b)
            if cap:
                if len(x) >= cap:
                    x = x[-cap:]
                n = len(x)
                p = (self._total + n_in - n) % cap
                k = min(n, cap - p)
                b[p:p + k] = x[:k]
                if k < n:
                    b[:n - k] = x[k:]
            self._total += n_in
        return n_in

    # -- snapshot (worker thread) ---------------------------------------------
    def snapshot(self, post_s=0.0, pre_s=None):
        """Wait up to ``post_s`` seconds past now (cut short by a retune), then
        copy out everything buffered for the tuning that was live at the call --
        or, with ``pre_s``, only the last ``pre_s`` seconds before the call.
        Returns (int16 array, meta) or (None, None) if nothing is buffered.
        meta: samp_rate, center_hz, trigger_offset_s, duration_s."""
        with self._lock:
            trigger = self._total
            buf = self._buf                    # configure() may swap in a new one meanwhile
            rate, cap = self.samp_rate, len(buf)
            mark0 = self._valid_from
            center = self.center_hz
        if not cap or not rate:
            return None, None
        want = trigger + 2 * int(post_s * rate)
        deadline = time.monotonic() + post_s + 2.0
        while post_s > 0:
            with self._lock:
                done = self._total >= want or self._valid_from != mark0
            if done or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        with self._lock:
            if self._buf is not buf:
                return None, None
            end = min(self._total, want)
            if self._valid_from != mark0:      # retuned during the wait: stop at the retune
                end = min(end, self._valid_from - 2 * int(RETUNE_GUARD_S * rate))
            span = self._span(end, mark0)
            if pre_s is not None:              # just the last pre_s before the call (+ post_s)
                span = min(span, (end - trigger) + 2 * int(pre_s * rate))
            span -= span % 2
        if span <= 0:
            return None, None
        start = end - span
        out = np.empty(span, np.int16)
        for o in range(0, span, COPY_CHUNK):   # chunked so work() keeps getting the GIL
            m = min(COPY_CHUNK, span - o)
            p = (start + o) % cap
            k = min(m, cap - p)
            out[o:o + k] = buf[p:p + k]
            if k < m:
                out[o + k:o + m] = buf[:m - k]
        with self._lock:
            if self._buf is not buf:           # reconfigured mid-copy: contents are gone
                return None, None
            lapped = self._total - cap - start  # the writer overwrote the head of the copy
        if lapped > 0:
            lapped += lapped % 2
            out = out[lapped:]
            start += lapped
        return out, {
            "samp_rate": rate,
            "center_hz": center,
            "trigger_offset_s": max(0.0, (trigger - start) / 2 / rate),
            "duration_s": len(out) / 2 / rate,
        }


def write_band(path, iq16):
    """The snapshot as-is: the ring already holds the recorder's cs16 format."""
    with open(path, "wb") as fh:
        for o in range(0, len(iq16), COPY_CHUNK * 4):
            fh.write(memoryview(iq16[o:o + COPY_CHUNK * 4]))


def save_rows(path, rows):
    """The waterfall rows on screen at the click ([rows, NFFT] dB, oldest first)."""
    np.save(path, np.asarray(rows, np.float32))


def subband_plan(samp_rate, bw_hz):
    """(decimation, taps) for a sub-band of ``bw_hz``: the same design as the
    live sub-band recorder (ScannerRadio.start_record), so both files match."""
    from gnuradio.filter import firdes
    decim = max(1, int(samp_rate / (bw_hz * 1.4)))
    taps = np.asarray(firdes.low_pass(1.0, samp_rate, bw_hz / 2,
                                      max(5_000, bw_hz * 0.15)), np.float32)
    return decim, taps


def write_subband(path, iq16, samp_rate, offset_hz, bw_hz, chunk=1 << 22):
    """Shift ``offset_hz`` to baseband, low-pass to ``bw_hz`` and decimate, in
    chunks (overlap-add FFT filtering), writing cs16. ~6 s for a 12 s,
    19.2 Msps snapshot. Returns the output sample rate."""
    from scipy.signal import oaconvolve
    decim, h = subband_plan(samp_rate, bw_hz)
    h = h.astype(np.complex64)
    n = len(iq16) // 2
    pad = -(-(len(h) - 1) // decim) * decim          # filter history, a multiple of decim
    chunk -= chunk % decim
    w = -2 * np.pi * offset_hz / samp_rate
    # mixer phasor for one chunk (+ history), rotated per chunk by its start phase
    base = np.exp(1j * w * np.arange(chunk + pad)).astype(np.complex64)
    with open(path, "wb") as fh:
        for s in range(0, n, chunk):
            e = min(n, s + chunk)
            a = s - pad
            v = np.zeros(e - a, np.complex64)
            lo = max(0, a)                            # start of the file: zero history
            v[lo - a:] = iq16[2 * lo:2 * e].astype(np.float32).view(np.complex64)
            v *= base[:e - a] * np.complex64(np.exp(1j * ((w * a) % (2 * np.pi))) / 32767.0)
            # full convolution sample k*decim == upfirdn(h, v, 1, decim)[k]
            y = oaconvolve(v, h)[pad:pad + (e - s):decim]
            out = np.empty(2 * len(y), np.int16)
            out[0::2] = np.clip(np.round(y.real * 32767), -32768, 32767)
            out[1::2] = np.clip(np.round(y.imag * 32767), -32768, 32767)
            fh.write(out.tobytes())
    return samp_rate / decim
