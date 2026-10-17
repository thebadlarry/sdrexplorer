#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Tests for the example module, against the sdrexplorer checkout this example lives in.

    QT_QPA_PLATFORM=offscreen python3 tests/test_activity.py

The first test needs only numpy. The second builds the real main window offscreen (no SDR: the radio's
start() is stubbed), loads this module through the normal discovery path, and drives it with a synthetic
spectrum frame. Every user-data path goes to a temp folder.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                                   # examples/sdrexplorer-activity
CORE = os.path.dirname(os.path.dirname(REPO))                  # the sdrexplorer checkout
sys.path.insert(0, os.path.join(REPO, "src"))
sys.path.insert(0, CORE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                             # noqa: E402

from sdrexplorer_activity.detect import find_peaks             # noqa: E402

CENTER, RATE, NFFT = 462_000_000, 2_400_000, 2048


def frame_db(signals):
    """A synthetic log-power frame: a -90 dB floor with ripple, plus (offset_hz, width_hz, level_db) signals."""
    rng = np.random.default_rng(1)
    db = -90.0 + rng.normal(0, 1.0, NFFT)
    f = (np.arange(NFFT) / NFFT - 0.5) * RATE
    for off, width, level in signals:
        db[np.abs(f - off) <= width / 2] = level
    return db.astype(np.float32)


def test_find_peaks():
    db = frame_db([(100_000, 12_500, -60.0), (-400_000, 25_000, -70.0)])
    peaks = find_peaks(db, CENTER, RATE, threshold_db=10.0)
    assert len(peaks) == 2, peaks
    assert abs(peaks[0].freq_hz - (CENTER + 100_000)) < 2 * RATE / NFFT        # strongest first
    assert abs(peaks[1].freq_hz - (CENTER - 400_000)) < 2 * RATE / NFFT
    assert 10_000 < peaks[0].bw_hz < 16_000 and 28 < peaks[0].snr_db < 32
    assert find_peaks(frame_db([]), CENTER, RATE) == []
    print("ok  test_find_peaks")


def test_in_the_real_window():
    from PyQt5 import QtWidgets
    from sdrexplorer import paths as P

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    P.override(tempfile.mkdtemp(prefix="sdrexplorer-activity-test-"), module_dirs=[REPO])

    from sdrexplorer import app as A
    from sdrexplorer.api import spectrum
    from sdrexplorer.api.spectrum import SpectrumFrame

    A.ScannerRadio.start = lambda self: setattr(self, "running", True)        # no SDR in a test
    w = A.ScannerApp()
    try:
        mod = w.modules.get("activity")
        assert mod is not None, "the module did not load"
        # what it registered
        assert w.mode_combo.findText("NFM-25") >= 0
        w.mode_combo.setCurrentText("NFM-25")                                  # the core builds its demod
        assert w.radio.mode.name == "NFM-25" and w.radio.current_bw() == 25_000
        w.mode_combo.setCurrentText("NFM")
        assert any(w.module_tabs.tabText(i) == "Activity" for i in range(w.module_tabs.count()))
        assert spectrum.subscribers() >= 1

        # a frame with one busy channel: an annotation, the panel row, the request answer
        spectrum.deliver(SpectrumFrame(t=0.0, center_hz=CENTER, samp_rate=RATE,
                                       db=frame_db([(100_000, 12_500, -60.0)])))
        ann = w.annotation_near(CENTER + 100_000)
        assert ann is not None and ann["source"] == "activity", ann
        assert mod._list.count() == 1
        peaks = w.bus.request("activity.peaks", default=[])
        assert len(peaks) == 1 and abs(peaks[0]["freq_hz"] - (CENTER + 100_000)) < 3_000

        # the right-click entry publishes a Decode into the core's decode log
        entries = mod.context_entries(ann["freq_hz"], ann)
        assert len(entries) == 1
        entries[0].triggered()
        assert "busy channel" in "\n".join(w._dig_lines)

        # bank channel fields
        from sdrexplorer.api import Channel
        fields = mod.channel_fields(Channel(freq=CENTER + 100_000, mode="NFM"))
        assert fields and fields["snr_db"] > 20, fields

        # the menu toggle switches it off and clears the tags
        mod._menu.action.setChecked(False)
        assert not mod.enabled and mod.peaks == []
        assert mod.save_state() == {"enabled": False, "threshold_db": 10.0}
    finally:
        w.close()
    print("ok  test_in_the_real_window")


if __name__ == "__main__":
    test_find_peaks()
    test_in_the_real_window()
