# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Scan workers that run off the UI thread.

Two behaviors:

* ``ChannelScanWorker`` — the classic scanner: step through a bank's channels,
  dwell, read channel power off the probe, and STOP on an active channel until
  the user resumes. This is what the Scan button drives.

* ``DiscoverWorker`` — a band sweep that finds *new* signals across a frequency
  range by peak prominence (generalized from the WFM reference detector), so
  discoveries can be saved into a bank.
"""
from __future__ import annotations

import threading
import time

import numpy as np
from PyQt5 import QtCore

from .radio import OFFSET, NFFT, DC_GUARD


class ChannelScanWorker(QtCore.QThread):
    """Sweep a list of channels, stop-on-active, then AUTO-resume once the
    signal has been gone for ``hang_ms`` (like a real scanner). A manual Resume
    skips immediately.

    Fast path: the probe FFT covers 2.4 MHz, so channels that fall within one
    window are all measured from a SINGLE hardware tune + FFT read, instead of
    retuning (and waiting for PLL settle) per channel. For clustered banks this
    is several times faster.
    """

    active = QtCore.pyqtSignal(int, float)   # (channel index, snr_dB)
    # (channel index just measured, signal_dB, noise_floor_dB) — the levels let
    # the UI show a live per-channel readout so a false stop is diagnosable
    checked = QtCore.pyqtSignal(int, float, float)
    resumed = QtCore.pyqtSignal()            # left an active channel, scanning on
    finished_scan = QtCore.pyqtSignal()
    # a priority channel (from the priority bank) is active: (freq, mode, label, snr).
    # freq is "qlonglong": pyqtSignal(int) is 32-bit and wraps above 2147.48 MHz
    priority_hit = QtCore.pyqtSignal("qlonglong", str, str, float)

    # Each channel is offset-tuned to +OFFSET and measured there — the same
    # position the radio uses when parked, so scan detection matches what you'd
    # hear. (Earlier a wideband window measured many channels per tune at varying
    # offsets, which under-read some and read the DC hump as a false signal on
    # others; per-channel tuning trades speed for that reliability.)
    # After a retune the HackRF USB pipeline + flowgraph still hold old-band
    # samples for a while; the instantaneous measurement FFT only reflects the
    # NEW band once they drain. Wait at least this long before trusting a read,
    # so the scanner never measures the previous (adjacent) channel's energy.
    MIN_SETTLE_MS = 120
    PRIO_SETTLE_MS = 120        # PLL/FFT/pipeline settle for a priority re-tune
    RECHECK_MS = 40             # gap for the de-bounce read (a fresh FFT frame)

    def __init__(self, radio, channels, dwell_ms=120,
                 hang_ms=2000, monitor_ms=150, priority=None, priority_ms=2000,
                 parent=None):
        super().__init__(parent)
        self.radio = radio
        # sort by frequency so nearby channels share a window
        self.channels = sorted(channels, key=lambda ic: ic[1].freq)
        self.dwell_ms = dwell_ms
        self.hang_ms = hang_ms               # resume after signal gone this long
        self.monitor_ms = monitor_ms         # how often to re-check while holding
        self.priority = list(priority or [])  # list[Channel] checked periodically
        self.priority_ms = priority_ms       # how often to poll the priority bank
        self._last_prio = 0.0
        self._abort = False
        self._resume = threading.Event()

    def _meas_bw(self, ch):
        return ch.bw or 12_500               # detection window if unset

    def _open(self, freq, bw):
        """True if the channel breaks the CURRENT squelch (read live, so turning
        the squelch knob or toggling auto-squelch during a scan takes effect
        immediately). Auto-squelch adapts to each band's noise floor."""
        sig, floor = self.radio.channel_power(freq, bw)
        return self.radio.squelch_open(sig, floor)

    # -- priority ------------------------------------------------------------
    def _due_priority(self):
        return bool(self.priority) and \
            (time.monotonic() - self._last_prio) >= self.priority_ms / 1000.0

    def _scan_priority(self):
        """Tune to each priority channel in turn and measure it; return the first
        active one as (Channel, snr_dB), else None. Restores the previous tune so
        the interrupted sweep/hold resumes where it was."""
        saved = self.radio.demod_hz
        hit = None
        for ch in self.priority:
            if self._abort:
                break
            self.radio.set_frequency(ch.freq)              # full park at +OFFSET
            self.msleep(self.PRIO_SETTLE_MS)
            sig, floor = self.radio.channel_power(ch.freq, self._meas_bw(ch))
            if self.radio.squelch_open(sig, floor):
                hit = (ch, sig - floor)
                break
        self._last_prio = time.monotonic()
        if hit is None:
            self.radio.set_frequency(saved)                # nothing -> undo tune
        return hit

    def _service_priority(self, hit):
        """Hold on an active priority channel (the caller emits `resumed` after)."""
        ch, snr = hit
        self._resume.clear()
        self.radio.set_frequency(ch.freq)                  # park for monitoring
        self.priority_hit.emit(int(ch.freq), str(ch.mode),
                               str(ch.label or ""), float(snr))
        self._hold(ch, is_priority=True)

    # -- main loop -----------------------------------------------------------
    def run(self):
        n = len(self.channels)
        if n == 0 and not self.priority:
            self.finished_scan.emit()
            return
        self._last_prio = time.monotonic()
        i = 0
        while not self._abort:
            # periodic priority poll (also drives a priority-only scan when the
            # normal channel list is empty)
            if self._due_priority():
                hit = self._scan_priority()
                if hit is not None:
                    self._service_priority(hit)
                    if self._abort:
                        break
                    self.resumed.emit()
                    continue
            if n == 0:
                self.msleep(120)
                continue
            idx, ch = self.channels[i]
            bw = self._meas_bw(ch)
            # Fully park on THIS channel (set_frequency: band center -> freq-OFFSET,
            # channelizer -> +OFFSET, demod_hz -> freq). Same
            # hardware tune as before, but now the demod/audio/waterfall marker and
            # modules' decoders all track the channel being measured — so "show during scan"
            # marks each channel, and a stop lands exactly where it was measured.
            self.radio.set_frequency(ch.freq)
            # wait long enough for the pipeline to flush the previous band before
            # trusting the instantaneous measurement FFT (never below MIN_SETTLE)
            self.msleep(max(self.dwell_ms, self.MIN_SETTLE_MS))
            sig, floor = self.radio.channel_power(ch.freq, bw)
            self.checked.emit(idx, sig, floor)
            if self.radio.squelch_open(sig, floor):
                self.msleep(self.RECHECK_MS)     # de-bounce (same tune, no settle)
                s2, f2 = self.radio.channel_power(ch.freq, bw)
                if self.radio.squelch_open(s2, f2):
                    self._resume.clear()
                    self.active.emit(idx, s2 - f2)
                    prio = self._hold(self.channels[i][1], is_priority=False)
                    if self._abort:
                        break
                    if prio is not None:         # a priority interrupted the hold
                        self._service_priority(prio)
                        if self._abort:
                            break
                    self.resumed.emit()          # tell the UI to re-mute + continue
            i += 1
            if i >= n:
                i = 0                            # wrap around
        self.finished_scan.emit()

    def _hold(self, ch, is_priority=False):
        """Stay on an active channel until it goes quiet for ``hang_ms`` or the
        user skips. While holding on a NON-priority channel, poll the priority
        bank periodically; if a priority channel is active, return it so the
        caller can switch to it. Returns the priority hit tuple or None."""
        gone_ms = 0
        while not self._abort:
            if self._resume.is_set():        # manual Resume / skip
                self._resume.clear()
                return None
            time.sleep(self.monitor_ms / 1000.0)
            if not is_priority and self._due_priority():
                phit = self._scan_priority()
                if phit is not None:
                    return phit              # -> caller services the priority
            if self._open(ch.freq, self._meas_bw(ch)):
                gone_ms = 0                  # still breaking squelch -> hold
            else:
                gone_ms += self.monitor_ms
                if gone_ms >= self.hang_ms:
                    return None              # quiet long enough -> auto-resume
        return None

    def resume(self):
        self._resume.set()

    def stop(self):
        self._abort = True
        self._resume.set()


