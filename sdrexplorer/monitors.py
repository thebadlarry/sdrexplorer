# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Queue drainers for the core's own decoders: each turns a queue filled in the GNU Radio scheduler thread
into a Qt signal, so the GUI is only touched on the main thread."""
from __future__ import annotations

import queue

from PyQt5 import QtCore


class RdsMonitor(QtCore.QThread):
    """Drain the RDS field-update queue (filled by radio.RdsMsgSink from
    gr-rds's rds.parser message output in the GR scheduler thread) and emit
    each merged station-field dict as a Qt signal — queued, so the GUI is
    touched only on the main thread. ``stop()`` ends it. Mirrors
    AdsbMonitor below."""

    page = QtCore.pyqtSignal(dict)

    def __init__(self, out_queue, parent=None):
        super().__init__(parent)
        self._q = out_queue
        self._keep = True

    def run(self):
        while self._keep:
            try:
                p = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if p is not None:
                self.page.emit(p)

    def stop(self):
        self._keep = False


class AdsbMonitor(QtCore.QThread):
    """Drain the ADS-B aircraft queue (filled by radio.AdsbMsgSink from gr-adsb's
    decoder) and emit each aircraft-state dict as a Qt signal — queued, so the GUI
    is touched only on the main thread. ``stop()`` ends it."""

    aircraft = QtCore.pyqtSignal(dict)

    def __init__(self, out_queue, parent=None):
        super().__init__(parent)
        self._q = out_queue
        self._keep = True

    def run(self):
        while self._keep:
            try:
                d = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if d is not None:
                self.aircraft.emit(d)

    def stop(self):
        self._keep = False
