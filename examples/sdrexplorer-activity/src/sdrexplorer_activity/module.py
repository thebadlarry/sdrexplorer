# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The activity module: an example of the sdrexplorer module API (docs/module-tutorial.md walks through it).

It reads the core's spectrum frames, finds busy channels, and:
  - tags each one on the waterfall (annotations),
  - lists them in an "Activity" tab of the decode strip (a panel),
  - adds a View menu toggle, a right-click entry and a Mode-picker entry,
  - answers the request "activity.peaks" for other modules,
  - stores the last measured level on bank channels saved while tuned to one,
  - keeps its settings in the session state.

Everything goes through sdrexplorer.api; nothing else of the core is imported.
"""
from __future__ import annotations

from PyQt5 import QtWidgets

from sdrexplorer.api import (Annotation, Decode, DemodSpec, MenuEntry, ModeSpec, Module, Panel, spectrum)

from . import __version__
from .detect import find_peaks

FRAME_RATE_HZ = 2.0            # spectrum frames per second: plenty for a display, cheap on the Qt thread


class ActivityModule(Module):
    name = "activity"
    title = "Band activity (example)"
    version = __version__
    api = ">=1.0,<2.0"

    def __init__(self):
        super().__init__()
        self.ui = None
        self.enabled = True
        self.threshold_db = 10.0
        self.peaks = []                  # [detect.Peak] of the last frame
        self._list = None                # the panel's list widget, once the panel exists
        self._menu = None

    # -- lifecycle ------------------------------------------------------------------------------------
    def probe(self):
        try:
            import numpy  # noqa: F401
        except ImportError as e:
            return False, f"numpy missing: {e}"
        return True, ""

    def attach_ui(self, app, ui):
        super().attach_ui(app, ui)
        self.ui = ui
        ui.bus.subscribe("radio_stopped", self._on_radio_stopped)
        ui.bus.subscribe("band_changed", self._on_band_changed)
        ui.bus.provide_request("activity.peaks", self._answer_peaks, owner=self.name)

    def modes(self):
        # A wide-NFM entry in the Mode picker: the core builds the audio path from the DemodSpec.
        return [ModeSpec("NFM-25", demod=DemodSpec("fm", bw=25_000, max_dev=5_000, deemph=75e-6),
                         tooltip="NFM on a 25 kHz channel (example module)")]

    def menu_entries(self):
        self._menu = MenuEntry("View", "Band activity markers", checkable=True, checked=self.enabled,
                               toggled=self._set_enabled,
                               tooltip="Tag busy channels on the waterfall (example module)")
        return [self._menu]

    def panels(self):
        return [Panel("Activity", self._make_panel)]

    def context_entries(self, freq_hz, annotation):
        if not annotation or annotation.get("source") != self.name:
            return []
        f, snr = annotation["freq_hz"], annotation.get("snr_db", 0.0)
        return [MenuEntry("", f"Log this signal ({snr:.0f} dB)", triggered=lambda: self._log(f, snr))]

    def load_state(self, d):
        self.threshold_db = float(d.get("threshold_db", self.threshold_db))
        self._set_enabled(bool(d.get("enabled", self.enabled)))
        if self._menu is not None and self._menu.action is not None:
            self._menu.action.setChecked(self.enabled)

    def save_state(self):
        return {"enabled": self.enabled, "threshold_db": self.threshold_db}

    def shutdown(self):
        spectrum.unsubscribe(self._on_frame)

    # -- bank channels --------------------------------------------------------------------------------
    def channel_fields(self, channel):
        p = self._peak_at(channel.freq)
        return {"snr_db": round(p.snr_db, 1)} if p else None

    def channel_summary(self, fields):
        return f"{fields.get('snr_db', '?')} dB over the floor when saved"

    # -- work -----------------------------------------------------------------------------------------
    def _set_enabled(self, on):
        self.enabled = bool(on)
        if self.enabled:
            spectrum.subscribe(self._on_frame, rate_hz=FRAME_RATE_HZ)
        else:
            spectrum.unsubscribe(self._on_frame)
            self._clear()

    def _on_frame(self, frame):
        """An api.spectrum.SpectrumFrame, on the Qt thread: cheap work only."""
        self.peaks = find_peaks(frame.db, frame.center_hz, frame.samp_rate, self.threshold_db)
        self.ui.annotate([Annotation(freq_hz=p.freq_hz, bw_hz=p.bw_hz, label=f"{p.snr_db:.0f} dB",
                                     snr_db=p.snr_db, raw="busy") for p in self.peaks])
        self._show()

    def _on_radio_stopped(self):
        self._clear()

    def _on_band_changed(self, center_hz, samp_rate):
        self._clear()                        # the old tags point at frequencies no longer shown

    def _clear(self):
        self.peaks = []
        if self.ui is not None:
            self.ui.annotate([], trail=False)
        self._show()

    def _peak_at(self, freq_hz):
        return next((p for p in self.peaks if abs(p.freq_hz - freq_hz) <= max(p.bw_hz / 2, 2_500)), None)

    def _answer_peaks(self):
        return [{"freq_hz": p.freq_hz, "bw_hz": p.bw_hz, "snr_db": p.snr_db} for p in self.peaks]

    def _log(self, freq_hz, snr_db):
        self.ui.publish(Decode(source=self.name, protocol="activity",
                               text=f"busy channel {freq_hz / 1e6:.4f} MHz, {snr_db:.0f} dB over the floor",
                               fields={"snr_db": snr_db}, freq_hz=int(freq_hz), counts=False))

    # -- panel ----------------------------------------------------------------------------------------
    def _make_panel(self, parent):
        w = QtWidgets.QWidget(parent)
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        self._list = QtWidgets.QListWidget()
        self._list.itemDoubleClicked.connect(self._tune_item)
        lay.addWidget(self._list, 1)
        thr = QtWidgets.QDoubleSpinBox()
        thr.setRange(3, 40)
        thr.setSuffix(" dB")
        thr.setValue(self.threshold_db)
        thr.setToolTip("How far over the noise floor a channel must be to count as busy")
        thr.valueChanged.connect(lambda v: setattr(self, "threshold_db", float(v)))
        lay.addWidget(thr)
        self._show()
        return w

    def _show(self):
        if self._list is None:
            return
        self._list.clear()
        for p in self.peaks:
            item = QtWidgets.QListWidgetItem(f"{p.freq_hz / 1e6:10.4f} MHz  {p.bw_hz / 1e3:6.1f} kHz  "
                                             f"{p.snr_db:5.1f} dB")
            item.setData(0x0100, p.freq_hz)            # Qt.UserRole
            self._list.addItem(item)

    def _tune_item(self, item):
        # user=True: the user asked for this in our panel, so it tunes as a click would, never refused
        self.ui.tune(int(item.data(0x0100)), recenter=False, user=True)