class DiscoverWorker(QtCore.QThread):
    """Sweep [lo, hi] and return strong peaks as [(hz, prominence_dB), …]."""

    done = QtCore.pyqtSignal(list)
    progress = QtCore.pyqtSignal(int)

    # sweep geometry (mirrors the proven WFM scan constants)
    STEP = 800_000            # hardware retune step
    TRUST = 1_000_000         # only trust probe bins within ±this of step center
    SETTLE_MS = 180

    def __init__(self, radio, lo, hi, prom_thresh=6.0, min_sep=12_500,
                 parent=None):
        super().__init__(parent)
        self.radio = radio
        self.lo, self.hi = int(lo), int(hi)
        self.prom_thresh = prom_thresh
        self.min_sep = min_sep
        self._abort = False

    def run(self):
        centers = np.arange(self.lo + 400_000, self.hi - 300_000 + 1, self.STEP)
        if centers.size == 0:
            centers = np.array([(self.lo + self.hi) / 2.0])
        freqs_all, pow_all = [], []
        for k, fc in enumerate(centers):
            if self._abort:
                break
            self.radio.retune_only(fc + OFFSET)   # keep fc off the DC spike
            self.msleep(self.SETTLE_MS)
            level = np.asarray(self.radio.probe.level(), dtype=np.float64)
            if level.size != NFFT:
                self.progress.emit(int(100 * (k + 1) / len(centers)))
                continue
            center = fc                            # source is at fc (=fc+OFFSET-OFFSET)
            bins = center + (np.arange(NFFT) / NFFT - 0.5) * self.radio.samp_rate
            dist = np.abs(bins - center)
            keep = (dist < self.TRUST) & (dist > DC_GUARD)
            freqs_all.append(bins[keep])
            pow_all.append(level[keep])
            self.progress.emit(int(100 * (k + 1) / len(centers)))

        if not freqs_all:
            self.done.emit([])
            return
        peaks = detect_peaks(np.concatenate(freqs_all),
                             np.concatenate(pow_all),
                             self.lo, self.hi,
                             prom_thresh=self.prom_thresh,
                             min_sep=self.min_sep)
        self.done.emit(peaks)

    def stop(self):
        self._abort = True


