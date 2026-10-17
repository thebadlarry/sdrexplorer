# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Finding busy channels in one spectrum frame. numpy only: no Qt, no GNU Radio, so it tests on its own."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class Peak:
    freq_hz: float          # center of the run of busy bins
    bw_hz: float            # width of the run
    snr_db: float           # its strongest bin over the noise floor


def find_peaks(db, center_hz, samp_rate, threshold_db=10.0, max_peaks=12, min_bins=2):
    """Runs of at least ``min_bins`` adjacent bins more than ``threshold_db`` over the frame's median (the
    noise floor), strongest first. ``db``: log power, lowest frequency first, as api.spectrum delivers it."""
    db = np.asarray(db, np.float64)
    n = db.size
    if n == 0:
        return []
    floor = float(np.median(db))
    busy = db > floor + threshold_db
    # edges of each run of True: +1 where a run starts, -1 one past where it ends
    d = np.diff(np.concatenate([[0], busy.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    bin_hz = samp_rate / n
    out = []
    for s, e in zip(starts, ends):
        if e - s < min_bins:
            continue
        mid = (s + e - 1) / 2.0
        out.append(Peak(freq_hz=center_hz + (mid / n - 0.5) * samp_rate,
                        bw_hz=(e - s) * bin_hz,
                        snr_db=float(db[s:e].max() - floor)))
    out.sort(key=lambda p: p.snr_db, reverse=True)
    return out[:max_peaks]