def detect_peaks(freqs, powers, lo, hi, grid_step=5_000, prom_thresh=6.0,
                 min_sep=12_500, top_n=60):
    """Rasterless peak finder: max-hold the (freq, power) cloud onto a fine
    grid, then keep local maxima whose prominence over the surrounding noise
    exceeds ``prom_thresh``, enforcing ``min_sep`` between kept peaks.

    Returns [(hz, prominence_dB), …] strongest first. Pure numpy.
    """
    grid = np.arange(lo, hi + grid_step, grid_step)
    gpow = np.full(grid.size, -200.0)
    idx = np.round((freqs - lo) / grid_step).astype(int)
    ok = (idx >= 0) & (idx < grid.size)
    for i, p in zip(idx[ok], powers[ok]):
        if p > gpow[i]:
            gpow[i] = p

    seen = gpow > -150.0
    if seen.sum() < 8:
        return []

    floor = float(np.median(gpow[seen]))
    half = max(1, int(round((min_sep / 2) / grid_step)))

    # candidate = local maximum over a ±half window, above floor+thresh
    cands = []
    for j in range(grid.size):
        if not seen[j] or gpow[j] < floor + prom_thresh:
            continue
        a, b = max(0, j - half), min(grid.size, j + half + 1)
        if gpow[j] >= gpow[a:b].max():
            cands.append((gpow[j] - floor, int(grid[j])))

    # non-max suppression by separation, strongest first
    cands.sort(reverse=True)
    kept = []
    for prom, hz in cands:
        if all(abs(hz - k_hz) >= min_sep for _, k_hz in kept):
            kept.append((prom, hz))
        if len(kept) >= top_n:
            break

    return [(hz, round(prom, 1)) for prom, hz in kept]
