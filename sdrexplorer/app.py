#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""sdrexplorer — front panel for exploring and analyzing digital and analog
signals using software defined radio (HackRF / RTL-SDR).

Launch via run_sdrexplorer.sh (clean env so GR 3.10 isn't shadowed by gr4).

Panels:
  * LCD frequency readout + tuning, mode selector, gain knobs, volume, squelch
  * Bank selector + New/Delete bank, manual add, Save-to-bank
  * Loaded-frequencies table (the active bank) — double-click to tune
  * Scan (stop-on-active) with service-type filter checkboxes
  * Discover sweep over a range -> save peaks into a bank
  * Live spectrum plot (if qtgui available)
"""
import dataclasses
import gc
import json
import os
import re
import signal
import sys
import time
from datetime import datetime

from PyQt5 import QtCore, QtGui, QtWidgets

# QtWebEngine is an optional dependency (PyQtWebEngine) used only for the live
# map. It MUST be imported before the QApplication is constructed, so the guard
# lives at module import time. When absent, the Map menu item is disabled.
try:
    from PyQt5 import QtWebEngineWidgets
    WEBENGINE_AVAILABLE = True
except Exception:
    QtWebEngineWidgets = None
    WEBENGINE_AVAILABLE = False

from .banks import (BankStore, Bank, Channel, SERVICE_TAGS, ANALOG_MODES,
                    PRIORITY_BANK, parse_tone)
from .radio import (ScannerRadio, TxChain, MODES, TX_MODES, SDR_DEVICES, IQ_FILE_DEVICE,
                    ADSB_AVAILABLE, ADSB_FREQ, CUDA_AVAILABLE,
                    SAMP_RATE, SAMP_RATE_OPTIONS, CTCSS_TONES,
                    SNAPSHOT_SECONDS, in_us_ham_band, standard_repeater_offset)
from .scanner import ChannelScanWorker, DiscoverWorker
from .spectrum import SpectrumWidget, WATERFALL_ROW_S
from . import iqring as _iqring
from . import paths as _paths
from . import modules as _modules
from . import recindex
from .api import AudioArbiter, EventBus, importers, station
from .api import spectrum as _api_spectrum
from .api.tuner import Tuner, SCAN as TUNE_SCAN, PARK as TUNE_PARK
from .moduleui import ModuleUI, ModulesDialog
from .rectrigger import (TriggerMonitor, TRIGGER_THRESHOLD_DB, TRIGGER_HANG_S,
                         TRIGGER_MAX_S, TRIGGER_PRE_S)
from .targets import TargetStore
from .fetch import FetchWorker, DownloadWorker, GeocodeWorker, register_core_importers
from .monitors import AdsbMonitor, RdsMonitor
from . import pcap as _pcap
from .reclabel import RecordingLabels

from .providers import radioreference as rr
from .providers import fcc_uls, csv_import, geocode
from .providers.base import haversine_km

geocode.prefer_ipv4()      # dodge dead-IPv6 stalls for all network in this app

_EXIT_DEBUG = bool(os.environ.get("SDREXPLORER_EXIT_DEBUG"))


def _exit_trace(msg):
    """Print a shutdown-sequence marker when SDREXPLORER_EXIT_DEBUG is set, so a
    core dump on exit can be localized to a specific teardown step. No-op
    otherwise. Writes straight to fd 2 and flushes (the abort is a hard crash)."""
    if _EXIT_DEBUG:
        try:
            os.write(2, f"[sdrexplorer-exit] {msg}\n".encode())
        except OSError:
            pass


_HERE = os.path.dirname(os.path.abspath(__file__))


# Where the user's data lives (sdrexplorer.paths: ~/.local/share/sdrexplorer, or a portable
# <install>/data). Functions, not constants: the location can move at start-up (paths.migrate_legacy)
# and tests redirect it with paths.override().
def _memory_path():                       # banks
    return _paths.memory_file("banks.json")


OFFSET_MAX_MHZ = 50.0                      # repeater TX split box: 800 MHz repeaters use 45 MHz


def _offset_step(mhz):
    """The offset box's arrow step: 5 kHz for VHF/UHF splits (600 kHz, 5 MHz is typed), 100 kHz above."""
    return 0.005 if float(mhz) < 1.0 else 0.1


def _state_path():                        # last-session UI state
    return _paths.memory_file("state.json")


def _uls_dir():                           # auto-managed FCC ULS data directory
    return _paths.uls_dir()


def _recordings_dir():                    # saved IQ captures
    return _paths.recordings_dir()

# tuning step per mode (Hz) for the +/- buttons
STEP_HZ = {"WFM": 100_000, "NFM": 12_500, "AM": 10_000}


def _checkmark_icon():
    """Path of a small white check-mark PNG for the styled check-box indicator (a styled
    ::indicator no longer draws Qt's own tick). Drawn once with QPainter into the temp dir."""
    import tempfile
    path = os.path.join(tempfile.gettempdir(), "sdrexplorer_check.png")
    if not os.path.exists(path):
        img = QtGui.QImage(28, 28, QtGui.QImage.Format_ARGB32)
        img.fill(QtCore.Qt.transparent)
        p = QtGui.QPainter(img)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        pen = QtGui.QPen(QtGui.QColor("#ffffff"), 4.2, QtCore.Qt.SolidLine,
                         QtCore.Qt.RoundCap, QtCore.Qt.RoundJoin)
        p.setPen(pen)
        p.drawPolyline(QtGui.QPolygonF([QtCore.QPointF(6, 14.5), QtCore.QPointF(11.5, 20),
                                        QtCore.QPointF(22, 8)]))
        p.end()
        img.save(path)
    return path.replace("\\", "/")


def _fmt_mhz(hz):
    """A frequency in MHz with 4 decimals (100 Hz), or more when finer digits are set:
    154.0000, 7.030050, 7.0300123 -> "7.030012" (1 Hz)."""
    hz = int(round(hz))
    for d in (4, 5, 6):
        if hz % 10 ** (6 - d) == 0:
            return f"{hz / 1e6:.{d}f}"
    return f"{hz / 1e6:.6f}"


def _fmt_khz(khz):
    """12.5 -> '12.5', 5.0 -> '5' (matches the snap-spacing combo items)."""
    return "%g" % float(khz)


_APP_ICON = None


def make_scanner_icon():
    """A handheld-scanner glyph (body + antenna + signal waves + screen + speaker
    grille) drawn at runtime, used as the window / taskbar icon — no asset file
    to ship, and it matches the app's green-on-dark theme. Cached after first
    build. Must be called after a QApplication exists (QPixmap needs one)."""
    global _APP_ICON
    if _APP_ICON is not None:
        return _APP_ICON
    green = QtGui.QColor("#39ff88")
    dark = QtGui.QColor("#12161c")
    px = QtGui.QPixmap(256, 256)
    px.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(px)
    p.setRenderHint(QtGui.QPainter.Antialiasing, True)

    # signal waves radiating from the antenna tip (behind everything)
    tip = QtCore.QPointF(198, 28)
    p.setBrush(QtCore.Qt.NoBrush)
    p.setPen(QtGui.QPen(green, 7, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
    for r in (26, 44):
        p.drawArc(QtCore.QRectF(tip.x() - r, tip.y() - r, 2 * r, 2 * r),
                  8 * 16, 66 * 16)          # arc opening to the upper-right

    # antenna + tip
    p.setPen(QtGui.QPen(green, 11, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
    p.drawLine(QtCore.QPointF(150, 72), tip)
    p.setPen(QtCore.Qt.NoPen)
    p.setBrush(green)
    p.drawEllipse(tip, 11, 11)

    # radio body
    p.setBrush(dark)
    p.setPen(QtGui.QPen(green, 8))
    p.drawRoundedRect(QtCore.QRectF(66, 66, 104, 170), 20, 20)

    # display screen
    p.setBrush(QtGui.QColor(57, 255, 136, 55))
    p.setPen(QtGui.QPen(green, 3))
    p.drawRoundedRect(QtCore.QRectF(82, 86, 72, 42), 6, 6)

    # speaker grille
    p.setPen(QtGui.QPen(green, 9, QtCore.Qt.SolidLine, QtCore.Qt.RoundCap))
    for y in (152, 174, 196):
        p.drawLine(QtCore.QPointF(84, y), QtCore.QPointF(152, y))
    p.end()

    _APP_ICON = QtGui.QIcon(px)
    return _APP_ICON


class TuningDisplay(QtWidgets.QWidget):
    """LCD-style frequency readout you can tune directly, down to 1 Hz:

    * left-click a digit raises that place by 1, right-click lowers it
      (e.g. left-click the hundreds-MHz digit of 0400.000000 -> 0500.000000);
    * the mouse wheel over a digit steps that place, one step per notch;
    * ``start_entry()`` (the Tune button) blanks it to ---.---- and accepts
      number-pad / number-key entry of a new frequency in MHz (Enter tunes,
      Esc cancels); typing a digit while focused also starts entry.
    """

    # Hz. "qlonglong" (64-bit), not int: a pyqtSignal(int) is a C++ 32-bit int, so anything
    # above 2^31 Hz = 2147.48 MHz wrapped negative (HackRF tunes to 6 GHz)
    tuned = QtCore.pyqtSignal("qlonglong")  # new frequency, Hz
    # a step of a sub-kHz digit (100 / 10 / 1 Hz): a fine adjustment of the same channel, which the
    # app applies by moving only the demod within the band (no hardware retune)
    nudged = QtCore.pyqtSignal("qlonglong")
    FINE_HZ = 1000                          # digit places below this are "fine" (nudged)
    MIN_HZ = 1_000_000
    MAX_HZ = 6_000_000_000
    FRAC = 6                                # decimals shown = 1 Hz resolution
    KHZ_DIGITS = 3                          # decimals up to the kHz place (then a thin gap)

    def __init__(self, hz=154_000_000, parent=None):
        super().__init__(parent)
        self._hz = int(hz)
        self._entry = None                  # None = show mode; str = typing MHz
        self._tx_hz = None                  # None = normal (RX); int = cosmetic TX-frequency override
        self._subtitle = ""                 # channel info line for a known freq
        self._spans = []                    # [(x0, x1, place_hz)] per digit
        self._font = QtGui.QFont("DejaVu Sans Mono")
        self._font.setPixelSize(42)
        self._font.setBold(True)
        self.setMinimumHeight(80)
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setContextMenuPolicy(QtCore.Qt.PreventContextMenu)  # right-click = -1
        self.setToolTip("Left-click a digit to raise it, right-click to lower it, or turn the "
                        "mouse wheel over it. The last three digits are hundreds, tens and "
                        "units of Hz. Click Tune (or just type) to enter a frequency in MHz.")
        self._wheel_accum = 0               # high-resolution wheels send fractions of a notch

    def setFrequency(self, hz):
        self._hz = max(self.MIN_HZ, min(self.MAX_HZ, int(hz)))
        self.update()

    def frequency(self):
        return self._hz

    def set_tx_frequency(self, hz):
        """Cosmetically show ``hz`` in red with a TX marker while transmitting. Purely a paint
        override -- the underlying tuned frequency (self._hz) and the radio are untouched, so
        clear_tx_frequency() returns the display to exactly what it showed before."""
        self._tx_hz = int(hz)
        self.update()

    def clear_tx_frequency(self):
        if self._tx_hz is not None:
            self._tx_hz = None
            self.update()

    def setSubtitle(self, text):
        """Second line shown under the frequency when it's a known channel
        (empty string = unknown -> frequency only)."""
        text = text or ""
        if text != self._subtitle:
            self._subtitle = text
            self.update()

    def start_entry(self):
        self._entry = ""
        self.setFocus(QtCore.Qt.OtherFocusReason)
        self.update()

    INT_DIGITS = 4                          # MHz digits always shown: 0154.000000 .. 6000.000000

    def _text(self):
        # zero-padded to INT_DIGITS so the GHz digit is always there to click (left-click
        # 0154.000000's first digit -> 1154.000000); leading zeros are drawn dimmed. Built from
        # the integer Hz, not hz/1e6, so 1 Hz digits never round. During TX the cosmetic TX
        # frequency is shown instead of the tuned one.
        hz = self._tx_hz if self._tx_hz is not None else self._hz
        mhz, rest = divmod(int(hz), 1_000_000)
        return f"{mhz:0{self.INT_DIGITS}d}.{rest // 10 ** (6 - self.FRAC):0{self.FRAC}d}"

    def paintEvent(self, _ev):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#0a0c0f"))
        p.setFont(self._font)
        fm = p.fontMetrics()
        y = (self.height() + fm.ascent() - fm.descent()) // 2
        small = QtGui.QFont(self._font)
        small.setPixelSize(16)

        if self._entry is not None:          # entry mode
            s = self._entry or "----.------"
            x = max(12, (self.width() - fm.horizontalAdvance(s) - 50) // 2)
            p.setPen(QtGui.QColor("#f0b429" if self._entry else "#5a636e"))
            p.drawText(x, y, s)
            p.setFont(small)
            p.setPen(QtGui.QColor("#5a636e"))
            p.drawText(x + fm.horizontalAdvance(s) + 8, y, "MHz")
            self._spans = []
            return

        s = self._text()
        dot = s.index(".")
        tx = self._tx_hz is not None         # cosmetic transmit override: red digits + a TX marker
        bright = "#ff5555" if tx else "#39ff88"      # on-air red (matches the PTT button) vs the RX green
        dim = "#5a2222" if tx else "#1f5a3a"         # dimmed leading zeros, in the matching hue
        subtitle = "TX" if tx else self._subtitle
        # shift the frequency up a little when there's a subtitle line to show
        fy = (y - 10) if subtitle else y
        # the Hz digits after a thin gap, and a little smaller, like a rig's display:
        # 0154.000 000 -> MHz . kHz | Hz
        gap = max(4, fm.horizontalAdvance("0") // 3)
        hz_font = QtGui.QFont(self._font)
        hz_font.setPixelSize(int(self._font.pixelSize() * 0.8))
        hfm = QtGui.QFontMetrics(hz_font)
        split = dot + 1 + self.KHZ_DIGITS          # first Hz digit
        width = (fm.horizontalAdvance(s[:split]) + gap + hfm.horizontalAdvance(s[split:]))
        x = max(12, (self.width() - width - 50) // 2)
        self._spans = []
        lead = len(s) - len(s.lstrip("0"))         # leading zeros (e.g. the 0 of 0154.000000)
        lead = min(lead, dot - 1)                  # ... but never the units-MHz digit
        for i, chr_ in enumerate(s):
            if i == split:
                x += gap
                p.setFont(hz_font)
            w = (hfm if i >= split else fm).horizontalAdvance(chr_)
            p.setPen(QtGui.QColor(dim if i < lead else bright))
            p.drawText(x, fy, chr_)
            if chr_.isdigit():
                place_hz = 10 ** (dot - 1 - i + 6) if i < dot else 10 ** (6 - (i - dot))
                self._spans.append((x, x + w, place_hz))
            x += w
        p.setFont(small)
        p.setPen(QtGui.QColor("#5a636e"))
        p.drawText(x + 8, fy, "MHz")
        if subtitle:                        # known-channel info line, or "TX" during transmit
            sub = QtGui.QFont(self._font)
            sub.setPixelSize(14)
            sub.setBold(tx)
            p.setFont(sub)
            sfm = p.fontMetrics()
            txt = sfm.elidedText(subtitle, QtCore.Qt.ElideRight,
                                 self.width() - 20)
            p.setPen(QtGui.QColor(bright if tx else "#8b95a1"))
            p.drawText((self.width() - sfm.horizontalAdvance(txt)) // 2,
                       self.height() - 8, txt)

    def _place_at(self, x):
        """The tuning step (Hz) of the digit under pixel x, or None."""
        for (x0, x1, place) in self._spans:
            if x0 <= x < x1:
                return place
        return None

    def wheelEvent(self, ev):
        """Wheel over a digit: that place up (away from you) or down, one step per notch."""
        place = self._place_at(ev.x()) if self._entry is None else None
        if place is None:
            ev.ignore()
            return
        self._wheel_accum += ev.angleDelta().y()
        notches = int(self._wheel_accum / 120)
        if notches:
            self._wheel_accum -= notches * 120
            self.setFrequency(self._hz + notches * place)
            self._emit_step(place)
        ev.accept()

    def _emit_step(self, place):
        (self.nudged if place < self.FINE_HZ else self.tuned).emit(self._hz)

    def mousePressEvent(self, ev):
        if self._entry is not None:
            return
        for (x0, x1, place) in self._spans:
            if x0 <= ev.x() < x1:
                if ev.button() == QtCore.Qt.LeftButton:
                    self.setFrequency(self._hz + place)
                elif ev.button() == QtCore.Qt.RightButton:
                    self.setFrequency(self._hz - place)
                else:
                    return
                self._emit_step(place)
                return
        self.setFocus(QtCore.Qt.MouseFocusReason)   # click elsewhere: allow typing

    def keyPressEvent(self, ev):
        k, t = ev.key(), ev.text()
        if self._entry is None:
            if t.isdigit():
                self._entry = t
                self.update()
            else:
                super().keyPressEvent(ev)
            return
        if k in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            self._commit_entry()
        elif k == QtCore.Qt.Key_Escape:
            self._entry = None
            self.update()
        elif k == QtCore.Qt.Key_Backspace:
            self._entry = self._entry[:-1]
            self.update()
        elif t and (t.isdigit() or (t == "." and "." not in self._entry)):
            self._entry += t
            self.update()
        else:
            super().keyPressEvent(ev)

    def _commit_entry(self):
        s, self._entry = self._entry, None
        try:
            self.setFrequency(int(round(float(s) * 1e6)))
            self.tuned.emit(self._hz)
        except (ValueError, TypeError):
            self.update()


class CheckableHeaderView(QtWidgets.QHeaderView):
    """Horizontal table header whose first section is a check-all / uncheck-all
    checkbox controlling column 0 (the per-row 'include' ticks). Clicking the box
    emits ``toggledAll``; ``setChecked`` reflects the rows' aggregate state."""

    toggledAll = QtCore.pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(QtCore.Qt.Horizontal, parent)
        self._checked = False
        self.setSectionsClickable(True)

    def isChecked(self):
        return self._checked

    def setChecked(self, on):
        on = bool(on)
        if on != self._checked:
            self._checked = on
            self.updateSection(0)

    def _box_rect(self, rect):
        sz = 14
        return QtCore.QRect(rect.x() + (rect.width() - sz) // 2,
                            rect.y() + (rect.height() - sz) // 2, sz, sz)

    def paintSection(self, painter, rect, index):
        super().paintSection(painter, rect, index)
        if index != 0:
            return
        opt = QtWidgets.QStyleOptionButton()
        opt.rect = self._box_rect(rect)
        opt.state = QtWidgets.QStyle.State_Enabled | (
            QtWidgets.QStyle.State_On if self._checked
            else QtWidgets.QStyle.State_Off)
        self.style().drawPrimitive(QtWidgets.QStyle.PE_IndicatorCheckBox,
                                   opt, painter)

    def mousePressEvent(self, ev):
        if self.logicalIndexAt(ev.pos()) == 0:
            self.setChecked(not self._checked)
            self.toggledAll.emit(self._checked)
            return
        super().mousePressEvent(ev)


class CheckComboBox(QtWidgets.QComboBox):
    """A drop-down whose items are checkboxes (multi-select). The (read-only)
    line edit shows a summary of the checked items; the popup stays open while
    you tick several. Emits ``changed`` when the checked set changes."""

    changed = QtCore.pyqtSignal()

    def __init__(self, placeholder="", parent=None):
        super().__init__(parent)
        self._placeholder = placeholder
        self.setModel(QtGui.QStandardItemModel(0, 1, self))
        self.setEditable(True)
        self.lineEdit().setReadOnly(True)
        self.lineEdit().setPlaceholderText(placeholder)
        self.lineEdit().installEventFilter(self)           # click -> open popup
        self.view().viewport().installEventFilter(self)    # click -> toggle, keep open

    def eventFilter(self, obj, ev):
        if obj is self.lineEdit() and ev.type() == QtCore.QEvent.MouseButtonRelease:
            self.showPopup()
            return True
        if obj is self.view().viewport() \
                and ev.type() == QtCore.QEvent.MouseButtonRelease:
            idx = self.view().indexAt(ev.pos())
            if idx.isValid():
                it = self.model().itemFromIndex(idx)
                it.setCheckState(QtCore.Qt.Unchecked
                                 if it.checkState() == QtCore.Qt.Checked
                                 else QtCore.Qt.Checked)
                self._update_text()
                self.changed.emit()
            return True                                    # swallow -> stays open
        return super().eventFilter(obj, ev)

    def set_items(self, names, checked):
        """Populate with ``names``, ticking those in ``checked``."""
        m = self.model()
        m.clear()
        checked = set(checked or [])
        for n in names:
            it = QtGui.QStandardItem(n)
            it.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            it.setData(QtCore.Qt.Checked if n in checked else QtCore.Qt.Unchecked,
                       QtCore.Qt.CheckStateRole)
            m.appendRow(it)
        self._update_text()

    def checked_items(self):
        m = self.model()
        return [m.item(i).text() for i in range(m.rowCount())
                if m.item(i).checkState() == QtCore.Qt.Checked]

    def _update_text(self):
        chk = self.checked_items()
        self.lineEdit().setText(", ".join(chk))            # blank -> placeholder shows


class _WheelGuard(QtCore.QObject):
    """Stop the mouse wheel from EVER changing a spin box / combo / slider — the
    wheel scrolls the enclosing page instead. Adjust these controls deliberately
    (click the arrows, drag the slider, type, or use the arrow keys).

    Focus is intentionally NOT an exception: a combo/spin keeps keyboard focus
    after you click it to make a selection, so a later stray scroll over it would
    still change the value. That silent drift (Active bank, Dwell, gains, …) is
    exactly the confusing 'why did the scan behavior change?' bug this prevents."""

    _TYPES = (QtWidgets.QAbstractSpinBox, QtWidgets.QComboBox, QtWidgets.QSlider)

    def eventFilter(self, obj, ev):
        if ev.type() != QtCore.QEvent.Wheel or not isinstance(obj, self._TYPES):
            return False
        # redirect the wheel to the nearest scroll area so the page still scrolls
        area = obj.parent()
        while area is not None and not isinstance(
                area, QtWidgets.QAbstractScrollArea):
            area = area.parent()
        if area is not None:
            QtWidgets.QApplication.sendEvent(area.viewport(), ev)
        return True                           # never let the control consume it


class TxWatcher(QtCore.QThread):
    """Watches a TxChain for the flowgraph finishing on its own — a non-looping
    file-source TX hits EOF — and reports back on the GUI thread so PTT can
    auto-release. Never fires for a mic source or a looping file (see
    TxChain.wait())."""
    finished_tx = QtCore.pyqtSignal(object)     # emits self, so the slot can
                                                 # ignore a stale watcher

    def __init__(self, chain, parent=None):
        super().__init__(parent)
        self.chain = chain

    def run(self):
        self.chain.wait()
        self.finished_tx.emit(self)


class SnapshotWriter(QtCore.QThread):
    """Freezes the radio's snapshot ring (waits SNAPSHOT_POST_S past the click),
    then writes it as a band capture, or cuts a sub-band out of it, plus the
    usual .json sidecar and the waterfall rows that were on screen. Runs off
    the GUI thread: a 10 s, 19.2 Msps band snapshot is ~770 MB."""
    done = QtCore.pyqtSignal(str, str)          # (path or "", status text)

    def __init__(self, radio, path_for, wf_rows, subband=None, parent=None, post_s=None,
                 pre_s=None):
        super().__init__(parent)
        self.radio = radio
        self.path_for = path_for                # meta -> output path
        self.wf_rows = wf_rows                  # [rows, NFFT] float32 dB, newest last
        self.subband = subband                  # (center_hz, bw_hz) or None for the band
        self.post_s = post_s                    # None = SNAPSHOT_POST_S after the click
        self.pre_s = pre_s                      # None = the whole buffer before the click

    def run(self):
        try:
            iq, meta = (self.radio.take_snapshot() if self.post_s is None
                        else self.radio.take_snapshot(self.post_s, self.pre_s))
            if iq is None:
                self.done.emit("", "snapshot: nothing buffered yet")
                return
            path = self.path_for(meta)
            if self.subband is None:
                _iqring.write_band(path, iq)
            else:
                center, bw = self.subband
                rate = _iqring.write_subband(path, iq, meta["samp_rate"],
                                             center - meta["center_hz"], bw)
                meta.update(center_hz=int(center), samp_rate=rate,
                            rf_bw_hz=int(bw), scope="subband")
            del iq
            meta["file"] = os.path.basename(path)
            if self.wf_rows is not None and len(self.wf_rows):
                _iqring.save_rows(path + ".waterfall.npy", self.wf_rows)
                meta["snapshot"]["waterfall"] = {
                    "file": os.path.basename(path) + ".waterfall.npy",
                    "row_s": WATERFALL_ROW_S, "nfft": int(self.wf_rows.shape[1]),
                    "samp_rate": float(self.radio.samp_rate)}
            with open(path + ".json", "w") as fh:
                json.dump(meta, fh, indent=2)
            mb = os.path.getsize(path) / 1e6
            self.done.emit(path, f"snapshot {os.path.basename(path)} "
                                 f"({meta['snapshot']['duration_s']:.1f} s, {mb:.0f} MB)")
        except Exception as e:                  # disk full etc.: report, don't kill the app
            self.done.emit("", f"snapshot failed: {e}")


class ScannerApp(QtWidgets.QMainWindow):
    _PTT_TOOLTIP = ("Push-to-talk: stops the receiver, transmits "
                    "audio (mic or file — see Settings ▸ Transmit…) on "
                    "the tuned frequency using the current mode "
                    "(NFM/AM/WFM/USB/LSB), then restores the receiver. "
                    "Only on a device that reports a transmitter, and only "
                    "inside the US amateur bands unless the override is set.")

    def __init__(self, use_gpu=None):
        super().__init__()
        self._use_gpu_arg = use_gpu              # --no-gpu / --gpu override (None = auto)
        # modules (sdrexplorer.modules / .api): found and probed before anything is built, so their
        # branches can attach to the radio and their menus / modes to the UI as each is built
        self.bus = EventBus()
        self.audio = AudioArbiter()
        self.tuner = Tuner()                     # who may move the band (scan, ADS-B park, modules)
        self._mode_specs = {}                    # mode name -> (module, api.ModeSpec)
        self._module_mode = None                 # the module mode the Mode picker is in, or None
        self._module_actions = []                # QActions modules added
        self.modules = _modules.ModuleHost(builtin=self.BUILTIN_MODULES)
        self.store = BankStore(_memory_path())
        os.makedirs(_uls_dir(), exist_ok=True)      # auto-create the FCC data dir

        st = self._load_state()
        self._state = st
        self._init_volume = int(st.get("volume", 40))
        self._init_squelch = int(st.get("squelch", -70))
        self._init_margin = int(st.get("auto_margin", 6))
        self._init_autosq = bool(st.get("auto_squelch", False))
        mode = st.get("mode", "NFM")
        if mode not in ANALOG_MODES:
            mode = "NFM"
        # dual VFO: two independent {freq, mode} memories flipped via the A/B
        # buttons. VFO-A defaults to the last-tuned freq; VFO-B to a separate slot.
        self._active_vfo = st.get("vfo_active") if st.get("vfo_active") in ("A", "B") \
            else "A"
        a_freq = int(st.get("freq_hz", 154_000_000))
        self._vfos = {
            "A": dict(st.get("vfo_a") or {"freq": a_freq, "mode": mode}),
            "B": dict(st.get("vfo_b") or {"freq": 462_562_500, "mode": "NFM"}),
        }
        start = self._vfos[self._active_vfo]
        from . import sources as _sources
        self._sources = _sources
        _sources.scan()                       # the attached devices (~1 s; also lists remembered ones)
        self._sdr_device = st.get("sdr_device", "HackRF")
        if self._sdr_device == IQ_FILE_DEVICE:
            self._sdr_device = "HackRF"       # never auto-run a file replay at launch
        dev = _sources.find(self._sdr_device) or next((d for d in _sources.devices() if d.present), None)
        if dev is not None:
            self._sdr_device = dev.key        # a device key from now on (an old name maps to its family)
        self._hw_device = self._sdr_device    # last real device, for persistence
        self._tx_source = st.get("tx_source", "mic")
        if self._tx_source not in ("mic", "file"):
            self._tx_source = "mic"
        self._tx_file = st.get("tx_file") or None
        self._tx_loop = bool(st.get("tx_loop", False))
        # the TX: picker: "Analog" (follows the receive mode) or a module's transmit mode, which
        # registers later (api.TxModeSpec) -- the saved choice is kept until then
        self._tx_mode_saved = st.get("tx_digital_mode", "Analog")
        self._tx_digital_mode = "Analog"
        self._tx_specs = {}
        tg = st.get("tx_gains") or {}
        self._tx_rf_gain = int(tg.get("rf", 0))
        self._tx_if_gain = int(tg.get("if", 20))
        self._offset_dir = st.get("offset_dir", "Simplex")
        if self._offset_dir not in ("Simplex", "−", "+"):
            self._offset_dir = "Simplex"
        self._offset_mhz = float(st.get("offset_mhz", 0.600))
        self._ctcss_hz = float(st.get("ctcss_hz", 100.0))
        if self._ctcss_hz not in CTCSS_TONES:
            self._ctcss_hz = 100.0
        self._ctcss_tx = bool(st.get("ctcss_tx", False))
        self._ctcss_rx = bool(st.get("ctcss_rx", False))
        # transmit is confined to US amateur bands (radio.in_us_ham_band) unless a
        # licensed operator ticks this override in Settings > Transmit
        self._tx_ham_override = bool(st.get("tx_ham_override", False))
        # auto-fill the repeater TX offset from the US band convention on a user tune
        # (radio.standard_repeater_offset); suspended once the user sets the offset by
        # hand, until the convention's value changes (a new band / 2 m sub-band flip).
        # _offset_auto_std is the standard offset last auto-applied.
        self._offset_auto_std = None
        self._offset_user_set = False
        samp_rate = int(st.get("samp_rate", SAMP_RATE))
        if samp_rate not in self._rates_for(self._sdr_device):
            samp_rate = SAMP_RATE             # a rate the device doesn't take: the default
        self.radio = ScannerRadio(
            freq_hz=int(start.get("freq", a_freq)),
            mode_name=start.get("mode", mode) if start.get("mode") in ANALOG_MODES
            else mode,
            device=self._sdr_device,
            samp_rate=samp_rate,
            use_gpu=self._use_gpu_arg)
        if self.radio.audio_error:            # no sound device: say so once the window is up
            QtCore.QTimer.singleShot(0, lambda: self.statusBar().showMessage(
                "No audio output device: running without sound "
                f"({self.radio.audio_error}). Check your sound settings and restart.", 30000))
        if dev is not None:
            self.radio.devinfo = dev
            self.radio.dev_settings = _sources.settings(dev)
            self._prune_foreign_gains(dev)        # drop stray stages (e.g. an old cross-device "LNA")
            # one-time migration of the old device-agnostic gains block (state.json "gains") onto this
            # device, if it has no per-device gains saved yet (sources.json). After this the global
            # block is never read again -- gains are per device from here on.
            self._migrate_legacy_gains(dev, st.get("gains") or {})
        self.radio.set_volume(self._init_volume / 100.0)
        self.audio.bind(self.radio)              # module audio inputs go into the speaker chain
        station.bind_radio(self.radio)           # api.station's clock
        station.on_change(lambda loc: self.bus.publish("station_changed", location=loc))
        self.modules.remember_sections(st)
        self.modules.attach_radio(self.radio)
        self.radio.snapshot_seconds = max(0, int(st.get("snapshot_s", SNAPSHOT_SECONDS)))
        self._snap_writer = None      # SnapshotWriter while a snapshot is being saved
        # signal-triggered sub-band recording (right-drag ▸ Record when a signal appears)
        self._trigger = None          # {"center", "bw", "mon": TriggerMonitor} while armed
        self._trigger_db = float(st.get("trigger_db", TRIGGER_THRESHOLD_DB))
        self._trigger_hang = float(st.get("trigger_hang_s", TRIGGER_HANG_S))
        self._trigger_max = float(st.get("trigger_max_s", TRIGGER_MAX_S))
        self._trigger_pre = bool(st.get("trigger_pre", True))   # save a pre-roll from the ring
        self._trigger_pre_s = float(st.get("trigger_pre_s", TRIGGER_PRE_S))   # ... this long

        self.tx = None                # active TxChain (radio.py) while PTT is held
        self._tx_watcher = None       # TxWatcher (QThread): detects file-source EOF
        self._resume_rx = False       # restart RX after PTT release (it was running)

        self.scan_worker = None
        self.fetch_worker = None
        self.disc_worker = None
        self.dl_worker = None
        self.geo_worker = None
        self._decode_seen = {}      # (source, dedup_key) -> time last logged (api.Decode records)
        self.rds_monitor = None     # RDS field poller (QThread), active in WFM
        self._last_rds = None       # dedup key for the RDS log line
        self._rds_ps = ""           # latest decoded station name (WFM), "" = none yet
        self.adsb_monitor = None    # ADS-B aircraft poller (QThread), active in ADS-B
        self._in_adsb = False       # currently in the (non-MODES) ADS-B mode
        self._pre_adsb = None       # (freq, mode) to restore when leaving ADS-B
        self._adsb_win = None       # aircraft-table window (lazy)
        self._adsb_rows = {}        # ICAO -> table row
        self._adsb_seen = {}        # ICAO -> last-seen epoch seconds
        # Live map: shared target store fed by ADS-B aircraft and modules' positions
        # (api.Position), drawn in a lazy QWebEngineView.
        self.targets = TargetStore()
        self._map_win = None
        self._map_view = None
        self._map_timer = None
        self._map_ready = False     # set True on QWebEngineView loadFinished
        self._map_theme = "dark"    # "dark" | "light" (matches the app's dark UI)
        self._auto_mute = bool(st.get("auto_mute", True))   # honor modules' mute_analog requests
        self._payloads = []              # captured raw frame payloads (for hex/save)
        self._dig_lines = []             # backing store of all decode-log lines
        self._dig_refs = []              # each line's Decode.ref (None: not linked to a payload)
        self._dig_seq = 0                # a ref for a record that has none of its own
        self._match_refs = set()         # refs of the lines the log filter matches ("Follow log filter")
        self._hex_follow = bool(st.get("payload_follow", False))
        self._dig_filter = ""            # live text filter for the decode log
        self._hex_filter = ""            # live text filter for the payload pane
        self._last_payload = None        # dedup consecutive identical payloads
        self._payload_cap = 5000         # bound the in-memory payload buffer
        self._show_payload_hex = bool(st.get("payload_hex", False))
        self._snap_enabled = bool(st.get("snap", False))     # waterfall-click snap
        self._snap_khz = float(st.get("snap_khz", 12.5))     # channel grid (kHz)
        self._prio_enabled = bool(st.get("priority_scan", False))
        self._prio_interval = float(st.get("priority_interval", 2.0))
        self._wf_during_scan = bool(st.get("waterfall_scan", False))
        self._wf_labels = bool(st.get("wf_labels", True))    # channel-name overlay
        # what the waterfall labels show: memory names, modules' annotations, or both. A saved choice this
        # version doesn't know (an older name for the annotations) means the annotations.
        self._label_source = st.get("label_source", "memories")  # memories|annotations|both
        if self._label_source not in ("memories", "annotations", "both"):
            self._label_source = "annotations"
        self._show_recorded = bool(st.get("show_recorded", True))   # recorded-frequency badges
        self._uls_win = None        # FCC license search (ulsui.UlsSearchWindow), made on first use
        # user-adjustable label style (Settings ▸ Label style…): waterfall
        # channel labels and map target labels.
        self._wf_label_px = int(st.get("wf_label_px", 12))
        self._wf_label_color = str(st.get("wf_label_color", "#ffffff"))
        self._wf_tick_color = str(st.get("wf_tick_color", "#39ff88"))
        self._map_label_px = int(st.get("map_label_px", 12))
        self._map_label_color = str(st.get("map_label_color", "#f2f2f2"))
        self._label_dialog = None
        self._suppress_row_tune = False       # guard programmatic table changes
        self._rec_path = None       # active IQ recording path (None = not recording)
        self._rec_elapsed = 0
        self._rec_labels = None     # RecordingLabels tallying decodes during a recording
        self._reaping = []          # detached workers winding down after cancel
        self._scanning = False
        self._scan_holding = False  # scan is parked on an active channel
        self._resetting = False     # True while tearing down for a settings reset
        # which bank(s) to scan (main-UI multi-select). Empty => the active bank.
        self._scan_banks = [b for b in (st.get("scan_banks") or []) if isinstance(b, str)]
        self._scan_list = []        # [(bank_name, Channel)] built at scan start
        # live client-side filter over the active bank
        self._filter_center = None  # (lat, lon) resolved from the Where box
        self._filter_label = ""
        self._row_bankidx = []      # table row -> index into the active bank
        # The Memories view edits the ACTIVE bank directly; the merged table's
        # mode/service/text filter narrows only the *view* (never what a scan
        # iterates — see _scan_channels).
        self._src_search = str(st.get("src_search", "")).strip().lower()

        self._build_ui()
        self._attach_module_ui(st)
        # wheel-guard every scrollable control so scrolling the page can't
        # silently change spin boxes / combos / sliders under the cursor
        self._wheel_guard = _WheelGuard(self)
        QtWidgets.QApplication.instance().installEventFilter(self._wheel_guard)
        self._start_radio()             # auto-run the last SDR if it's connected
        # mode_combo's initial setCurrentText() (in _build_ui, above) predates
        # its currentTextChanged connection, so _enter_mode() never runs for
        # whichever mode was restored from state.json -- start the matching
        # parallel decode branch here instead, or a restored WFM session
        # silently never decodes anything until the mode is re-picked.
        if self.radio.mode.name == "WFM":
            self._start_rds()
        self._refresh_bank_combo()
        self._refresh_table()
        self._restore_ui_state(st)

        # recorded-frequency badges: rebuilt now, every 30 s (new files in recordings/), and
        # right after a recording / snapshot / "Label as..." finishes
        self._rec_index_timer = QtCore.QTimer(self)
        self._rec_index_timer.timeout.connect(self._refresh_recorded)
        self._rec_index_timer.start(30_000)
        self._refresh_recorded()


        # autosave the session state periodically (survives a non-clean exit too)
        self.state_timer = QtCore.QTimer(self)
        self.state_timer.timeout.connect(self._save_state)
        self.state_timer.start(15_000)


        # updates the "● REC Ns / MB" readout while an IQ recording runs
        self._rec_timer = QtCore.QTimer(self)
        self._rec_timer.timeout.connect(self._update_rec_status)

        # a module's mode restored from state.json: entered last, once the whole window exists
        if st.get("module_mode") in self._mode_specs:
            self.mode_combo.setCurrentText(st["module_mode"])     # -> _enter_mode

    # =====================================================================
    #  Modules (sdrexplorer.modules / sdrexplorer.api)
    # =====================================================================
    # Core features still inside the core that will move into module repos register here as
    # builtin modules until they do.
    BUILTIN_MODULES = ()

    def service(self, name):
        """A loaded module's service object (api.Module.services), or None."""
        return self._services.get(name)

    def _call_module(self, module, what, fn, *args):
        try:
            return fn(*args)
        except Exception:
            print(f"[sdrexplorer] module {module.name}: {what} failed:", file=sys.stderr)
            import traceback
            traceback.print_exc()
            return None

    def _attach_module_ui(self, st):
        """attach_ui every loaded module, then add what each declares (modes, menus, overlays,
        services), then hand each its saved state section."""
        self._services = {}
        self._decoders = {}                      # label -> [(module, handler)], first registered first
        for m, (name, obj) in self.modules.collect("services"):
            self._services.setdefault(name, obj)
        for m in self.modules.loaded:
            self._call_module(m, "attach_ui", m.attach_ui, self, ModuleUI(self, m))
        for m, spec in self.modules.collect("modes"):
            self._register_mode(m, spec)
        for m, e in self.modules.collect("menu_entries"):
            self._add_module_menu_entry(m, e)
        for m, o in self.modules.collect("overlays"):
            self.spectrum.add_overlay(o)
        for m, p in self.modules.collect("panels"):
            self._add_module_panel(m, p)
        for m, spec in self.modules.collect("tx_modes"):
            self._register_tx_mode(m, spec)
        for m, (label, handler) in self.modules.collect("decodes"):
            self._decoders.setdefault(label, []).append((m, handler))
        for m, prov in self.modules.collect("importers"):
            importers.register(prov, owner=m.name)
        self._refresh_importers()
        self.modules.load_state(st)

    def request_decode(self, label, freq_hz, info=None):
        """Ask whichever module decodes signals labeled ``label`` (api.Module.decodes) to take the one at
        freq_hz. True if one did; False (nothing happens) when no installed module decodes it."""
        for m, handler in getattr(self, "_decoders", {}).get(label, ()):
            if self._call_module(m, f"decode {label}", handler, int(freq_hz), label, dict(info or {})):
                return True
        return False

    def decodable_labels(self):
        """Every label some loaded module decodes."""
        return sorted(getattr(self, "_decoders", {}))

    def publish(self, record, module=None):
        """A module's api.Decode: decode log (with the core's station tag, deduplicated by its key),
        payload pane, recording labels, and the "decode" topic."""
        if record.counts and self._rec_labels is not None:
            self._rec_labels.feed_decode(record)
        ref = record.ref
        if ref is None:                         # a record's own line and payload are always linked
            self._dig_seq += 1
            ref = ("rec", self._dig_seq)
        if record.log and record.text:
            key = (record.source, record.dedup_key) if record.dedup_key is not None else None
            now = time.monotonic()
            last = self._decode_seen.get(key) if key is not None else None
            if last is None or now - last >= record.dedup_s:
                if key is not None:
                    self._decode_seen[key] = now
                    if len(self._decode_seen) > 4000:          # bounded: forget the oldest half
                        for k in sorted(self._decode_seen, key=self._decode_seen.get)[:2000]:
                            del self._decode_seen[k]
                station = self._decode_station(record.protocol, record.ids)
                self._dig_append(f"{station}  {record.text}", ref=ref)
        if record.payload:
            self._capture_payload(self._decode_station(record.protocol, record.ids), record.protocol,
                                  record.text, record.payload, record.linktype, ref=ref)
        self.bus.publish("decode", record=record)

    def publish_position(self, record):
        """A module's api.Position: onto the map's targets, and the "position" topic."""
        self.targets.upsert(record.id, record.kind, lat=record.lat, lon=record.lon, label=record.label,
                            **dict(record.attrs))
        self.bus.publish("position", record=record)

    def _add_module_panel(self, module, panel):
        """A module's api.Panel: a tab in the decode strip, or a widget in the status bar."""
        try:
            if panel.area == "status":
                panel.widget = panel.factory(self)
                self.statusBar().addPermanentWidget(panel.widget)
                return
            if panel.area != "decode":
                print(f"[sdrexplorer] module {module.name}: panel area {panel.area!r} unknown, "
                      "using the decode strip", file=sys.stderr)
            panel.widget = panel.factory(self.module_tabs)
            self.module_tabs.addTab(panel.widget, panel.title)
            self.module_tabs.setVisible(True)
        except Exception:
            print(f"[sdrexplorer] module {module.name}: panel {panel.title!r} failed:", file=sys.stderr)
            import traceback
            traceback.print_exc()

    def _register_tx_mode(self, module, spec):
        """A module's api.TxModeSpec: an entry in the TX: picker, its fields in Settings > Transmit."""
        if spec.name in self._tx_specs or spec.name in TX_MODES or spec.name == "Analog":
            print(f"[sdrexplorer] module {module.name}: TX mode {spec.name!r} already exists, skipped",
                  file=sys.stderr)
            return
        self._tx_specs[spec.name] = (module, spec)
        if hasattr(self, "tx_mode_combo"):
            c = self.tx_mode_combo
            c.blockSignals(True)
            c.addItem(spec.name)
            ok, why = self._mode_available(spec)
            tip = spec.tooltip or f"{spec.name} ({module.title or module.name})"
            c.setItemData(c.count() - 1, tip if ok else f"Unavailable: {why}", QtCore.Qt.ToolTipRole)
            if spec.name == self._tx_mode_saved:
                self._tx_digital_mode = spec.name
                c.setCurrentText(spec.name)
            c.blockSignals(False)
        if spec.settings_widget is not None and hasattr(self, "_tx_ext_box"):
            try:
                w = spec.settings_widget(self._tx_ext_box)
                if w is not None:
                    self._tx_ext_box.layout().addWidget(w)
            except Exception:
                print(f"[sdrexplorer] module {module.name}: TX settings for {spec.name!r} failed:",
                      file=sys.stderr)
                import traceback
                traceback.print_exc()
        if spec.quick_widget is not None and hasattr(self, "_tx_quick_box"):
            try:
                qw = spec.quick_widget(self._tx_quick_box)
                if qw is not None:
                    self._tx_quick_lay.addWidget(qw)
                    qw.setVisible(False)
                    self._tx_quick_widgets[spec.name] = qw
            except Exception:
                print(f"[sdrexplorer] module {module.name}: TX quick widget for {spec.name!r} failed:",
                      file=sys.stderr)
                import traceback
                traceback.print_exc()
        self._show_tx_quick_widget()
        self._update_ptt_enabled()

    def _show_tx_quick_widget(self):
        """Show only the selected TX mode's quick widget (hide the rest / all for Analog)."""
        if not hasattr(self, "_tx_quick_widgets"):
            return
        active = self._tx_digital_mode if self._tx_digital_mode in self._tx_specs else None
        for name, w in self._tx_quick_widgets.items():
            w.setVisible(name == active)

    def _tx_spec(self, name=None):
        hit = self._tx_specs.get(self._tx_digital_mode if name is None else name)
        return hit[1] if hit else None

    def _mode_available(self, spec):
        if spec.available is None:
            return True, ""
        try:
            return spec.available()
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def _register_mode(self, module, spec):
        if spec.name in self._mode_specs or spec.name in MODES or spec.name == "ADS-B":
            print(f"[sdrexplorer] module {module.name}: mode {spec.name!r} already exists, skipped",
                  file=sys.stderr)
            return
        self._mode_specs[spec.name] = (module, spec)
        demod = spec.demod
        if spec.picker != "mode":
            print(f"[sdrexplorer] module {module.name}: mode {spec.name!r}: picker {spec.picker!r} is not "
                  "supported; listed in the Mode picker", file=sys.stderr)
        if demod is not None and not isinstance(demod, str):
            from .radio import mode_from_spec
            self.radio.register_mode(mode_from_spec(spec.name, demod))
        combo = self.mode_combo
        combo.blockSignals(True)
        combo.addItem(spec.name)
        combo.blockSignals(False)
        ok, why = self._mode_available(spec)
        tip = spec.tooltip or f"{spec.name} ({module.title or module.name})"
        combo.setItemData(combo.count() - 1, tip if ok else f"Unavailable: {why}", QtCore.Qt.ToolTipRole)

    def _add_module_menu_entry(self, module, e):
        menu = self._menus.get(e.menu) or self._menus["Settings"]
        if e.menu not in self._menus:
            print(f"[sdrexplorer] module {module.name}: no {e.menu!r} menu, using Settings",
                  file=sys.stderr)
        a = QtWidgets.QAction(e.text, self, checkable=e.checkable)
        if e.checkable:
            a.setChecked(e.checked)
            if e.toggled is not None:
                a.toggled.connect(lambda on, fn=e.toggled: self._call_module(module, e.text, fn, on))
        elif e.triggered is not None:
            a.triggered.connect(lambda _c=False, fn=e.triggered: self._call_module(module, e.text, fn))
        a.setEnabled(e.enabled)
        if e.tooltip:
            a.setToolTip(e.tooltip)
        menu.addAction(a)
        e.action = a
        self._module_actions.append(a)
        return e

    def _open_modules_dialog(self):
        ModulesDialog(self, self.modules).exec_()

    # =====================================================================
    #  Session-state persistence
    # =====================================================================
    def _load_state(self):
        try:
            with open(_state_path()) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _reset_settings(self):
        """Delete the saved settings (and, if asked, all banks) and relaunch, so
        the app comes up on stock defaults — a clean slate for debugging."""
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Reset to defaults")
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setText("Reset all settings to defaults and restart?")
        box.setInformativeText(
            "Radio gains, squelch, filters, snap, search text and window size "
            "return to defaults. Your saved banks are kept unless you tick below.")
        also = QtWidgets.QCheckBox("Also delete ALL banks and channels")
        box.setCheckBox(also)
        box.setStandardButtons(QtWidgets.QMessageBox.Reset
                               | QtWidgets.QMessageBox.Cancel)
        box.setDefaultButton(QtWidgets.QMessageBox.Cancel)
        if box.exec_() != QtWidgets.QMessageBox.Reset:
            return
        self._resetting = True                 # closeEvent must not re-save state
        wipe_banks = also.isChecked()
        self.close()                           # full teardown (releases the HackRF)
        for path, doit in ((_state_path(), True), (_memory_path(), wipe_banks)):
            if doit:
                try:
                    os.remove(path)
                except OSError:
                    pass
        # re-exec as a package (relative imports need `-m`), inheriting the
        # launcher's clean environment
        try:
            os.execv(sys.executable, [sys.executable, "-m", "sdrexplorer.app"])
        except OSError:
            QtWidgets.QApplication.quit()

    def _restore_ui_state(self, st):
        """Apply saved state to widgets that exist only after _build_ui."""
        if st.get("bank") in self.store.banks:
            self.store.set_active(st["bank"])
            self._refresh_bank_combo()
            self._refresh_table()
        if st.get("source") and self.source_combo.findText(st["source"]) >= 0:
            self.source_combo.setCurrentText(st["source"])
        if st.get("radius_mi"):
            self.radius_spin.setValue(int(st["radius_mi"]))
        if st.get("location"):
            self.loc_edit.setText(st["location"])
        # restore the live filter (mode / services / no-GPS)
        self._refresh_mode_filter()             # modules' labels and the banks' modes are known now
        if st.get("mode_filter") and \
                self.mode_filter.findText(st["mode_filter"]) >= 0:
            self.mode_filter.setCurrentText(st["mode_filter"])
        if "radius_incl_nogps" in st:       # renamed key: old default was True,
            self.nogps_cb.setChecked(bool(st["radius_incl_nogps"]))
        svcs = st.get("services")
        if svcs:                              # specific services were ticked
            for tag in svcs:
                if tag in self.filter_cbs:
                    self.filter_cbs[tag].setChecked(True)   # unchecks "All"
        self._apply_filter()
        geo = st.get("geometry")
        if geo and len(geo) == 4:
            self.move(int(geo[0]), int(geo[1]))
            self.resize(int(geo[2]), int(geo[3]))
            self._restored_geometry = True

    def _save_state(self):
        self._store_current_vfo()              # capture the live tune into A or B
        self._save_device_settings()
        state = {
            "freq_hz": int(self.radio.freq_hz),
            "mode": self.radio.mode.name,
            "module_mode": self._module_mode,
            "vfo_active": self._active_vfo,
            "vfo_a": self._vfos["A"],
            "vfo_b": self._vfos["B"],
            "scan_banks": (self.scan_bank_combo.checked_items()
                           if hasattr(self, "scan_bank_combo") else self._scan_banks),
            "sdr_device": getattr(self, "_hw_device", "HackRF"),   # never "IQ file"
            "samp_rate": self.radio.samp_rate,
            # gains are per device (sources.json via _save_device_settings), not here
            "tx_source": self._tx_source,
            "tx_file": self._tx_file,
            "tx_loop": self._tx_loop,
            "tx_digital_mode": self._tx_mode_saved,      # also a module's TX mode not loaded this run
            "tx_gains": {"rf": self._tx_rf_gain, "if": self._tx_if_gain},
            "offset_dir": self._offset_dir,
            "offset_mhz": self._offset_mhz,
            "ctcss_hz": self._ctcss_hz,
            "ctcss_tx": self._ctcss_tx,
            "ctcss_rx": self._ctcss_rx,
            "tx_ham_override": self._tx_ham_override,
            "volume": self._init_volume,
            "squelch": int(self._sq_manual),
            "auto_squelch": self.autosq_cb.isChecked(),
            "auto_margin": int(self._sq_margin),
            "bank": self.store.active,
            "source": self.source_combo.currentText(),
            "radius_mi": self.radius_spin.value(),
            "location": self.loc_edit.text().strip(),
            "mode_filter": self.mode_filter.currentText(),
            "radius_incl_nogps": self.nogps_cb.isChecked(),
            "services": self._filter_services(),   # [] when "All"
            "src_search": (self.src_search.text().strip()
                           if hasattr(self, "src_search") else self._src_search),
            "payload_hex": (self.dig_hex_cb.isChecked()
                            if hasattr(self, "dig_hex_cb") else self._show_payload_hex),
            "payload_follow": self._hex_follow,
            "auto_mute": self._auto_mute,
            "snap": self._snap_enabled,
            "snap_khz": self._snap_khz,
            "snapshot_s": self.radio.snapshot_seconds,
            "trigger_db": self._trigger_db,
            "trigger_hang_s": self._trigger_hang,
            "trigger_max_s": self._trigger_max,
            "trigger_pre": self._trigger_pre,
            "trigger_pre_s": self._trigger_pre_s,
            "priority_scan": (self.prio_cb.isChecked()
                              if hasattr(self, "prio_cb") else self._prio_enabled),
            "priority_interval": (self.prio_spin.value()
                                  if hasattr(self, "prio_spin")
                                  else self._prio_interval),
            "waterfall_scan": (self.act_wf_scan.isChecked()
                               if hasattr(self, "act_wf_scan")
                               else self._wf_during_scan),
            "wf_labels": (self.act_wf_labels.isChecked()
                          if hasattr(self, "act_wf_labels") else self._wf_labels),
            "label_source": self._label_source,
            "show_recorded": self._show_recorded,
            "wf_label_px": int(self._wf_label_px),
            "wf_label_color": self._wf_label_color,
            "wf_tick_color": self._wf_tick_color,
            "map_label_px": int(self._map_label_px),
            "map_label_color": self._map_label_color,
            "geometry": [self.x(), self.y(), self.width(), self.height()],
        }
        self.modules.save_state(state)
        try:
            os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
            tmp = _state_path() + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp, _state_path())
        except OSError:
            pass

    # =====================================================================
    #  UI construction
    # =====================================================================
    def _build_ui(self):
        self.setWindowTitle("sdrexplorer — SDR signal explorer")
        self.setWindowIcon(make_scanner_icon())
        self.setMenuBar(self._build_menubar())

        # Build the Scan-settings and RF-gains dialog *content* eagerly so their
        # widgets (dwell/hang/priority, gains, scan_status, reset) exist for the
        # handlers and state-restore even before a dialog is first opened.
        self._scan_ctl_widget = self._build_scan_controls()
        self._rf_ctl_widget = self._build_rf_controls()
        self._rebuild_gain_controls()
        self._tx_ctl_widget = self._build_tx_controls()
        self._scan_dialog = None
        self._rf_dialog = None
        self._tx_dialog = None

        # Two full-window views swapped by a QStackedWidget: the Radio page
        # (maximized waterfall + digital decode) and the Memories page (bank /
        # data-set management only, no waterfall). Flip between them via the View
        # menu, the on-screen buttons, or Ctrl+M.
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self._build_radio_page())      # index 0
        self.stack.addWidget(self._build_memories_page())   # index 1
        self.stack.currentChanged.connect(self._on_view_changed)
        self.setCentralWidget(self.stack)
        QtWidgets.QShortcut(QtGui.QKeySequence("Ctrl+M"), self,
                            activated=self._toggle_view)

        # live signal / squelch indicator, polled off the same probe
        self.sig_timer = QtCore.QTimer(self)
        self.sig_timer.timeout.connect(self._update_signal)
        self.sig_timer.start(120)      # ~8 Hz: snappy software-squelch response

        # non-looping IQ-file replay reached EOF: stop the radio so Start/Stop
        # (and the SDR status) reflect it -- the flowgraph never stops by itself
        self._replay_eof_timer = QtCore.QTimer(self)
        self._replay_eof_timer.timeout.connect(self._check_replay_eof)
        self._replay_eof_timer.start(500)

        self._apply_style()
        self.setMinimumSize(560, 420)         # can shrink to fit small screens
        self.resize(900, 760)
        # centered only when there's no saved geometry to restore (see
        # _restore_ui_state, which runs after _build_ui)
        if not self._state.get("geometry"):
            self._center_on_screen()

    def _center_on_screen(self):
        """Open centered on the screen under the cursor, clamped to fit that
        screen's available geometry (so the window never opens off-screen)."""
        try:
            screen = QtWidgets.QApplication.screenAt(QtGui.QCursor.pos())
            avail = (screen or QtWidgets.QApplication.primaryScreen()).availableGeometry()
            w = min(self.width(), avail.width() - 40)
            h = min(self.height(), avail.height() - 60)
            self.resize(w, h)
            self.move(avail.x() + (avail.width() - w) // 2,
                      avail.y() + (avail.height() - h) // 2)
        except Exception:
            pass

    # =====================================================================
    #  Menu bar + full-window view switch (Radio <-> Memories)
    # =====================================================================
    def _build_menubar(self):
        mb = QtWidgets.QMenuBar(self)

        # --- View: Radio / Memories switch + waterfall overlay toggles -------
        view = mb.addMenu("&View")
        self._view_group = QtWidgets.QActionGroup(self)
        self._view_group.setExclusive(True)
        self.act_view_radio = QtWidgets.QAction("&Radio", self, checkable=True)
        self.act_view_radio.setChecked(True)
        self.act_view_mem = QtWidgets.QAction("&Memories", self, checkable=True)
        self._view_group.addAction(self.act_view_radio)
        self._view_group.addAction(self.act_view_mem)
        self.act_view_radio.triggered.connect(self._show_radio)
        self.act_view_mem.triggered.connect(self._show_memories)
        view.addAction(self.act_view_radio)
        view.addAction(self.act_view_mem)
        view.addSeparator()
        # These replace the old on-header checkboxes. setChecked BEFORE connecting
        # so the init doesn't fire the slot (which touches self.spectrum, built later).
        self.act_wf_labels = QtWidgets.QAction("Waterfall &labels", self,
                                               checkable=True)
        self.act_wf_labels.setChecked(self._wf_labels)
        self.act_wf_labels.toggled.connect(self._on_wf_labels_toggle)
        self.act_wf_scan = QtWidgets.QAction("Waterfall during &scan", self,
                                             checkable=True)
        self.act_wf_scan.setChecked(self._wf_during_scan)
        self.act_wf_scan.toggled.connect(self._on_wf_scan_toggle)
        view.addAction(self.act_wf_labels)
        view.addAction(self.act_wf_scan)
        # what the waterfall labels show: saved-memory names, modules' annotations (api.Annotation), or both
        srcmenu = view.addMenu("Label &source")
        self._label_src_group = QtWidgets.QActionGroup(self)
        self._label_src_group.setExclusive(True)
        self._label_src_actions = {}
        for key, text in (("memories", "&Memories"), ("annotations", "&Annotations (modules)"),
                          ("both", "&Both")):
            a = QtWidgets.QAction(text, self, checkable=True)
            a.setChecked(key == self._label_source)
            a.triggered.connect(lambda _c, k=key: self._set_label_source(k))
            self._label_src_group.addAction(a)
            self._label_src_actions[key] = a
            srcmenu.addAction(a)
        view.addAction("&FCC license search…", self._show_uls_window)
        view.addSeparator()
        view.addAction("&Aircraft (ADS-B)…", self._show_aircraft_window)
        self.act_map = QtWidgets.QAction("Live &map…", self)
        self.act_map.triggered.connect(self._show_map_window)
        if not WEBENGINE_AVAILABLE:
            self.act_map.setEnabled(False)
            self.act_map.setToolTip(
                "Install PyQtWebEngine to enable the live map "
                "(pip install PyQtWebEngine)")
        view.addAction(self.act_map)

        # --- Scan: start/stop, resume, and the settings dialog ---------------
        scan = mb.addMenu("&Scan")
        self.act_scan = QtWidgets.QAction("Start / stop &scan", self)
        self.act_scan.triggered.connect(self._toggle_scan)
        scan.addAction(self.act_scan)
        self.act_resume = QtWidgets.QAction("&Resume (skip held)", self)
        self.act_resume.triggered.connect(self._resume_scan)
        scan.addAction(self.act_resume)
        scan.addSeparator()
        scan.addAction("Scan se&ttings…", self._open_scan_settings)

        # --- Settings: SDR device, RF gains, reset ---------------------------
        settings = mb.addMenu("Se&ttings")
        sdrmenu = settings.addMenu("SDR &device")
        self._sdr_dev_group = QtWidgets.QActionGroup(self)
        self._sdr_dev_group.setExclusive(True)
        self._sdr_dev_actions = {}
        self._sdr_menu = sdrmenu
        self._fill_sdr_menu()
        settings.addAction("RF &gains…", self._open_rf_gains)
        settings.addAction("Find SDR &devices", self._rescan_devices)
        settings.addAction("&Transmit…", self._open_tx_settings)
        settings.addAction("&Label style…", self._open_label_style)
        settings.addAction("IQ s&napshot…", self._open_snapshot_settings)
        settings.addAction("Recording t&rigger…", self._open_trigger_settings)
        self.act_show_recorded = QtWidgets.QAction("Show &recorded frequencies", self, checkable=True)
        self.act_show_recorded.setToolTip(
            "Mark channels on the waterfall that already have recordings (recordings/ folders and "
            "the real-data store of labeled examples) with a ▼. Hover one for what was recorded there.")
        self.act_show_recorded.setChecked(self._show_recorded)
        self.act_show_recorded.toggled.connect(self._on_show_recorded_toggle)
        settings.addAction(self.act_show_recorded)
        settings.addSeparator()
        settings.addAction("Station &location…", self._open_station_settings)
        settings.addAction("RadioReference &account…", self._open_rr_account)
        settings.addAction("&Modules…", self._open_modules_dialog)
        settings.addAction("Reset all settings…", self._reset_settings)
        self._menus = {"View": view, "Scan": scan, "Settings": settings}
        return mb

    def _on_radio_page(self):
        return self.stack.currentIndex() == 0

    def _sync_view_actions(self):
        """Reflect the current stack index in the View-menu radio actions
        without re-triggering the switch."""
        for act, idx in ((self.act_view_radio, 0), (self.act_view_mem, 1)):
            act.blockSignals(True)
            act.setChecked(self.stack.currentIndex() == idx)
            act.blockSignals(False)

    def _show_radio(self):
        self.stack.setCurrentIndex(0)

    def _show_memories(self):
        self.stack.setCurrentIndex(1)

    def _toggle_view(self):
        self.stack.setCurrentIndex(0 if self.stack.currentIndex() == 1 else 1)

    def _on_view_changed(self, _idx):
        self._sync_view_actions()
        self._apply_waterfall_state()     # pause the waterfall while on Memories

    # =====================================================================
    #  Full-window pages
    # =====================================================================
    def _build_radio_page(self):
        """The default view: tuning + audio + SDR strip, the maximized waterfall,
        and the digital decode strip. No channel table (that lives in Memories)."""
        w = QtWidgets.QWidget()
        root = QtWidgets.QVBoxLayout(w)
        root.addLayout(self._build_top_row())     # display + Tune/REC/Scan/Mode/SIG
        root.addLayout(self._build_audio_row())   # Volume + Squelch
        root.addLayout(self._build_repeater_row())  # Offset split + CTCSS
        root.addLayout(self._build_sdr_strip())   # SDR selector + Start/Stop
        root.addWidget(self._build_playback_bar())  # IQ-file replay position + seek
        self.spectrum = SpectrumWidget(self.radio)
        self.spectrum.tuned.connect(self._on_spectrum_click)
        self.spectrum.bandwidth_changed.connect(self._on_bandwidth_drag)
        self.spectrum.selection_made.connect(self._on_spectrum_selection)
        self.spectrum.context_requested.connect(self._on_spectrum_context)
        self.spectrum.show_markers = self._wf_labels
        self.spectrum.set_label_source(self._label_source)
        self.spectrum.show_recorded = self._show_recorded
        self.spectrum.set_label_style(self._wf_label_px, self._wf_label_color,
                                      self._wf_tick_color)
        root.addLayout(self._build_waterfall_header())   # label + Snap + Memories btn
        root.addWidget(self.spectrum, 1)                 # stretch: gets the space
        root.addWidget(self._build_digital_group())      # decode strip
        return w

    def _build_sdr_strip(self):
        """SDR front-end selector + Start/Stop + status — kept on the Radio page
        so you can bring the radio up without opening a menu. (Gains move to the
        Settings ▸ RF gains dialog.)"""
        sdr = QtWidgets.QHBoxLayout()
        sdr.addWidget(QtWidgets.QLabel("SDR:"))
        self.sdr_combo = QtWidgets.QComboBox()
        self.sdr_combo.setToolTip("Which SDR to use: every device attached (any the installed drivers "
                                  "support: HackRF, RTL-SDR, LimeSDR, Airspy, USRP, ... through "
                                  "gr-osmosdr / SoapySDR), plus IQ file replay. Switching stops the "
                                  "current device and starts the selected one. Settings ▸ Find SDR "
                                  "devices looks again.")
        self._fill_sdr_combo()
        self.sdr_combo.currentIndexChanged.connect(
            lambda _i: self._on_sdr_change(self.sdr_combo.currentData()))
        sdr.addWidget(self.sdr_combo)
        sdr.addWidget(QtWidgets.QLabel("BW:"))
        self.bw_combo = QtWidgets.QComboBox()
        self.bw_combo.setToolTip("Capture bandwidth (RF window width): the rates this device "
                                 "takes. Changing it stops and restarts the SDR.")
        self._refresh_bw_combo()
        self.bw_combo.currentIndexChanged.connect(self._on_samp_rate_change)
        sdr.addWidget(self.bw_combo)
        self.sdr_btn = QtWidgets.QPushButton("Start")
        self.sdr_btn.clicked.connect(self._toggle_radio)
        sdr.addWidget(self.sdr_btn)
        self.ptt_btn = QtWidgets.QPushButton("🎙 PTT")
        self.ptt_btn.setToolTip(self._PTT_TOOLTIP)
        self.ptt_btn.clicked.connect(self._toggle_ptt)
        sdr.addWidget(self.ptt_btn)
        sdr.addWidget(QtWidgets.QLabel("TX:"))
        self.tx_mode_combo = QtWidgets.QComboBox()
        self.tx_mode_combo.addItem("Analog")          # modules add their transmit modes
        self.tx_mode_combo.setToolTip(
            "What PTT transmits: 'Analog' follows the current RX mode "
            "(NFM/AM/WFM/USB/LSB); a module's transmit mode encodes the same audio "
            "source its own way, regardless of the RX mode picker — see "
            "Settings ▸ Transmit for gains and that mode's settings.")
        self.tx_mode_combo.currentTextChanged.connect(self._on_tx_digital_mode_change)
        sdr.addWidget(self.tx_mode_combo)
        # a module TX mode's quick-edit widget (api.TxModeSpec.quick_widget) lives here, next to the
        # TX: picker; only the selected mode's is shown (see _register_tx_mode / _show_tx_quick_widget)
        self._tx_quick_box = QtWidgets.QWidget()
        self._tx_quick_lay = QtWidgets.QHBoxLayout(self._tx_quick_box)
        self._tx_quick_lay.setContentsMargins(0, 0, 0, 0)
        self._tx_quick_widgets = {}        # TX mode name -> its quick widget
        sdr.addWidget(self._tx_quick_box)
        self.loop_cb = QtWidgets.QCheckBox("Loop")
        self.loop_cb.setToolTip("Loop the IQ file replay (repeat when it reaches "
                                "the end). Only applies to the 'IQ file' device.")
        self.loop_cb.toggled.connect(self._on_loop_toggle)
        sdr.addWidget(self.loop_cb)
        # pick a different capture without leaving the 'IQ file' device (the
        # combo only prompts on the switch INTO it); shown only for that device
        self.iq_open_btn = QtWidgets.QPushButton("Open…")
        self.iq_open_btn.setToolTip("Replay a different IQ capture (.cs16 + its "
                                    ".json sidecar). Stops the current replay.")
        self.iq_open_btn.clicked.connect(self._open_iq_file)
        self.iq_open_btn.setVisible(self._sdr_device == IQ_FILE_DEVICE)
        sdr.addWidget(self.iq_open_btn)
        self.sdr_status = QtWidgets.QLabel("")
        self.sdr_status.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                      QtWidgets.QSizePolicy.Preferred)
        sdr.addWidget(self.sdr_status, 1)
        self._update_ptt_enabled()
        return sdr

    def _build_memories_page(self):
        """Bank / data-set management: pick a bank, import, filter/search, edit
        the merged channel table, and copy channels out. No waterfall/digital."""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)
        head = QtWidgets.QHBoxLayout()
        back = QtWidgets.QPushButton("◂ Radio")
        back.setToolTip("Back to the radio display (Ctrl+M).")
        back.clicked.connect(self._show_radio)
        head.addWidget(back)
        title = QtWidgets.QLabel("<b>Memories</b> — banks &amp; data sets")
        head.addWidget(title)
        head.addStretch(1)
        v.addLayout(head)
        v.addWidget(self._build_bank_group())
        v.addWidget(self._build_import_filter_group())
        v.addWidget(self._build_table())
        v.addLayout(self._build_memory_actions_row())
        v.addWidget(self._build_discover_group())
        v.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        return scroll

    def _open_scan_settings(self):
        if self._scan_dialog is None:
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Scan settings")
            lay = QtWidgets.QVBoxLayout(dlg)
            lay.addWidget(self._scan_ctl_widget)
            self._scan_dialog = dlg
        self._scan_dialog.show()
        self._scan_dialog.raise_()

    def _open_rf_gains(self):
        if self._rf_dialog is None:
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("RF gains")
            lay = QtWidgets.QVBoxLayout(dlg)
            lay.addWidget(self._rf_ctl_widget)
            self._rf_dialog = dlg
        self._rf_dialog.show()
        self._rf_dialog.raise_()

    def _open_tx_settings(self):
        if self._tx_dialog is None:
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Transmit")
            lay = QtWidgets.QVBoxLayout(dlg)
            lay.addWidget(self._tx_ctl_widget)
            self._tx_dialog = dlg
        self._tx_dialog.show()
        self._tx_dialog.raise_()

    def _open_label_style(self):
        """Adjust the waterfall channel-label and map target-label size/color.
        Changes apply live and persist with the rest of the settings."""
        if self._label_dialog is None:
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("Label style")
            form = QtWidgets.QFormLayout(dlg)

            def color_btn(get, setter):
                btn = QtWidgets.QPushButton()
                btn.setFixedWidth(72)

                def repaint():
                    btn.setStyleSheet(f"background:{get()}; border:1px solid #888;")
                    btn.setText(get())

                def pick():
                    c = QtWidgets.QColorDialog.getColor(
                        QtGui.QColor(get()), dlg, "Pick color")
                    if c.isValid():
                        setter(c.name())
                        repaint()

                btn.clicked.connect(pick)
                repaint()
                return btn

            def size_spin(value, slot):
                sp = QtWidgets.QSpinBox()
                sp.setRange(6, 32)
                sp.setValue(int(value))
                sp.setSuffix(" px")
                sp.valueChanged.connect(slot)
                return sp

            form.addRow("Waterfall label size",
                        size_spin(self._wf_label_px, self._set_wf_label_px))
            form.addRow("Waterfall text color",
                        color_btn(lambda: self._wf_label_color,
                                  self._set_wf_label_color))
            form.addRow("Waterfall tick color",
                        color_btn(lambda: self._wf_tick_color,
                                  self._set_wf_tick_color))
            form.addRow("Map label size",
                        size_spin(self._map_label_px, self._set_map_label_px))
            form.addRow("Map text color (light theme inverts)",
                        color_btn(lambda: self._map_label_color,
                                  self._set_map_label_color))

            btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
            btns.rejected.connect(dlg.close)
            btns.accepted.connect(dlg.close)
            form.addRow(btns)
            self._label_dialog = dlg
        self._label_dialog.show()
        self._label_dialog.raise_()








    # -- live label-style setters (also used by the dialog above) -------------
    def _set_wf_label_px(self, px):
        self._wf_label_px = int(px)
        self.spectrum.set_label_style(font_px=px)

    def _set_wf_label_color(self, c):
        self._wf_label_color = str(c)
        self.spectrum.set_label_style(text_color=c)

    def _set_wf_tick_color(self, c):
        self._wf_tick_color = str(c)
        self.spectrum.set_label_style(tick_color=c)

    def _set_map_label_px(self, px):
        self._map_label_px = int(px)
        self._push_map_label_style()

    def _set_map_label_color(self, c):
        self._map_label_color = str(c)
        self._push_map_label_style()

    def _push_map_label_style(self):
        """Send the current map-label size/color to the live Leaflet page (a
        no-op until the map view exists and has loaded)."""
        if getattr(self, "_map_view", None) is None:
            return
        color = json.dumps(self._map_label_color)
        self._map_view.page().runJavaScript(
            f"if(window.setLabelStyle)"
            f"window.setLabelStyle({int(self._map_label_px)},{color});")

    def _build_top_row(self):
        row = QtWidgets.QVBoxLayout()
        # stacked display: the directly-tunable frequency readout (page 0), or a
        # two-line SCANNING / ACTIVE / PRIORITY banner while scanning (page 1)
        self.freq_disp = TuningDisplay(self.radio.freq_hz)
        self.freq_disp.tuned.connect(self._on_display_tuned)
        self.freq_disp.nudged.connect(self._on_display_nudged)
        self.scan_banner = QtWidgets.QLabel("◦ SCANNING …")
        self.scan_banner.setAlignment(QtCore.Qt.AlignCenter)
        self.scan_banner.setObjectName("scanBanner")
        self.lcd_stack = QtWidgets.QStackedWidget()
        self.lcd_stack.setFixedHeight(80)
        self.lcd_stack.addWidget(self.freq_disp)    # index 0
        self.lcd_stack.addWidget(self.scan_banner)  # index 1
        row.addWidget(self.lcd_stack)

        # one action row: Tune | REC | Scan | Resume | Mode | ... | SIG
        ctl = QtWidgets.QHBoxLayout()
        # VFO A / B: two independent freq+mode memories. Flip between them; each
        # remembers its last tune (e.g. scan a bank on A, tune around on B).
        ctl.addWidget(QtWidgets.QLabel("VFO"))
        self.vfo_a_btn = QtWidgets.QPushButton("A")
        self.vfo_b_btn = QtWidgets.QPushButton("B")
        for b, w in ((self.vfo_a_btn, "A"), (self.vfo_b_btn, "B")):
            b.setCheckable(True)
            b.setFixedWidth(30)
            b.setToolTip(f"VFO {w}: its own frequency + mode, remembered when you "
                         f"switch away and restored when you switch back.")
        self._vfo_group = QtWidgets.QButtonGroup(self)
        self._vfo_group.setExclusive(True)
        self._vfo_group.addButton(self.vfo_a_btn)
        self._vfo_group.addButton(self.vfo_b_btn)
        self.vfo_a_btn.setChecked(self._active_vfo == "A")
        self.vfo_b_btn.setChecked(self._active_vfo == "B")
        self.vfo_a_btn.clicked.connect(lambda: self._set_active_vfo("A"))
        self.vfo_b_btn.clicked.connect(lambda: self._set_active_vfo("B"))
        ctl.addWidget(self.vfo_a_btn)
        ctl.addWidget(self.vfo_b_btn)
        ctl.addSpacing(8)

        self.tune_btn = QtWidgets.QPushButton("Tune")
        self.tune_btn.setToolTip("Blank the display and type a frequency in MHz "
                                 "on the number keys (Enter to tune).")
        self.tune_btn.clicked.connect(self.freq_disp.start_entry)
        ctl.addWidget(self.tune_btn)

        self.save_mem_btn = QtWidgets.QPushButton("★ Save")
        self.save_mem_btn.setToolTip("Save the tuned frequency to a memory bank "
                                     "(existing or new) with a name, service, "
                                     "tone and notes.")
        self.save_mem_btn.clicked.connect(self._save_to_memory)
        ctl.addWidget(self.save_mem_btn)

        self.rec_btn = QtWidgets.QPushButton("● REC")
        self.rec_btn.setObjectName("recBtn")
        self.rec_btn.setToolTip("Save raw IQ to a .cs16 file for offline spectrum "
                                "analysis / offline replay. Click again to stop.")
        self.rec_btn.clicked.connect(self._toggle_record)
        ctl.addWidget(self.rec_btn)
        self.rec_scope_combo = QtWidgets.QComboBox()
        self.rec_scope_combo.addItem("Full band", "band")     # 2.4 MHz
        self.rec_scope_combo.addItem("Demod window", "demod")  # ~240 kHz channel
        self.rec_scope_combo.setToolTip(
            "What to record:\n"
            "• Full band — the whole 2.4 MHz capture (large file, replayable)\n"
            "• Demod window — just the tuned channel (~240 kHz, small file)")
        ctl.addWidget(self.rec_scope_combo)
        self.snap_btn = QtWidgets.QPushButton("◷ SNAP")
        self.snap_btn.setToolTip(
            "Save the last few seconds of the whole band (what the waterfall just\n"
            "showed) to a .cs16 file, plus 2 s after the click — for a transmission\n"
            "you couldn't tune to in time. Open it with Source ▸ IQ file.\n"
            "Length: Settings ▸ IQ snapshot…")
        self.snap_btn.clicked.connect(lambda: self._take_snapshot())
        ctl.addWidget(self.snap_btn)
        self.rec_status = QtWidgets.QLabel("")
        self.rec_status.setObjectName("recStatus")
        ctl.addWidget(self.rec_status)

        self.scan_bank_combo = CheckComboBox("(active bank)")
        self.scan_bank_combo.setToolTip("Which bank(s) to scan. Tick one or more; "
                                        "leave all unticked to scan the active bank.")
        self.scan_bank_combo.setMinimumWidth(150)
        self.scan_bank_combo.setMaximumWidth(220)
        self.scan_bank_combo.changed.connect(self._on_scan_banks_change)
        ctl.addWidget(QtWidgets.QLabel("Banks:"))
        ctl.addWidget(self.scan_bank_combo)
        self.scan_btn = QtWidgets.QPushButton("▶ Scan")
        self.scan_btn.clicked.connect(self._toggle_scan)
        ctl.addWidget(self.scan_btn)
        self.resume_btn = QtWidgets.QPushButton("Resume")
        self.resume_btn.setEnabled(False)
        self.resume_btn.setToolTip("Skip the held channel and resume scanning.")
        self.resume_btn.clicked.connect(self._resume_scan)
        ctl.addWidget(self.resume_btn)

        ctl.addWidget(QtWidgets.QLabel("Mode:"))
        self.mode_combo = QtWidgets.QComboBox()
        self.mode_combo.addItems(ANALOG_MODES + ["ADS-B"])     # + modules' modes (_register_mode)
        self.mode_combo.setCurrentText(self.radio.mode.name)
        self.mode_combo.currentTextChanged.connect(self._on_mode_change)
        ctl.addWidget(self.mode_combo)

        ctl.addStretch(1)
        self.sig_label = QtWidgets.QLabel("SIG  —")
        self.sig_label.setToolTip("Signal / noise-floor level and squelch state "
                                  "at the tuned channel. 'Tone' is a blind CTCSS "
                                  "decode (NFM only) — whatever sub-audible tone "
                                  "is actually present, independent of the "
                                  "RX-Sq tone you've selected. Doesn't decode "
                                  "DCS/DPL (a different digital scheme).")
        self.sig_label.setFixedWidth(260)
        self.sig_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        ctl.addWidget(self.sig_label)
        row.addLayout(ctl)
        return row

    def _build_audio_row(self):
        # Volume + squelch stay on the main window (RF gains move to a tab).
        # Compact horizontal sliders instead of dials to save vertical space.
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._make_slider("Volume", 0, 100, 1, self._init_volume,
                                        " %", self._set_volume), 1)
        row.addWidget(self._build_squelch_control(), 1)
        return row

    def _build_repeater_row(self):
        """Repeater TX split + CTCSS. Offset shifts where PTT transmits (RX
        stays on the tuned/output frequency); CTCSS can be encoded on TX (to
        open a repeater) and/or decoded on RX (tone squelch — ignore other
        traffic on the same frequency). NFM only, same as TX itself."""
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Offset:"))
        self.offset_dir_combo = QtWidgets.QComboBox()
        self.offset_dir_combo.addItems(["Simplex", "−", "+"])
        self.offset_dir_combo.setCurrentText(self._offset_dir)
        self.offset_dir_combo.setToolTip(
            "Repeater TX split direction. PTT transmits on the tuned "
            "frequency shifted by this amount; RX stays on the tuned "
            "(repeater output) frequency. Auto-filled from the US band "
            "convention when you tune (−5 MHz on 70 cm, ±0.6 on 2 m, …); "
            "override here and it holds until you tune to another band.")
        self.offset_dir_combo.currentTextChanged.connect(self._on_offset_dir_change)
        row.addWidget(self.offset_dir_combo)
        self.offset_spin = QtWidgets.QDoubleSpinBox()
        # up to 50 MHz: 900 MHz repeaters split 25 MHz, 800 MHz ones 45 MHz
        self.offset_spin.setRange(0.0, OFFSET_MAX_MHZ)
        self.offset_spin.setDecimals(4)
        self.offset_spin.setSingleStep(_offset_step(self._offset_mhz))
        self.offset_spin.setSuffix(" MHz")
        self.offset_spin.setValue(self._offset_mhz)
        self.offset_spin.setEnabled(self._offset_dir != "Simplex")
        self.offset_spin.valueChanged.connect(self._on_offset_spin_change)
        row.addWidget(self.offset_spin)

        row.addSpacing(12)
        row.addWidget(QtWidgets.QLabel("Tone:"))
        self.ctcss_combo = QtWidgets.QComboBox()
        for hz in CTCSS_TONES:
            self.ctcss_combo.addItem(f"{hz:.1f} Hz", hz)
        idx = self.ctcss_combo.findData(self._ctcss_hz)
        self.ctcss_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.ctcss_combo.setToolTip(
            "CTCSS tone — shared by TX encode and RX tone squelch (below).")
        self.ctcss_combo.currentIndexChanged.connect(self._on_ctcss_tone_change)
        row.addWidget(self.ctcss_combo)
        self.ctcss_tx_cb = QtWidgets.QCheckBox("TX")
        self.ctcss_tx_cb.setToolTip(
            "Encode the tone into transmitted audio (needed to open most "
            "repeaters).")
        self.ctcss_tx_cb.setChecked(self._ctcss_tx)
        self.ctcss_tx_cb.toggled.connect(self._on_ctcss_tx_toggle)
        row.addWidget(self.ctcss_tx_cb)
        self.ctcss_rx_cb = QtWidgets.QCheckBox("RX Sq")
        self.ctcss_rx_cb.setToolTip(
            "Tone squelch: only unmute RX audio when this tone is present "
            "(in addition to the power squelch above).")
        self.ctcss_rx_cb.setChecked(self._ctcss_rx)
        self.ctcss_rx_cb.toggled.connect(self._on_ctcss_rx_toggle)
        row.addWidget(self.ctcss_rx_cb)
        row.addStretch(1)
        self.radio.set_ctcss_freq(self._ctcss_hz)
        return row

    def _current_offset_hz(self):
        if self._offset_dir == "Simplex":
            return 0
        sign = 1 if self._offset_dir == "+" else -1
        return sign * round(self._offset_mhz * 1e6)

    def _apply_offset(self, offset_hz):
        """Apply a signed offset (Hz) — e.g. from a recalled channel or VFO, or the band auto-default —
        to app state and the UI controls. Treated as an explicit offset (a later same-band user tune
        won't auto-override it); _auto_offset_for_tune clears that flag for its own auto defaults."""
        self._offset_user_set = True
        offset_hz = int(offset_hz)
        self._offset_dir = ("Simplex" if offset_hz == 0
                            else ("+" if offset_hz > 0 else "−"))
        if offset_hz:
            self._offset_mhz = abs(offset_hz) / 1e6
        if not hasattr(self, "offset_dir_combo"):
            return
        self.offset_dir_combo.blockSignals(True)
        self.offset_dir_combo.setCurrentText(self._offset_dir)
        self.offset_dir_combo.blockSignals(False)
        self.offset_spin.blockSignals(True)
        self.offset_spin.setValue(self._offset_mhz)
        self.offset_spin.setSingleStep(_offset_step(self._offset_mhz))
        self.offset_spin.setEnabled(self._offset_dir != "Simplex")
        self.offset_spin.blockSignals(False)
        self._update_ptt_enabled()

    def _auto_offset_for_tune(self, hz):
        """On a user tune, fill the repeater TX offset from the US band convention
        (radio.standard_repeater_offset). Tracks the standard offset last auto-applied; when the
        convention's value for the new tune differs (a different band, or the 2 m sub-band sign flip),
        the auto default is applied again — unless the user has overridden it and the convention value
        hasn't changed since, in which case the override is left alone."""
        std = standard_repeater_offset(hz)
        if self._offset_user_set and std == self._offset_auto_std:
            return                       # user overrode and the convention hasn't changed: leave it
        self._offset_auto_std = std
        self._apply_offset(std)
        self._offset_user_set = False    # this value is the auto default, not a user choice

    def _on_offset_dir_change(self, text):
        self._offset_dir = text
        self.offset_spin.setEnabled(text != "Simplex")
        self._offset_user_set = True     # manual change: stop auto-filling for this band
        self._update_ptt_enabled()      # offset moves the TX frequency the guard checks

    def _on_offset_spin_change(self, v):
        self._offset_mhz = float(v)
        self.offset_spin.setSingleStep(_offset_step(v))
        self._offset_user_set = True
        self._update_ptt_enabled()

    def _apply_ctcss(self, hz, tx, rx):
        """Apply CTCSS state — e.g. from a recalled channel or VFO — to app
        state, the UI controls, and the live RX tone-squelch block."""
        self._ctcss_hz = float(hz) if hz in CTCSS_TONES else 100.0
        self._ctcss_tx = bool(tx)
        self._ctcss_rx = bool(rx)
        if hasattr(self, "ctcss_combo"):
            idx = self.ctcss_combo.findData(self._ctcss_hz)
            self.ctcss_combo.blockSignals(True)
            self.ctcss_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self.ctcss_combo.blockSignals(False)
            self.ctcss_tx_cb.blockSignals(True)
            self.ctcss_tx_cb.setChecked(self._ctcss_tx)
            self.ctcss_tx_cb.blockSignals(False)
            self.ctcss_rx_cb.blockSignals(True)
            self.ctcss_rx_cb.setChecked(self._ctcss_rx)
            self.ctcss_rx_cb.blockSignals(False)
        self.radio.set_ctcss_freq(self._ctcss_hz)

    def _on_ctcss_tone_change(self, _idx):
        hz = self.ctcss_combo.currentData()
        if hz is None:
            return
        self._ctcss_hz = float(hz)
        self.radio.set_ctcss_freq(self._ctcss_hz)


    def _tx_tones(self):
        """{TX mode: its tone text} for every module TX mode that has one (kept in a VFO)."""
        out = {}
        for name, (m, spec) in self._tx_specs.items():
            if spec.channel_tone is not None:
                t = self._call_module(m, "channel tone", spec.channel_tone)
                if t:
                    out[name] = str(t)
        return out

    def _apply_tx_tone(self, tone, only=None):
        """A recalled channel's / VFO's tone text, offered to the module TX modes (each takes what it
        understands)."""
        if not tone:
            return
        for name, (m, spec) in self._tx_specs.items():
            if spec.apply_tone is not None and only in (None, name):
                self._call_module(m, "apply tone", spec.apply_tone, tone)


    def _on_ctcss_tx_toggle(self, on):
        self._ctcss_tx = bool(on)

    def _on_ctcss_rx_toggle(self, on):
        self._ctcss_rx = bool(on)

    def _build_rf_controls(self):
        # RF/IF/BB gains — hosted in the Settings ▸ RF gains dialog. Lambdas look
        # up self.radio at call time (a bound method would pin the old radio after
        # a rebuild and keep the HackRF busy).
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        # one slider per gain stage the device reports (api.sources.Caps.gains), rebuilt on a device
        # change; then frequency correction
        self._gain_box = QtWidgets.QVBoxLayout()
        v.addLayout(self._gain_box)
        self._gain_note = QtWidgets.QLabel("")
        self._gain_note.setStyleSheet("color:#8b95a1;")
        self._gain_note.setWordWrap(True)
        v.addWidget(self._gain_note)
        ppm_row = QtWidgets.QHBoxLayout()
        ppm_row.addWidget(QtWidgets.QLabel("Frequency correction"))
        self.ppm_spin = QtWidgets.QDoubleSpinBox()
        self.ppm_spin.setRange(-100.0, 100.0)
        self.ppm_spin.setDecimals(2)
        self.ppm_spin.setSingleStep(0.1)
        self.ppm_spin.setSuffix(" ppm")
        self.ppm_spin.setToolTip("Correct the radio's crystal error, per device: tune to a signal whose "
                                 "frequency you know and adjust until the readout matches it.")
        self.ppm_spin.valueChanged.connect(lambda v: self.radio.set_freq_corr(v))
        ppm_row.addWidget(self.ppm_spin)
        ppm_row.addStretch(1)
        v.addLayout(ppm_row)
        v.addStretch(1)

        # start-fresh: wipe saved UI/radio settings (and optionally banks)
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.HLine)
        line.setStyleSheet("color:#243040;")
        v.addWidget(line)
        reset_row = QtWidgets.QHBoxLayout()
        self.reset_btn = QtWidgets.QPushButton("Reset all settings to defaults…")
        self.reset_btn.setToolTip("Clear saved settings (gains, squelch, filters, "
                                  "snap, search, window size) and restart fresh. "
                                  "Banks are kept unless you choose otherwise.")
        self.reset_btn.clicked.connect(self._reset_settings)
        reset_row.addWidget(self.reset_btn)
        reset_row.addStretch(1)
        v.addLayout(reset_row)
        return w

    def _rebuild_gain_controls(self):
        """The gain sliders for the current device's stages (Caps.gains), at its saved values."""
        if not hasattr(self, "_gain_box"):
            return
        while self._gain_box.count():
            item = self._gain_box.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        caps = self._device_caps()
        saved = dict(self.radio.dev_settings.get("gains") or {})
        if caps is None or not caps.gains:
            self._gain_note.setText("No gain controls for this source." if caps is not None
                                    else "An IQ file has no gain controls.")
        else:
            defaults = self.radio._default_gains(self.radio.devinfo) if self.radio.devinfo else {}
            for g in caps.gains:
                step = max(1, int(round(g.step))) if g.step >= 1 else 1
                init = saved.get(g.name, defaults.get(g.name, g.lo))
                self._gain_box.addWidget(self._make_slider(
                    g.name, int(g.lo), int(g.hi), step, int(init), " dB",
                    lambda val, n=g.name: self.radio.set_gain(n, val)))
            self._gain_note.setText(f"Gain stages of {self._device_label()}, as its driver names them; "
                                    "kept per device.")
        self.ppm_spin.blockSignals(True)
        self.ppm_spin.setValue(float(self.radio.dev_settings.get("ppm") or 0.0))
        self.ppm_spin.setEnabled(bool(caps and caps.ppm))
        self.ppm_spin.blockSignals(False)

    def _build_tx_controls(self):
        """PTT source, TX gains, and modules' transmit-mode settings — hosted in
        the Settings ▸ Transmit dialog. Which mode PTT actually uses (analog,
        following the RX mode, or a module's transmit mode) is picked by the TX: combo in the main window
        (_build_sdr_strip), not here — see radio.TX_MODES. HackRF only (see
        radio.TxChain). The PTT button itself also lives in the SDR strip so
        it's reachable without a menu."""
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)

        src_row = QtWidgets.QHBoxLayout()
        src_row.addWidget(QtWidgets.QLabel("Source:"))
        self.tx_mic_rb = QtWidgets.QRadioButton("Microphone")
        self.tx_file_rb = QtWidgets.QRadioButton("Audio file")
        (self.tx_mic_rb if self._tx_source == "mic" else self.tx_file_rb) \
            .setChecked(True)
        self.tx_mic_rb.toggled.connect(self._on_tx_source_change)
        src_row.addWidget(self.tx_mic_rb)
        src_row.addWidget(self.tx_file_rb)
        src_row.addStretch(1)
        v.addLayout(src_row)

        file_row = QtWidgets.QHBoxLayout()
        self.tx_file_edit = QtWidgets.QLineEdit(self._tx_file or "")
        self.tx_file_edit.setReadOnly(True)
        self.tx_file_edit.setPlaceholderText("no file selected")
        file_row.addWidget(self.tx_file_edit, 1)
        self.tx_file_btn = QtWidgets.QPushButton("Choose file…")
        self.tx_file_btn.clicked.connect(self._choose_tx_file)
        file_row.addWidget(self.tx_file_btn)
        self.tx_loop_cb = QtWidgets.QCheckBox("Loop")
        self.tx_loop_cb.setToolTip("Loop the file (repeat when it reaches the "
                                   "end) instead of auto-releasing PTT at EOF.")
        self.tx_loop_cb.setChecked(self._tx_loop)
        self.tx_loop_cb.toggled.connect(self._on_tx_loop_toggle)
        file_row.addWidget(self.tx_loop_cb)
        v.addLayout(file_row)

        # modules' TX modes put their fields here (api.TxModeSpec.settings_widget)
        self._tx_ext_box = QtWidgets.QWidget()
        QtWidgets.QVBoxLayout(self._tx_ext_box).setContentsMargins(0, 0, 0, 0)
        v.addWidget(self._tx_ext_box)

        v.addWidget(self._make_slider("RF Amp", 0, 14, 14, self._tx_rf_gain,
                                      " dB", self._set_tx_rf_gain))
        v.addWidget(self._make_slider("VGA", 0, 47, 1, self._tx_if_gain,
                                      " dB", self._set_tx_if_gain))
        # Safety override: by default PTT refuses to key outside the US amateur
        # bands (radio.US_HAM_BANDS). A licensed operator can lift that here.
        self.tx_ham_override_cb = QtWidgets.QCheckBox(
            "Allow transmit outside the US amateur bands — I am licensed and "
            "accept responsibility")
        self.tx_ham_override_cb.setChecked(self._tx_ham_override)
        self.tx_ham_override_cb.setToolTip(
            "Off (default): PTT only keys inside a US amateur band. On: PTT "
            "will transmit on any tuned frequency — only legal with the right "
            "license and authorization. You are responsible for what you key.")
        self.tx_ham_override_cb.toggled.connect(self._on_tx_ham_override_toggle)
        v.addWidget(self.tx_ham_override_cb)
        lbl = QtWidgets.QLabel(
            "Transmit follows the current RX mode (NFM, AM, WFM, USB, or "
            "LSB) unless the TX: combo in the main window selects a module's "
            "transmit mode. PTT stops the receiver, keys "
            "the transmitter on the currently tuned frequency, and restores "
            "the receiver when released.")
        lbl.setWordWrap(True)
        lbl.setStyleSheet("color:#8b95a1;")
        v.addWidget(lbl)
        v.addStretch(1)
        self._update_tx_controls_enabled()
        return w

    def _choose_tx_file(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Choose audio file for TX", "", "WAV audio (*.wav);;All files (*)")
        if not path:
            return
        self._tx_file = path
        self.tx_file_edit.setText(path)

    def _on_tx_source_change(self, _checked=None):
        self._tx_source = "mic" if self.tx_mic_rb.isChecked() else "file"
        self._update_tx_controls_enabled()

    def _update_tx_controls_enabled(self):
        is_file = self._tx_source == "file"
        if hasattr(self, "tx_file_btn"):
            self.tx_file_btn.setEnabled(is_file)
            self.tx_loop_cb.setEnabled(is_file)

    def _on_tx_loop_toggle(self, on):
        self._tx_loop = bool(on)

    def _on_tx_ham_override_toggle(self, on):
        self._tx_ham_override = bool(on)
        self._update_ptt_enabled()

    def _set_tx_rf_gain(self, v):
        self._tx_rf_gain = int(v)

    def _set_tx_if_gain(self, v):
        self._tx_if_gain = int(v)




    def _build_waterfall_header(self):
        row = QtWidgets.QHBoxLayout()
        lbl = QtWidgets.QLabel("Spectrum + waterfall  (scroll to zoom · click to "
                               "place the demod marker):")
        lbl.setWordWrap(True)
        row.addWidget(lbl, 1)
        # Snap-to-grid on a waterfall click (Labels / Show-during-scan moved to the
        # View menu).
        self.snap_cb = QtWidgets.QCheckBox("Snap")
        self.snap_cb.setToolTip("Snap a clicked signal to the channel grid so the "
                                "tuned freq lands on channel center.")
        self.snap_cb.setChecked(self._snap_enabled)
        self.snap_cb.toggled.connect(self._on_snap_toggle)
        row.addWidget(self.snap_cb)
        self.snap_combo = QtWidgets.QComboBox()
        self.snap_combo.addItems(["0.1", "1", "5", "6.25", "7.5", "12.5", "25"])
        self.snap_combo.setCurrentText(_fmt_khz(self._snap_khz))
        self.snap_combo.setToolTip("Channel spacing (kHz)")
        self.snap_combo.currentTextChanged.connect(self._on_snap_khz_change)
        row.addWidget(self.snap_combo)
        row.addWidget(QtWidgets.QLabel("kHz"))
        mem_btn = QtWidgets.QPushButton("Memories ▸")
        mem_btn.setToolTip("Open bank & data-set management (Ctrl+M).")
        mem_btn.clicked.connect(self._show_memories)
        row.addSpacing(12)
        row.addWidget(mem_btn)
        return row

    def _build_squelch_control(self):
        """Squelch as a compact horizontal slider with an Auto checkbox. Manual:
        absolute dB (-100..0). Auto: 'floor + margin' dB (0..30), adapting to
        each band's noise floor."""
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(6)
        self._sq_manual = int(self._init_squelch)
        self._sq_margin = int(self._init_margin)
        title = QtWidgets.QLabel("Squelch")
        title.setMinimumWidth(58)
        self.sq_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.sq_slider.setMinimumWidth(110)
        self.sq_label = QtWidgets.QLabel()
        self.sq_label.setMinimumWidth(62)
        self.sq_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.autosq_cb = QtWidgets.QCheckBox("Auto")
        self.autosq_cb.setToolTip("Auto-squelch: open when the signal is this many "
                                  "dB above the channel's measured noise floor "
                                  "(adapts per band).")

        def apply_dial(val):
            if self.autosq_cb.isChecked():
                self._sq_margin = val
                self.radio.set_auto_margin(val)
                self.sq_label.setText(f"floor+{val}")
            else:
                self._sq_manual = val
                self.radio.set_squelch(val)
                self.sq_label.setText(f"{val} dB")

        def on_auto(on):
            self.radio.set_auto_squelch(on)
            self.sq_slider.blockSignals(True)
            if on:
                self.sq_slider.setRange(0, 30)
                self.sq_slider.setValue(self._sq_margin)
            else:
                self.sq_slider.setRange(-100, 0)
                self.sq_slider.setValue(self._sq_manual)
            self.sq_slider.blockSignals(False)
            apply_dial(self.sq_slider.value())

        self.sq_slider.valueChanged.connect(apply_dial)
        self.autosq_cb.toggled.connect(on_auto)
        # initialize in whichever mode was saved
        self.radio.set_auto_margin(self._sq_margin)
        self.radio.set_squelch(self._sq_manual)
        self.autosq_cb.setChecked(bool(self._init_autosq))
        on_auto(self.autosq_cb.isChecked())     # sets range/value/label + radio
        h.addWidget(title)
        h.addWidget(self.sq_slider, 1)
        h.addWidget(self.sq_label)
        h.addWidget(self.autosq_cb)
        return w

    def _make_slider(self, title, lo, hi, step, init, unit, on_change):
        """Compact horizontal slider control: [title] [====slider====] [value].
        Snaps to ``step`` and reports the value. Much shorter than a dial."""
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(2, 2, 2, 2)
        h.setSpacing(6)
        lbl = QtWidgets.QLabel(title)
        lbl.setMinimumWidth(58)
        sld = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        sld.setRange(lo, hi)
        sld.setSingleStep(step)
        sld.setPageStep(step)
        sld.setValue(int(init))
        sld.setMinimumWidth(110)
        val = QtWidgets.QLabel(f"{init}{unit}")
        val.setMinimumWidth(52)
        val.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)

        def handle(v):
            snapped = round(v / step) * step
            if snapped != v:
                sld.blockSignals(True)
                sld.setValue(snapped)
                sld.blockSignals(False)
            val.setText(f"{snapped}{unit}")
            on_change(snapped)

        sld.valueChanged.connect(handle)
        h.addWidget(lbl)
        h.addWidget(sld, 1)
        h.addWidget(val)
        return w

    def _build_import_filter_group(self):
        # Import a data set into the ACTIVE bank, and filter/search the merged
        # channel table below. The bank selector lives in the Banks group above.
        box = QtWidgets.QGroupBox("Import / filter")
        v = QtWidgets.QVBoxLayout(box)

        # --- import row: load a FULL dataset into a bank --------------------
        imp = QtWidgets.QHBoxLayout()
        imp.addWidget(QtWidgets.QLabel("Import:"))
        self.source_combo = QtWidgets.QComboBox()   # filled from api.importers (_refresh_importers)
        self.source_combo.currentTextChanged.connect(self._on_source_change)
        imp.addWidget(self.source_combo)
        self.csv_btn = QtWidgets.QPushButton("Choose file…")
        self.csv_btn.clicked.connect(self._choose_source_path)
        imp.addWidget(self.csv_btn)
        self.dataset_combo = QtWidgets.QComboBox()
        self.dataset_combo.addItems(["LMcomm (77 MB)", "LMpriv (403 MB)",
                                     "LMbcast (7 MB)"])
        self.download_btn = QtWidgets.QPushButton("Download FCC data")
        self.download_btn.clicked.connect(self._toggle_download)
        self.dl_bar = QtWidgets.QProgressBar()
        self.dl_bar.setRange(0, 100)
        self.dl_bar.setFixedWidth(140)
        self.dl_bar.setVisible(False)
        self.uls_search_btn = QtWidgets.QPushButton("Search FCC data…")
        self.uls_search_btn.setToolTip("Search the downloaded FCC licenses by place, frequency, "
                                       "licensee or service, without importing them.")
        self.uls_search_btn.clicked.connect(self._show_uls_window)
        imp.addWidget(self.dataset_combo)
        imp.addWidget(self.download_btn)
        imp.addWidget(self.dl_bar)
        imp.addWidget(self.uls_search_btn)
        self._import_row = imp
        self._importer_widgets = {}          # provider name -> [its widgets], shown while it's picked
        register_core_importers(lambda _parent: [self.dataset_combo, self.download_btn, self.uls_search_btn])
        self.import_btn = QtWidgets.QPushButton("Import → bank")
        self.import_btn.setToolTip("Load the full dataset into a bank (CSV = all "
                                   "rows; FCC/RR use the Where box as the region).")
        self.import_btn.clicked.connect(self._import_source)
        imp.addWidget(self.import_btn)
        imp.addStretch(1)
        v.addLayout(imp)

        # --- filter row: live radius + mode filter over the SELECTED data set -
        filt = QtWidgets.QHBoxLayout()
        filt.addWidget(QtWidgets.QLabel("Filter — Mode:"))
        self.mode_filter = QtWidgets.QComboBox()
        self.mode_filter.addItems(["All"] + self._known_modes())
        self.mode_filter.setToolTip("Show only channels of this mode.")
        self.mode_filter.currentTextChanged.connect(lambda _t: self._apply_filter())
        filt.addWidget(self.mode_filter)
        filt.addWidget(QtWidgets.QLabel("Where:"))
        self.loc_edit = QtWidgets.QLineEdit()
        self.loc_edit.setPlaceholderText("zip · city, ST · lat,lon  (blank = no radius)")
        self._applied_loc_text = None            # last text we geocoded
        self.loc_edit.returnPressed.connect(self._on_where_apply)
        # also apply when focus leaves the field (e.g. you click the radius),
        # so you don't have to remember to press Enter / Apply
        self.loc_edit.editingFinished.connect(self._on_where_apply)
        apply_btn = QtWidgets.QPushButton("Apply")
        apply_btn.clicked.connect(self._on_where_apply)
        here_btn = QtWidgets.QToolButton()
        here_btn.setText("⌂")
        here_btn.setToolTip("Use the station location (Settings ▸ Station location…)")
        here_btn.clicked.connect(self._where_from_station)
        self.radius_spin = QtWidgets.QSpinBox()
        self.radius_spin.setRange(1, 250)
        self.radius_spin.setValue(25)
        self.radius_spin.setSuffix(" mi")
        self.radius_spin.valueChanged.connect(lambda _v: self._apply_filter())
        self.nogps_cb = QtWidgets.QCheckBox("incl. no-GPS")
        self.nogps_cb.setChecked(False)     # strict radius by default
        self.nogps_cb.setToolTip("When a radius is set, ALSO keep channels that "
                                 "have no coordinates (county-wide/regional, and "
                                 "campus/hospital systems). Off = strict radius.")
        self.nogps_cb.toggled.connect(lambda _v: self._apply_filter())
        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_filter)
        filt.addWidget(self.loc_edit, 1)
        filt.addWidget(here_btn)
        filt.addWidget(apply_btn)
        filt.addWidget(QtWidgets.QLabel("within"))
        filt.addWidget(self.radius_spin)
        filt.addWidget(self.nogps_cb)
        filt.addWidget(clear_btn)
        v.addLayout(filt)

        # --- services row (also part of the live filter) --------------------
        svc = QtWidgets.QHBoxLayout()
        svc.addWidget(QtWidgets.QLabel("Services:"))
        self.filter_cbs = {}
        for tag in ["Police", "Fire", "EMS", "Public Safety", "Business",
                    "Ham", "Aircraft", "Schools"]:
            cb = QtWidgets.QCheckBox(tag)
            cb.toggled.connect(self._on_service_cb)
            self.filter_cbs[tag] = cb
            svc.addWidget(cb)
        self.filter_all_cb = QtWidgets.QCheckBox("All")
        self.filter_all_cb.setChecked(True)
        self.filter_all_cb.toggled.connect(self._on_all_services)
        svc.addWidget(self.filter_all_cb)
        svc.addStretch(1)
        v.addLayout(svc)

        # --- text search over the active bank --------------------------------
        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(QtWidgets.QLabel("Search:"))
        self.src_search = QtWidgets.QLineEdit()
        self.src_search.setPlaceholderText(
            "town or text — narrows the table below "
            "(label · service · city · freq)")
        self.src_search.setClearButtonEnabled(True)
        self.src_search.blockSignals(True)      # table not built yet; restore later
        self.src_search.setText(self._src_search)
        self.src_search.blockSignals(False)
        self.src_search.textChanged.connect(self._on_src_search_change)
        srow.addWidget(self.src_search, 1)
        v.addLayout(srow)

        self._csv_path = ""
        self._refresh_importers()
        return box

    def _refresh_importers(self):
        """The Import picker from api.importers (the core's and every module's, in registration order)."""
        cur = self.source_combo.currentText()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for p in importers.providers():
            self.source_combo.addItem(p.name)
            ok, why = p.check()
            tip = p.tooltip + (("\n" if p.tooltip else "") + f"Not ready: {why}" if not ok else "")
            if tip:
                self.source_combo.setItemData(self.source_combo.count() - 1, tip, QtCore.Qt.ToolTipRole)
            if p.widgets is not None and p.name not in self._importer_widgets:
                ws = self._call_import_widgets(p) or []
                for w in ws:
                    if self._import_row.indexOf(w) < 0:
                        self._import_row.insertWidget(self._import_row.indexOf(self.import_btn), w)
                self._importer_widgets[p.name] = list(ws)
        if cur and self.source_combo.findText(cur) >= 0:
            self.source_combo.setCurrentText(cur)
        self.source_combo.blockSignals(False)
        self._on_source_change(self.source_combo.currentText())

    def _call_import_widgets(self, p):
        try:
            return list(p.widgets(self))
        except Exception:
            import traceback
            print(f"[sdrexplorer] import provider {p.name}: widgets failed:", file=sys.stderr)
            traceback.print_exc()
            return []

    def _build_memory_actions_row(self):
        # status + copy-out actions, shown below the merged channel table.
        stat = QtWidgets.QHBoxLayout()
        self.fetch_status = QtWidgets.QLabel("")
        self.fetch_status.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                        QtWidgets.QSizePolicy.Preferred)
        self.add_checked_btn = QtWidgets.QPushButton("Add checked → bank…")
        self.add_checked_btn.setToolTip("Copy the checked (☑) channels shown below "
                                        "into a new or existing bank — e.g. a "
                                        "small 'Test' bank of known-busy channels.")
        self.add_checked_btn.clicked.connect(self._add_checked_to_bank)
        self.export_btn = QtWidgets.QPushButton("Export all shown → bank")
        self.export_btn.setToolTip("Save every channel currently shown (after the "
                                   "filter/search) into a new or existing bank.")
        self.export_btn.clicked.connect(self._export_filtered)
        stat.addWidget(self.fetch_status, 1)
        stat.addWidget(self.add_checked_btn)
        stat.addWidget(self.export_btn)
        return stat

    def _build_bank_group(self):
        box = QtWidgets.QGroupBox("Banks")
        h = QtWidgets.QHBoxLayout(box)
        self.bank_combo = QtWidgets.QComboBox()
        self.bank_combo.currentIndexChanged.connect(self._on_bank_change)
        new_btn = QtWidgets.QPushButton("New")
        new_btn.clicked.connect(self._new_bank)
        del_btn = QtWidgets.QPushButton("Delete")
        del_btn.clicked.connect(self._delete_bank)

        add_btn = QtWidgets.QPushButton("Add current → bank")
        add_btn.setToolTip("Save the currently tuned freq+mode into this bank.")
        add_btn.clicked.connect(self._add_current)
        self.remove_btn = QtWidgets.QPushButton("Remove sel.")
        self.remove_btn.setToolTip("Remove the selected channel row(s) from this "
                                   "bank (also: Delete key, or right-click).")
        self.remove_btn.clicked.connect(self._remove_selected_scan)
        save_btn = QtWidgets.QPushButton("Save memory")
        save_btn.clicked.connect(self._save)

        h.addWidget(QtWidgets.QLabel("Active:"))
        h.addWidget(self.bank_combo, 1)
        h.addWidget(new_btn)
        h.addWidget(del_btn)
        h.addSpacing(16)
        h.addWidget(add_btn)
        h.addWidget(self.remove_btn)
        h.addWidget(save_btn)
        return box

    def _make_channel_table(self):
        """A channel table (freq/mode/label/service) whose first-column header is
        a check-all/uncheck-all box. Returns (table, header). Used by the merged
        Memories table (checkbox = include-in-scan)."""
        t = QtWidgets.QTableWidget(0, 5)
        hdr = CheckableHeaderView(t)
        hdr.setStretchLastSection(True)
        t.setHorizontalHeader(hdr)
        t.setHorizontalHeaderLabels(["", "Freq (MHz)", "Mode", "Label", "Service"])
        t.setColumnWidth(0, 30)
        t.setColumnWidth(1, 110)
        t.setColumnWidth(2, 60)
        t.setColumnWidth(3, 220)
        t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        t.setMinimumHeight(140)
        t.setMaximumHeight(240)               # bounded so the waterfall gets room
        return t, hdr

    def _fill_channel_table(self, table, pairs, checked_fn):
        """Populate ``table`` from ``pairs`` = [(bank_index, Channel), …]; the
        row-0 checkbox starts ticked when ``checked_fn(ch)`` is true."""
        table.setRowCount(len(pairs))
        for r, (_i, ch) in enumerate(pairs):
            cb = QtWidgets.QTableWidgetItem()
            cb.setFlags(QtCore.Qt.ItemIsUserCheckable | QtCore.Qt.ItemIsEnabled)
            cb.setCheckState(QtCore.Qt.Checked if checked_fn(ch)
                             else QtCore.Qt.Unchecked)
            table.setItem(r, 0, cb)
            table.setItem(r, 1, QtWidgets.QTableWidgetItem(_fmt_mhz(ch.freq)))
            table.setItem(r, 2, QtWidgets.QTableWidgetItem(ch.mode))
            table.setItem(r, 3, QtWidgets.QTableWidgetItem(ch.label))
            table.setItem(r, 4, QtWidgets.QTableWidgetItem(ch.service))

    def _build_table(self):
        self.table, self._table_header = self._make_channel_table()
        # This is the main content of the Memories view now (no waterfall
        # competing for space), so let it grow past the shared 240px cap.
        self.table.setMaximumHeight(16_777_215)
        self.table.setMinimumHeight(240)
        self._table_header.toggledAll.connect(self._on_check_all)
        # tune when the current row changes — via mouse OR the arrow keys
        self.table.currentCellChanged.connect(self._on_current_cell)
        # keep the header's check-all state in sync with manual row ticks
        self.table.itemChanged.connect(self._on_table_item_changed)
        self._wire_table_removal(self.table, self._remove_selected_scan,
                                  self._edit_selected_channel)
        return self.table

    def _wire_table_removal(self, table, remove_fn, edit_fn=None):
        """Enable Delete-key + right-click removal of the selected rows, and,
        if ``edit_fn`` is given, double-click / right-click editing of a
        single row."""
        table.setContextMenuPolicy(QtCore.Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(
            lambda pos, t=table, f=remove_fn, e=edit_fn:
                self._table_remove_menu(t, pos, f, e))
        sc = QtWidgets.QShortcut(QtGui.QKeySequence.Delete, table)
        sc.setContext(QtCore.Qt.WidgetShortcut)      # only when this table has focus
        sc.activated.connect(remove_fn)
        if edit_fn:
            table.cellDoubleClicked.connect(lambda r, c, e=edit_fn: e())

    def _table_remove_menu(self, table, pos, remove_fn, edit_fn=None):
        menu = QtWidgets.QMenu(self)
        if edit_fn:
            menu.addAction("Edit…", edit_fn)
        menu.addAction("Remove selected from bank", remove_fn)
        menu.exec_(table.viewport().mapToGlobal(pos))

    def _build_scan_controls(self):
        box = QtWidgets.QGroupBox("Scan (stop-on-active)")
        v = QtWidgets.QVBoxLayout(box)
        hint = QtWidgets.QLabel(
            "Scans the ticked channels in the active bank. The ▶ Scan button is on "
            "the Radio view. "
            "Stops when a channel breaks the SQUELCH level (adjust it live). "
            "Untick a row to skip it. Dwell = settle time per channel before "
            "measuring; too low reads the previous channel (leave ≥150 ms).")
        hint.setWordWrap(True)
        v.addWidget(hint)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Dwell ms:"))
        self.dwell_spin = QtWidgets.QSpinBox()
        self.dwell_spin.setRange(120, 1000)      # floor: pipeline flush after retune
        self.dwell_spin.setValue(200)
        self.dwell_spin.setToolTip("Settle time per channel before the power read. "
                                   "Below ~150 ms the read can still reflect the "
                                   "previous (adjacent) channel — watch the "
                                   "sig/floor in the scan banner and tune down "
                                   "only as far as stays reliable.")
        row.addWidget(self.dwell_spin)
        row.addWidget(QtWidgets.QLabel("Hang s:"))
        self.hang_spin = QtWidgets.QDoubleSpinBox()
        self.hang_spin.setRange(0.0, 30.0)
        self.hang_spin.setSingleStep(0.5)
        self.hang_spin.setValue(2.0)
        self.hang_spin.setToolTip("Auto-resume after the signal has been gone "
                                  "this long (0 = resume immediately).")
        row.addWidget(self.hang_spin)
        row.addStretch(1)
        v.addLayout(row)

        # priority: periodically poll the ★ Priority bank during a scan and
        # interrupt to a priority channel when it goes active
        prow = QtWidgets.QHBoxLayout()
        self.prio_cb = QtWidgets.QCheckBox("Priority scan")
        self.prio_cb.setChecked(self._prio_enabled)
        self.prio_cb.setToolTip("While scanning, check the ★ Priority bank every "
                                "few seconds and jump to a priority channel when "
                                "it's active (even while holding on another).")
        prow.addWidget(self.prio_cb)
        prow.addWidget(QtWidgets.QLabel("every"))
        self.prio_spin = QtWidgets.QDoubleSpinBox()
        self.prio_spin.setRange(0.5, 30.0)
        self.prio_spin.setSingleStep(0.5)
        self.prio_spin.setValue(self._prio_interval)
        self.prio_spin.setSuffix(" s")
        prow.addWidget(self.prio_spin)
        self.prio_add_btn = QtWidgets.QPushButton("Add selected → ★ Priority")
        self.prio_add_btn.setToolTip("Copy the selected channel(s) into the "
                                     "★ Priority bank.")
        self.prio_add_btn.clicked.connect(self._add_to_priority)
        prow.addWidget(self.prio_add_btn)
        prow.addStretch(1)
        v.addLayout(prow)

        self.scan_status = QtWidgets.QLabel("idle")
        self.scan_status.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                       QtWidgets.QSizePolicy.Preferred)
        v.addWidget(self.scan_status)      # own row; can't widen the window
        return box

    def _build_discover_group(self):
        box = QtWidgets.QGroupBox("Search (band sweep for unknown signals)")
        v = QtWidgets.QVBoxLayout(box)
        row = QtWidgets.QHBoxLayout()
        self.lo_edit = QtWidgets.QLineEdit("154.0")
        self.hi_edit = QtWidgets.QLineEdit("155.0")
        self.lo_edit.setFixedWidth(80)
        self.hi_edit.setFixedWidth(80)
        self.disc_btn = QtWidgets.QPushButton("Discover")
        self.disc_btn.clicked.connect(self._start_discover)
        self.disc_mode = QtWidgets.QComboBox()
        self.disc_mode.addItems(ANALOG_MODES)
        self.disc_mode.setCurrentText("NFM")
        save_sel = QtWidgets.QPushButton("Save selected → bank")
        save_sel.clicked.connect(self._save_discovered)
        self.disc_thresh = QtWidgets.QDoubleSpinBox()
        self.disc_thresh.setRange(1.0, 40.0)
        self.disc_thresh.setValue(6.0)
        self.disc_thresh.setToolTip("Peak prominence (dB) needed to report a signal.")
        row.addWidget(QtWidgets.QLabel("Lo MHz:"))
        row.addWidget(self.lo_edit)
        row.addWidget(QtWidgets.QLabel("Hi MHz:"))
        row.addWidget(self.hi_edit)
        row.addWidget(QtWidgets.QLabel("as"))
        row.addWidget(self.disc_mode)
        row.addWidget(QtWidgets.QLabel("prom dB:"))
        row.addWidget(self.disc_thresh)
        row.addWidget(self.disc_btn)
        row.addWidget(save_sel)
        self.disc_status = QtWidgets.QLabel("")
        self.disc_status.setSizePolicy(QtWidgets.QSizePolicy.Ignored,
                                       QtWidgets.QSizePolicy.Preferred)
        row.addWidget(self.disc_status, 1)
        v.addLayout(row)
        self.disc_list = QtWidgets.QListWidget()
        self.disc_list.setSelectionMode(
            QtWidgets.QAbstractItemView.ExtendedSelection)
        self.disc_list.setMaximumHeight(90)
        self.disc_list.itemDoubleClicked.connect(self._on_disc_activate)
        v.addWidget(self.disc_list)
        return box

    # =====================================================================
    #  Tuning / mode
    # =====================================================================
    def _match_channel(self, hz, prefer_mode=None, tol=500, ids=None):
        """Return (bank_name, Channel) for the stored channel best matching ``hz``
        (any bank), or None. When several channels SHARE the frequency (common
        for fireground/itinerant channels), disambiguate by the decoded identity:
        first a decoded id matching the channel's tone code (``ids``: {kind: value},
        kinds as banks.parse_tone gives them, any case), then a matching mode,
        else the first."""
        cands = [(bname, ch) for bname, bank in self.store.banks.items()
                 for ch in bank.channels if abs(ch.freq - hz) <= tol]
        if not cands:
            return None
        want = {str(k).upper(): v for k, v in (ids or {}).items() if v is not None}
        if want:
            for bname, ch in cands:
                t = parse_tone(getattr(ch, "tone", ""))
                if t and t["kind"] in want and t["value"] == want[t["kind"]]:
                    return (bname, ch)
        if prefer_mode:
            for bname, ch in cands:
                if ch.mode == prefer_mode:
                    return (bname, ch)
        return cands[0]

    def _channel_for_freq(self, hz, tol=500):
        """(label, mode, bank) for the channel matching ``hz``, or None. Captions
        the tuning display for known channels."""
        m = self._match_channel(hz, tol=tol)
        if m is None:
            return None
        bname, ch = m
        return (ch.label or "(unlabeled)", ch.mode, bname)

    def _show_freq(self):
        self.freq_disp.setFrequency(self.radio.freq_hz)
        hit = self._channel_for_freq(int(self.radio.freq_hz))
        bank_sub = f"{hit[0]} · {hit[1]} · {hit[2]}" if hit else ""
        # Live RDS station name (WFM only) takes priority over/combines with a
        # saved-channel match — it's the more current "what am I listening to".
        if self.radio.mode.name == "WFM" and self._rds_ps:
            sub = f"{self._rds_ps} · RDS"
            if bank_sub:
                sub += f" · {bank_sub}"
            self.freq_disp.setSubtitle(sub)
        else:
            self.freq_disp.setSubtitle(bank_sub)

    def _show_scanning(self, on):
        """Swap the main display between the tuning readout and the banner."""
        self.lcd_stack.setCurrentIndex(1 if on else 0)

    def _set_freq(self, hz, how="channel"):
        """Primary retune — recenters the band on ``hz`` (moves the waterfall). ``how`` goes out with the
        "tuned" topic: "typed" (the user picked a frequency with no mode), "channel" (a bank channel or VFO,
        which bring their mode), "scan", "restore", or the tuning module's name."""
        self.radio.set_frequency(hz)
        self._show_freq()
        self.bus.emit("tuned", freq_hz=int(self.radio.demod_hz), primary=True, how=how)
        # User tuning (typed/clicked/nudged) auto-fills the repeater offset from the band convention;
        # a channel/VFO/scan/restore brings its own offset, so don't override those.
        if how in ("typed", "click", "nudge"):
            self._auto_offset_for_tune(int(self.radio.demod_hz))
        self._update_ptt_enabled()      # the TX band guard depends on the tune

    # -- dual VFO -------------------------------------------------------------
    def _store_current_vfo(self):
        """Snapshot the radio's live tune into the active VFO slot (skipped mid-
        sweep so a transient scan frequency doesn't overwrite the slot)."""
        if not self._scanning:
            self._vfos[self._active_vfo] = {
                "freq": int(self.radio.freq_hz), "mode": self.radio.mode.name,
                "offset_hz": self._current_offset_hz(), "ctcss_hz": self._ctcss_hz,
                "ctcss_tx": self._ctcss_tx, "ctcss_rx": self._ctcss_rx,
                "tx_tones": self._tx_tones()}

    def _set_active_vfo(self, which):
        """Flip to VFO A or B: stash the current tune in the outgoing VFO, then
        tune to the incoming VFO's remembered freq + mode. A running scan is
        stopped (you're leaving it for a fixed frequency)."""
        if which not in ("A", "B"):
            return
        (self.vfo_a_btn if which == "A" else self.vfo_b_btn).setChecked(True)
        if which == self._active_vfo:
            return
        self._store_current_vfo()
        self._user_tunes()                   # (a scan is stopped by its lease's pre-empt)
        self._active_vfo = which
        v = self._vfos[which]
        self._leave_channel()
        mode = v.get("mode", "NFM")
        if mode in ANALOG_MODES or mode in self._mode_specs:
            self._apply_channel_mode(mode)
        self._set_freq(int(v.get("freq", 154_000_000)), how="channel")
        self._apply_offset(v.get("offset_hz", 0))
        self._apply_ctcss(v.get("ctcss_hz", 100.0), v.get("ctcss_tx", False),
                          v.get("ctcss_rx", False))
        for name, tone in (v.get("tx_tones") or {}).items():
            self._apply_tx_tone(tone, only=name)
        self.scan_status.setText(
            f"VFO {which}  {_fmt_mhz(self.radio.freq_hz)} MHz  {self.radio.mode.name}")

    def tune(self, freq_hz, recenter=True, owner="user"):
        """Tune for ``owner``. recenter=True moves the band (a primary retune): only allowed for the
        holder of the tuner lease, or anyone when nobody holds it; "user" always may (it pre-empts the
        holder). recenter=False moves the demod within the band. Returns (ok, reason)."""
        if recenter:
            if owner == "user":
                self._user_tunes()
            else:
                ok, why = self.tuner.may_tune(owner)
                if not ok:
                    return False, why
            if not self._scanning:
                self._leave_channel()
            self._set_freq(int(freq_hz), how="typed" if owner == "user" else owner)
        else:
            self.radio.set_demod(int(freq_hz))
            self._show_freq()
            self.bus.emit("tuned", freq_hz=int(self.radio.demod_hz), primary=False,
                          how="click" if owner == "user" else owner)
        return True, ""

    def _user_tunes(self):
        """The user is retuning by hand: whoever holds the band (a scan, a module) is pre-empted."""
        self._recalled_channel = None        # (a bank recall sets it again after this)
        self.tuner.user_tunes()

    def _leave_channel(self):
        """A retune by hand abandons the current channel: every decoder's claim on the speaker ends now
        (and its queued audio is dropped), so analog comes back under squelch control on the new
        frequency at once instead of after the decoder notices it lost sync. Modules reset the rest of
        their state from the "tuned" topic."""
        self.audio.release_all()

    def _on_display_tuned(self, hz):
        """User changed the frequency directly on the readout (digit click or
        typed entry) — recenter the radio on it."""
        self._user_tunes()
        if not self._scanning:
            self._leave_channel()
        self._set_freq(int(hz), how="typed")

    def _on_display_nudged(self, hz):
        """A 100 / 10 / 1 Hz digit step: the same channel, a little off. Move only the demod (as a
        waterfall click does), so the HackRF isn't retuned, modules keep their tracks, extra
        recorders keep running and a digital lock isn't dropped. Outside the band: a normal retune."""
        hz = int(hz)
        if abs(hz - self.radio.band_center_hz) > self.radio.demod_span():
            self._on_display_tuned(hz)
            return
        self.radio.set_demod(hz)
        self._show_freq()
        self.bus.emit("tuned", freq_hz=int(self.radio.demod_hz), primary=False, how="nudge")

    # -- save the tuned VFO into a memory bank -------------------------------
    def _commit_memory(self, bank_name, ch):
        """Add ``ch`` to ``bank_name`` (creating the bank if it's new), persist,
        and refresh the views. Returns the resolved bank name. Factored out of
        the dialog so it's unit-testable."""
        bank_name = (bank_name or self.store.active or "Saved").strip()
        self.store.create_bank(bank_name)            # no-op if it already exists
        self.store.add_channel(ch, bank_name)
        self.store.save()
        self._refresh_bank_combo()
        if bank_name == self.store.active:
            self._refresh_table()
        self._update_markers()                       # new marker on the waterfall
        return bank_name

    def _save_to_memory(self):
        """Save the currently tuned VFO frequency into a bank (existing or new)
        with a name / service / tone / notes — the quick 'I found something
        interesting while tuning' memory store."""
        freq = int(self.radio.freq_hz)
        mode = self.radio.mode.name
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Save to memory")
        form = QtWidgets.QFormLayout(dlg)
        form.addRow("Frequency:", QtWidgets.QLabel(f"{_fmt_mhz(freq)} MHz    {mode}"))

        bank_row = QtWidgets.QHBoxLayout()
        bank_combo = QtWidgets.QComboBox()
        bank_combo.setEditable(True)
        bank_combo.addItems(list(self.store.banks))
        bank_combo.setCurrentText(self.store.active or "")
        bank_combo.setToolTip("Choose an existing bank, type a new name, or "
                              "click New… to create one.")
        bank_row.addWidget(bank_combo, 1)
        new_bank_btn = QtWidgets.QPushButton("New…")
        new_bank_btn.setToolTip("Create a new bank to save into.")

        def _new_bank_in_dialog():
            name, ok = QtWidgets.QInputDialog.getText(
                dlg, "New bank", "New bank name:")
            name = name.strip() if ok else ""
            if name:
                if bank_combo.findText(name) < 0:
                    bank_combo.addItem(name)
                bank_combo.setCurrentText(name)

        new_bank_btn.clicked.connect(_new_bank_in_dialog)
        bank_row.addWidget(new_bank_btn)
        form.addRow("Bank:", bank_row)

        name_edit = QtWidgets.QLineEdit()
        hit = self._channel_for_freq(freq)           # prefill if it's a known freq
        if hit:
            name_edit.setText(hit[0])
        form.addRow("Name:", name_edit)

        svc_combo = QtWidgets.QComboBox()
        svc_combo.setEditable(True)
        svc_combo.addItem("")
        svc_combo.addItems(SERVICE_TAGS)
        form.addRow("Service:", svc_combo)

        tone_edit = QtWidgets.QLineEdit()
        tone_edit.setPlaceholderText(
            "131.8 PL · 023 DPL · 307 NAC · CC 6 TG 9   (optional)")
        form.addRow("Tone/ID:", tone_edit)

        notes_edit = QtWidgets.QPlainTextEdit()
        notes_edit.setPlaceholderText("free-form notes (optional)")
        notes_edit.setFixedHeight(56)
        form.addRow("Notes:", notes_edit)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        name_edit.setFocus()

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        bw = int(getattr(self.radio, "_bw_override", 0) or 0)   # keep a dragged BW
        ch = Channel(freq=freq, mode=mode, bw=bw,
                     label=name_edit.text().strip(),
                     service=svc_combo.currentText().strip(),
                     tone=tone_edit.text().strip(),
                     notes=notes_edit.toPlainText().strip(),
                     source="manual")
        self._module_channel_fields(ch)
        name = self._commit_memory(bank_combo.currentText(), ch)
        self.statusBar().showMessage(
            f"saved {_fmt_mhz(freq)} MHz → “{name}”"
            f"{('  ' + ch.label) if ch.label else ''}", 5000)

    def _set_volume(self, pct):
        self._init_volume = int(pct)         # remembered for the next session
        self.radio.set_volume(pct / 100.0)

    # -- SDR front-end start/stop/select -------------------------------------
    def _start_radio(self):
        """Start the flowgraph on the selected SDR (auto-run at launch + Start
        button). If the device isn't connected, stay stopped with a note. For the
        'IQ file' device with nothing loaded yet, prompt for a file first."""
        if self.radio.running:
            self._update_sdr_status()
            return
        if self.radio.device == IQ_FILE_DEVICE and not self.radio.replay:
            self._open_iq_file()        # prompts, then starts replay itself
            return
        try:
            self.radio.start()
        except Exception as e:
            self._update_sdr_status(err=str(e))
            return
        if self.radio.is_replay():      # sync the UI to the file's channel + mode
            self._sync_after_replay_start()
        # the device is open now: rebuild the gain sliders from its live caps (handle-backed, not the
        # disk cache) so they match the device that actually started
        self._rebuild_gain_controls()
        self._update_sdr_status()
        self.bus.emit("radio_started", replay=self.radio.is_replay())

    # -- IQ-file playback bar (DVR-style position + seek) ------------------------
    PLAYBACK_STEPS = 1000               # slider resolution (per mille of the file)
    PLAYBACK_SKIP_S = 10.0

    def _build_playback_bar(self):
        """Elapsed / slider / total for IQ-file replay. The slider follows the play position
        and can be dragged (or clicked) to seek; ⏪/⏩ jump 10 s. Shown only for 'IQ file'."""
        w = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        self.pb_back = QtWidgets.QToolButton()
        self.pb_back.setText("⏪")
        self.pb_back.setToolTip(f"Back {self.PLAYBACK_SKIP_S:.0f} s")
        self.pb_back.clicked.connect(lambda: self._playback_skip(-self.PLAYBACK_SKIP_S))
        self.pb_fwd = QtWidgets.QToolButton()
        self.pb_fwd.setText("⏩")
        self.pb_fwd.setToolTip(f"Forward {self.PLAYBACK_SKIP_S:.0f} s")
        self.pb_fwd.clicked.connect(lambda: self._playback_skip(self.PLAYBACK_SKIP_S))
        self.pb_pause = QtWidgets.QToolButton()
        self.pb_pause.setText("⏸")
        self.pb_pause.setToolTip("Pause / resume the replay (Space while the bar has focus)")
        self.pb_pause.clicked.connect(self._toggle_playback_pause)
        self.pb_elapsed = QtWidgets.QLabel("0:00")
        self.pb_total = QtWidgets.QLabel("0:00")
        for lab in (self.pb_elapsed, self.pb_total):
            lab.setStyleSheet("font-family:'DejaVu Sans Mono';")
        self.pb_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.pb_slider.setRange(0, self.PLAYBACK_STEPS)
        self.pb_slider.setToolTip("IQ-file play position: drag or click to jump")
        self.pb_slider.setPageStep(self.PLAYBACK_STEPS // 20)
        self._pb_dragging = False
        self.pb_slider.sliderPressed.connect(self._on_playback_press)
        self.pb_slider.sliderMoved.connect(self._on_playback_move)
        self.pb_slider.sliderReleased.connect(self._on_playback_release)
        self.pb_slider.actionTriggered.connect(self._on_playback_action)
        row.addWidget(self.pb_back)
        row.addWidget(self.pb_pause)
        row.addWidget(self.pb_elapsed)
        row.addWidget(self.pb_slider, 1)
        row.addWidget(self.pb_total)
        row.addWidget(self.pb_fwd)
        sc = QtWidgets.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Space), w)
        sc.setContext(QtCore.Qt.WidgetWithChildrenShortcut)    # only while the bar has focus
        sc.activated.connect(self._toggle_playback_pause)
        self.playback_bar = w
        w.setVisible(self._sdr_device == IQ_FILE_DEVICE)
        self._pb_timer = QtCore.QTimer(self)
        self._pb_timer.timeout.connect(self._update_playback_bar)
        self._pb_timer.timeout.connect(self._poll_core_topics)
        self._pb_timer.start(250)
        # api.spectrum: the display FFT for modules, at most 20 frames/s, only while someone listens
        self._spec_timer = QtCore.QTimer(self)
        self._spec_timer.timeout.connect(self._deliver_spectrum)
        self._spec_timer.start(int(1000 / _api_spectrum.MAX_RATE_HZ))
        return w

    def _deliver_spectrum(self):
        r = self.radio
        if not _api_spectrum.subscribers() or not r.running or getattr(r, "probe", None) is None:
            return
        if getattr(self.spectrum, "_tx_probe", None) is not None:
            return                               # transmitting: the band isn't being received
        import numpy as np
        try:
            lv = np.asarray(r.probe.level(), dtype=np.float64)
        except Exception:
            return
        if lv.size == 0 or not np.isfinite(lv).any():
            return
        from .spectrum import blank_dc
        db = blank_dc(lv, samp_rate=r.samp_rate).astype(np.float32)
        _api_spectrum.deliver(_api_spectrum.SpectrumFrame(t=station.now(), center_hz=int(r.band_center_hz),
                                                          samp_rate=float(r.samp_rate), db=db))

    def _poll_core_topics(self):
        """band_changed / rate_changed whenever the band moves or the rate changes (by any path), and
        replay_wrap when a looping file restarts (the position jumps back without a seek)."""
        r = self.radio
        band = (int(r.band_center_hz), int(r.samp_rate))
        last = getattr(self, "_last_band", None)
        if last != band:
            self._last_band = band
            if last is not None:
                if band[1] != last[1]:
                    self.bus.publish("rate_changed", samp_rate=band[1])
                self.bus.publish("band_changed", center_hz=band[0], samp_rate=band[1])
        pos = r.replay_position_s() if (r.running and r.is_replay()) else None
        prev = getattr(self, "_replay_last_pos", None)
        if pos is not None and prev is not None and (r.replay or {}).get("loop") and pos + 0.5 < prev:
            self.bus.publish("replay_wrap", position_s=pos)
        self._replay_last_pos = pos

    @staticmethod
    def _fmt_clock(s):
        s = max(0, int(round(s)))
        return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

    def _update_playback_bar(self):
        show = self.radio.device == IQ_FILE_DEVICE and self.radio.replay is not None
        if self.playback_bar.isVisible() != show:
            self.playback_bar.setVisible(show)
        if not show:
            return
        dur = self.radio.replay_duration_s() or 0.0
        pos = self.radio.replay_position_s()
        self.pb_total.setText(self._fmt_clock(dur))
        paused = self.radio.replay_paused()
        self.pb_pause.setText("▶" if paused else "⏸")
        self.pb_elapsed.setStyleSheet("font-family:'DejaVu Sans Mono';"
                                      + ("color:#f0b429;" if paused else ""))
        if self._pb_dragging:                  # the user owns the slider until release
            return
        if pos is None:
            pos = 0.0
        self.pb_elapsed.setText(self._fmt_clock(pos))
        self.pb_slider.blockSignals(True)
        self.pb_slider.setValue(int(round(self.PLAYBACK_STEPS * pos / dur)) if dur else 0)
        self.pb_slider.blockSignals(False)

    def _playback_slider_s(self, value=None):
        dur = self.radio.replay_duration_s() or 0.0
        v = self.pb_slider.value() if value is None else value
        return dur * v / self.PLAYBACK_STEPS

    def _on_playback_press(self):
        self._pb_dragging = True

    def _on_playback_move(self, value):
        self.pb_elapsed.setText(self._fmt_clock(self._playback_slider_s(value)))

    def _on_playback_release(self):
        self._pb_dragging = False
        self._playback_seek(self._playback_slider_s())

    def _on_playback_action(self, action):
        # click in the groove / PgUp / arrow keys: seek to where the slider moves to
        if action in (QtWidgets.QAbstractSlider.SliderPageStepAdd, QtWidgets.QAbstractSlider.SliderPageStepSub,
                      QtWidgets.QAbstractSlider.SliderSingleStepAdd, QtWidgets.QAbstractSlider.SliderSingleStepSub):
            QtCore.QTimer.singleShot(0, lambda: self._playback_seek(self._playback_slider_s()))

    def _toggle_playback_pause(self):
        """⏸/▶: pause in place (the flowgraph keeps running on silence). If the replay isn't
        running yet (stopped or at EOF), ▶ starts it from where the bar points."""
        if not self.radio.running:
            self.radio.set_replay_paused(False)
            self._start_radio()
        else:
            self.radio.set_replay_paused(not self.radio.replay_paused())
            self.bus.publish("replay_pause", paused=self.radio.replay_paused(),
                             position_s=self.radio.replay_position_s())
        self._update_playback_bar()

    def _playback_skip(self, delta_s):
        pos = self.radio.replay_position_s() or 0.0
        self._playback_seek(pos + delta_s)

    def _playback_seek(self, seconds):
        was = self.radio.replay_position_s()
        if not self.radio.seek_replay(seconds):
            return
        self.spectrum.clear_history()         # the rows on screen were a different moment
        self._update_playback_bar()
        to = self.radio.replay_position_s()
        self._replay_last_pos = to
        # decoders reset rather than decode across the jump
        self.bus.publish("replay_seek", from_s=was, to_s=to)

    def _stop_radio(self):
        if self._rec_path is not None:  # a recording hangs off the source: finish it before it goes
            self._stop_record()
        self.radio.stop()               # releases the device
        self._update_sdr_status()
        self.bus.emit("radio_stopped")

    def _check_replay_eof(self):
        if not self.radio.replay_finished():
            return
        name = os.path.basename((self.radio.replay or {}).get("path", ""))
        if self._rec_path is not None:     # recording the replay: finish the file too
            self._stop_record()
        self._stop_radio()                 # -> button back to "Start"
        self.statusBar().showMessage(f"replay finished: {name}  (Start plays it again)", 8000)

    def _toggle_radio(self):
        self._stop_radio() if self.radio.running else self._start_radio()

    def _on_sdr_change(self, name):
        """``name``: a device key (sources.devices()), an old fixed name, or IQ_FILE_DEVICE."""
        if name is None:
            return
        if name == IQ_FILE_DEVICE:
            if not self._open_iq_file():    # canceled -> revert to previous device
                self._select_sdr_combo(self._sdr_device)
            return
        dev = self._sources.find(name)
        if dev is not None:
            name = dev.key
        if name == self._sdr_device and self.radio.device == name:
            return
        was = self.radio.running
        was_adsb = self._in_adsb
        if was_adsb:
            self._stop_adsb()           # clean app+radio teardown before the drop
        self._save_device_settings()                # the old device keeps its gains / ppm / rate
        self.radio.set_device(name, self._sources.settings(dev) if dev is not None else None)
        if dev is not None:
            self._prune_foreign_gains(dev)        # drop stray stages before they reach the hardware
        self._sdr_device = self.radio.device
        self._hw_device = self.radio.device       # a real device: remember for restart
        self._select_sdr_combo(name)
        if hasattr(self, "_sdr_dev_actions"):     # keep the Settings menu in step
            a = self._sdr_dev_actions.get(self.radio.device)
            if a is not None and not a.isChecked():
                a.setChecked(True)
        rates = self._rates_for(name)
        want = int((self._sources.settings(dev) if dev is not None else {}).get("rate") or self.radio.samp_rate)
        if want not in rates:
            want = SAMP_RATE if SAMP_RATE in rates else (rates[0] if rates else SAMP_RATE)
        if want != self.radio.samp_rate:
            self.radio.set_samp_rate(want)
        self._refresh_bw_combo()
        self._rebuild_gain_controls()
        self._restart_preserving_adsb(was, was_adsb)
        self._update_ptt_enabled()

    # -- devices (sdrexplorer.sources) --------------------------------------------------------------
    def _rates_for(self, key):
        """The capture rates offered for a device: SAMP_RATE_OPTIONS it accepts (all of them if its
        driver reports no range), always at least the default. An IQ file: the default only."""
        if key == IQ_FILE_DEVICE:
            return [SAMP_RATE]
        dev = self._sources.find(key)
        if dev is None:
            return list(SAMP_RATE_OPTIONS)
        try:
            c = self._sources.caps(dev, self.radio.handle if getattr(self, "radio", None) is not None
                                   and self.radio.devinfo is dev else None)
        except Exception:
            return [SAMP_RATE]
        if not c.rate_ranges and not c.rates:
            return list(SAMP_RATE_OPTIONS)
        ok = [r for r in SAMP_RATE_OPTIONS if c.accepts_rate(r)]
        return ok or [SAMP_RATE]

    def _device_caps(self):
        if self.radio.device == IQ_FILE_DEVICE:
            return None
        try:
            return self.radio.caps()
        except Exception:
            return None

    def _fill_sdr_combo(self):
        self.sdr_combo.blockSignals(True)
        self.sdr_combo.clear()
        for d in self._sources.devices():
            self.sdr_combo.addItem(d.label + ("" if d.present else "  (not connected)"), d.key)
            self.sdr_combo.setItemData(self.sdr_combo.count() - 1,
                                       f"{d.label}\n{d.args}" + ("" if d.present else "\nnot attached now"),
                                       QtCore.Qt.ToolTipRole)
        self.sdr_combo.addItem(IQ_FILE_DEVICE, IQ_FILE_DEVICE)
        self.sdr_combo.blockSignals(False)
        self._select_sdr_combo(self._sdr_device)

    def _select_sdr_combo(self, key):
        if not hasattr(self, "sdr_combo"):
            return
        i = self.sdr_combo.findData(key)
        if i < 0:
            dev = self._sources.find(key)
            i = self.sdr_combo.findData(dev.key) if dev is not None else -1
        if i >= 0:
            self.sdr_combo.blockSignals(True)
            self.sdr_combo.setCurrentIndex(i)
            self.sdr_combo.blockSignals(False)

    def _fill_sdr_menu(self):
        menu = self._sdr_menu
        menu.clear()
        for a in self._sdr_dev_group.actions():
            self._sdr_dev_group.removeAction(a)
        self._sdr_dev_actions = {}
        for key, label in [(d.key, d.label) for d in self._sources.devices()] + [(IQ_FILE_DEVICE, IQ_FILE_DEVICE)]:
            a = QtWidgets.QAction(label, self, checkable=True)
            a.setChecked(key == self._sdr_device)
            a.triggered.connect(lambda _c, k=key: self._on_sdr_change(k))
            self._sdr_dev_group.addAction(a)
            self._sdr_dev_actions[key] = a
            menu.addAction(a)

    def _rescan_devices(self):
        """Settings ▸ Find SDR devices: list what's attached now (a device plugged in after start)."""
        self._sources.reset_cache()
        self._sources.forget_caps()           # a re-plugged / updated device is asked again
        devs = self._sources.scan()
        self._fill_sdr_combo()
        self._fill_sdr_menu()
        present = [d.label for d in devs if d.present]
        self.statusBar().showMessage(("found: " + ", ".join(present)) if present else "no SDR found", 6000)

    def _prune_foreign_gains(self, dev):
        """Drop any saved gain stage the device doesn't report, in memory and on disk. An old
        cross-device leak left names like "LNA" in a HackRF's gains; gr-osmosdr routes an unknown name
        to the overall gain and corrupts the amp (no signal until the user toggles RF)."""
        g = self.radio.dev_settings.get("gains")
        if not g:
            return
        try:
            valid = {s.name for s in self._sources.caps(dev).gains}
        except Exception:
            return
        if not valid:
            return
        pruned = {k: v for k, v in g.items() if k in valid}
        if pruned != g:
            self.radio.dev_settings["gains"] = pruned
            try:
                self._sources.save_settings(dev, gains=pruned)
            except OSError:
                pass

    def _migrate_legacy_gains(self, dev, legacy):
        """One-time import of the old device-agnostic state.json "gains" {rf,if,bb} onto ``dev``, mapped
        to its stage names, if it has no per-device gains saved yet. Keeps an upgrading user's HackRF
        gains instead of resetting to defaults. A no-op once sources.json has gains for the device."""
        if not legacy or (self.radio.dev_settings.get("gains")):
            return
        try:
            stages = [g.name for g in self._sources.caps(dev).gains]
        except Exception:
            stages = []
        if not stages:
            return
        vals = {"RF": legacy.get("rf"), "IF": legacy.get("if"), "BB": legacy.get("bb"),
                "AMP": legacy.get("rf"), "LNA": legacy.get("if"), "VGA": legacy.get("bb")}
        out = {s: int(vals[s]) for s in stages if vals.get(s) is not None}
        if not out and stages and legacy.get("rf") is not None:
            out[stages[0]] = int(legacy["rf"])     # single-stage tuner: the old RF scalar
        if not out:
            return
        self.radio.dev_settings["gains"] = out
        try:
            self._sources.save_settings(dev, gains=out)
        except OSError:
            pass

    def _save_device_settings(self):
        """The current device's gains / ppm / antenna / rate, kept per device (sources.json)."""
        dev = self.radio.devinfo or (self._sources.find(self.radio.device)
                                     if self.radio.device != IQ_FILE_DEVICE else None)
        if dev is None:
            return
        st = dict(self.radio.dev_settings)
        st["rate"] = int(self.radio.samp_rate)
        try:
            self._sources.save_settings(dev, **st)
        except OSError:
            pass

    def _refresh_bw_combo(self):
        """(Re)populate the capture-bandwidth combo for the current device:
        HackRF gets the full SAMP_RATE_OPTIONS range; every other device
        (RTL-SDR, IQ file) is locked to the default 2.4 Msps — RTL never
        supported wideband capture, and a replay file's rate comes from its
        own sidecar, not this control."""
        if not hasattr(self, "bw_combo"):
            return
        opts = self._rates_for(self._sdr_device)
        self.bw_combo.blockSignals(True)
        self.bw_combo.clear()
        for rate in opts:
            self.bw_combo.addItem(f"{rate/1e6:.1f} Msps", rate)
        idx = self.bw_combo.findData(self.radio.samp_rate)
        self.bw_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.bw_combo.setEnabled(len(opts) > 1 and self._sdr_device != IQ_FILE_DEVICE)
        self.bw_combo.blockSignals(False)

    def _on_samp_rate_change(self, _index):
        rate = self.bw_combo.currentData()
        if rate is None or int(rate) == self.radio.samp_rate:
            return
        was = self.radio.running
        was_adsb = self._in_adsb
        if was_adsb:
            self._stop_adsb()           # clean app+radio teardown before the rebuild
        self.radio.set_samp_rate(int(rate))
        if hasattr(self, "spectrum"):
            self.spectrum.reset_view()    # old zoom/history no longer match the band
        self._restart_preserving_adsb(was, was_adsb)

    def _restart_preserving_adsb(self, was_running, was_adsb):
        """Restart after a radio-level reconfigure (device/bandwidth change)
        that already called ScannerRadio.stop() as a side effect — which also
        tears down the gr-adsb decode chain (ScannerRadio.stop() unconditionally
        calls stop_adsb()). set_device()/set_samp_rate() don't know about ADS-B
        mode, so without this it's left silently dead (audio path restored,
        _in_adsb stuck True, adsb_monitor reading a dead queue) — heard as raw
        static at 1090 MHz instead of the analog path staying muted."""
        if not was_running:
            self._update_sdr_status()
            return
        self._start_radio()
        if was_adsb:
            self.radio.mute()           # belt-and-suspenders, matches _enter_mode
            self._start_adsb()          # rebuild the gr-adsb chain at the new config

    def _open_iq_file(self):
        """Pick a saved .cs16 capture (+ its .json sidecar) and start replaying it
        through the full pipeline (waterfall + audio + decoders). Returns True if
        replay started, False if canceled."""
        os.makedirs(_recordings_dir(), exist_ok=True)
        cur = (self.radio.replay or {}).get("path")   # reopen where the last pick was
        start = os.path.dirname(cur) if cur and os.path.isdir(os.path.dirname(cur)) \
            else _recordings_dir()
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open IQ file for replay", start,
            "IQ captures (*.cs16 *.iq *.raw);;All files (*)")
        if not path:
            return False
        meta = {}
        try:
            with open(path + ".json") as fh:       # self-describing sidecar
                meta = json.load(fh)
        except (OSError, ValueError):
            pass
        if self.radio.running:
            self._stop_radio()
        self.radio.set_replay(path, meta, self.loop_cb.isChecked())
        self._sdr_device = IQ_FILE_DEVICE
        self._select_sdr_combo(IQ_FILE_DEVICE)
        self._refresh_bw_combo()
        try:
            self.radio.start()
        except Exception as e:
            self._update_sdr_status(err=str(e))
            return True
        self._sync_after_replay_start(meta)
        self._update_sdr_status()
        self.bus.emit("radio_started", replay=True)
        return True

    def _sync_after_replay_start(self, meta=None):
        """After replay starts, refresh the UI. If the user's VFO lies inside the recording, the radio
        kept it (radio.replay_kept_vfo) and so the current mode is kept too -- they tuned there on
        purpose. Only when the VFO had to fall back to the file's channel is the file's recorded mode
        applied as well."""
        meta = meta or (self.radio.replay or {}).get("meta") or {}
        mode = meta.get("mode")
        if (not self.radio.replay_kept_vfo and mode in ANALOG_MODES
                and mode != self.radio.mode.name):
            self._enter_mode(mode)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentText(mode)
            self.mode_combo.blockSignals(False)
        self._show_freq()
        if hasattr(self, "spectrum"):
            self.spectrum.update()
        path = (self.radio.replay or {}).get("path", "")
        self.statusBar().showMessage(
            f"replaying {os.path.basename(path)}  ({meta.get('samp_rate', '?')} sps @ "
            f"{meta.get('center_hz', 0)/1e6:.4f} MHz) · "
            + (f"kept VFO {self.radio.demod_hz/1e6:.5f} MHz" if self.radio.replay_kept_vfo
               else f"VFO outside the recording -> recorded channel {self.radio.demod_hz/1e6:.5f} MHz"),
            6000)

    def _on_loop_toggle(self, on):
        # apply live: rebuild the file source so the repeat flag takes effect now
        if self.radio.device == IQ_FILE_DEVICE and self.radio.replay is not None:
            self.radio.set_replay_loop(on)
            self.bus.publish("replay_loop", enabled=bool(on))
            if self.radio.running:
                self._stop_radio()
                self._start_radio()

    def _update_sdr_status(self, err=None):
        if not hasattr(self, "sdr_btn"):
            return
        running = self.radio.running
        self.sdr_btn.setText("Stop" if running else "Start")
        if hasattr(self, "iq_open_btn"):
            self.iq_open_btn.setVisible(self.radio.device == IQ_FILE_DEVICE)
        gpu_tag = " · GPU" if getattr(self.radio, "use_gpu", False) else ""
        self.sdr_status.setToolTip("")
        if err:
            # "not connected" only when it really looks like the device; otherwise show the
            # actual failure -- a flowgraph wiring error used to masquerade as a missing HackRF
            low = err.lower()
            device_err = any(k in low for k in ("no supported devices", "not found", "hackrf_open",
                                                "failed to open", "no device", "resource busy"))
            self.sdr_status.setText(f"{self._device_label()}: not connected" if device_err
                                    else f"{self._device_label()}: start failed — {err[:60]}")
            self.sdr_status.setToolTip(err)
            self.sdr_status.setStyleSheet("color:#ff6b6b;")
            print(f"[sdrexplorer] start failed on {self.radio.device}: {err}", flush=True)
        elif running:
            self.sdr_status.setText(f"running · {self._device_label()}{gpu_tag}")
            self.sdr_status.setStyleSheet("color:#39ff88;")
        else:
            self.sdr_status.setText(f"stopped · {self._device_label()}{gpu_tag}")
            self.sdr_status.setStyleSheet("color:#8b95a1;")

    def _device_label(self):
        if self.radio.device == IQ_FILE_DEVICE:
            return IQ_FILE_DEVICE
        dev = self.radio.devinfo or self._sources.find(self.radio.device)
        return dev.label if dev is not None else str(self.radio.device)

    # -- PTT / transmit -------------------------------------------------------
    def _tx_mode(self):
        """The mode PTT would transmit right now: the module transmit mode the main-UI TX: combo picks
        (independent of the receive mode picker) or, for 'Analog', whatever analog mode is currently
        tuned (radio.mode.name)."""
        if self._tx_digital_mode in self._tx_specs:
            return self._tx_digital_mode
        return self.radio.mode.name

    def _tx_mode_available(self, tx_mode):
        spec = self._tx_spec(tx_mode)
        return self._mode_available(spec)[0] if spec is not None else True

    def _update_ptt_enabled(self):
        """PTT needs a transmitter, a TX-capable mode (see radio.TX_MODES — NFM/AM/
        WFM/USB/LSB, or a module's transmit mode the main-UI TX: combo selects, if
        it is available; CW and modules' receive modes have no
        modulator), and can't run while something else owns the device
        lifecycle (a channel sweep or ADS-B) — but stays enabled while
        already transmitting so it can be un-pressed. The TX: mode combo and
        the whole Settings ▸ Transmit dialog are HackRF-only too (RTL-SDR/IQ
        file can't transmit at all), so gray them out in lockstep rather than
        leaving them interactive but inert."""
        caps = self._device_caps()
        can_tx = bool(caps and caps.tx)
        if hasattr(self, "tx_mode_combo"):
            self.tx_mode_combo.setEnabled(can_tx)
        if hasattr(self, "_tx_ctl_widget"):
            self._tx_ctl_widget.setEnabled(can_tx)
        if not hasattr(self, "ptt_btn"):
            return
        if self.tx is not None:
            self.ptt_btn.setEnabled(True)
            return
        tx_mode = self._tx_mode()
        ok = (tx_mode in TX_MODES or tx_mode in self._tx_specs) and self._tx_mode_available(tx_mode)
        # Safety: the frequency PTT would key must be inside a US amateur band
        # (radio.in_us_ham_band) unless the operator set the override. This gates
        # the button itself (the hard stop in _start_tx stays as a backstop), and
        # drives the tooltip so hovering explains an out-of-band block.
        tx_freq = self.radio.demod_hz + self._current_offset_hz()
        in_band, _band = in_us_ham_band(tx_freq)
        band_ok = in_band or self._tx_ham_override
        self.ptt_btn.setEnabled(
            can_tx and not self._scanning and not self._in_adsb and ok and band_ok)
        if can_tx and not band_ok:
            self.ptt_btn.setToolTip(
                f"{tx_freq/1e6:.4f} MHz is outside the US amateur bands — PTT "
                "is blocked (enable the override in Settings ▸ Transmit if "
                "you are licensed).")
        else:
            self.ptt_btn.setToolTip(self._PTT_TOOLTIP)

    def _toggle_ptt(self):
        self._stop_tx() if self.tx is not None else self._start_tx()

    def _on_tx_digital_mode_change(self, text):
        self._tx_digital_mode = text if text in self._tx_specs else "Analog"
        self._tx_mode_saved = self._tx_digital_mode
        self._show_tx_quick_widget()
        self._update_ptt_enabled()

    def _start_tx(self):
        if self.tx is not None:
            return
        caps = self._device_caps()
        if not (caps and caps.tx):
            self.statusBar().showMessage(f"PTT: {self._device_label()} has no transmitter"
                                         + (f" ({caps.tx_why})" if caps and caps.tx_why else ""), 4000)
            return
        if self._tx_source == "file" and not self._tx_file:
            self.statusBar().showMessage(
                "choose a TX audio file first (Settings ▸ Transmit…)", 4000)
            return
        tx_mode = self._tx_mode()
        spec = self._tx_spec(tx_mode)
        if spec is not None and not self._mode_available(spec)[0]:
            self.statusBar().showMessage(f"{tx_mode} TX: {self._mode_available(spec)[1]}", 4000)
            return
        if tx_mode not in TX_MODES and spec is None:
            self.statusBar().showMessage(
                f"PTT isn't supported in {tx_mode} mode", 4000)
            return
        tx_freq = self.radio.demod_hz + self._current_offset_hz()
        # Safety: transmit only inside US amateur bands unless the operator has
        # taken responsibility (Settings > Transmit). This is the single hard
        # stop every TX mode (analog and module) passes through.
        ok, band = in_us_ham_band(tx_freq)
        if not ok and not self._tx_ham_override:
            self.statusBar().showMessage(
                f"TX blocked: {tx_freq/1e6:.4f} MHz is outside the US amateur bands "
                "(enable the override in Settings ▸ Transmit if you are licensed)", 6000)
            return
        self._resume_rx = self.radio.running
        if self._resume_rx:
            if self._rec_path is not None:     # the receiver stops for TX: finish the recording first
                self._stop_record()
                self.statusBar().showMessage("recording stopped: the receiver is off while transmitting", 6000)
            self.radio.stop()          # release the HackRF — TX needs it now
        try:
            self.tx = TxChain(
                freq_hz=tx_freq, source=self._tx_source,
                tx_gains=(self._tx_rf_gain, self._tx_if_gain),
                device=self.radio.device, file_path=self._tx_file,
                loop=self._tx_loop, mode=tx_mode,
                ctcss_hz=(self._ctcss_hz if self._ctcss_tx else 0.0), tx_spec=spec)
            self.tx.start()
        except Exception as e:
            self.tx = None
            if self._resume_rx:
                self.radio.start()
            self.statusBar().showMessage(f"TX failed: {e}", 6000)
            return
        self.ptt_btn.setText("■ ON AIR")
        self.ptt_btn.setStyleSheet("color:#ff5555; font-weight:bold;")
        # Show the TX frequency on the main VFO readout (cosmetic only -- the tuned
        # frequency is untouched, so it reverts on release). For repeater work this
        # reads the output frequency while keyed.
        self.freq_disp.set_tx_frequency(tx_freq)
        self.sdr_btn.setEnabled(False)
        self.sdr_combo.setEnabled(False)
        self.bw_combo.setEnabled(False)
        # Show the actual modulated signal on the waterfall instead of a
        # frozen last-RX frame — the RX source is stopped for the duration
        # of TX (HackRF is half-duplex).
        self.spectrum.set_tx_display(self.tx.probe, self.tx.samp_rate, self.tx.freq_hz)
        if self._tx_source == "file" and not self._tx_loop:
            self._tx_watcher = TxWatcher(self.tx)
            self._tx_watcher.finished_tx.connect(self._on_tx_file_done)
            self._tx_watcher.start()

    def _stop_tx(self):
        self._tx_watcher = None
        if self.tx is not None:
            self.tx.stop()
            self.tx = None
        self.spectrum.clear_tx_display()    # back to the live RX view
        self.freq_disp.clear_tx_frequency()  # VFO readout back to the (unchanged) RX frequency
        self.ptt_btn.setText("🎙 PTT")
        self.ptt_btn.setStyleSheet("")
        self.sdr_btn.setEnabled(True)
        self.sdr_combo.setEnabled(True)
        self.bw_combo.setEnabled(len(self._rates_for(self._sdr_device)) > 1
                                  and self._sdr_device != IQ_FILE_DEVICE)
        resume, self._resume_rx = self._resume_rx, False
        if resume:
            self._start_radio()
        else:
            self._update_sdr_status()
        self._update_ptt_enabled()

    def _on_tx_file_done(self, watcher):
        if self._tx_watcher is watcher:      # ignore a stale/already-stopped watcher
            self._stop_tx()

    def _on_mode_change(self, name):
        self._enter_mode(name)

    def _enter_mode(self, name):
        """Set a demod mode. A module's mode (api.ModeSpec, picker "mode") is entered through its
        enter() / leave(); the built-in ones are handled below. ADS-B is special (not in MODES): it
        parks the radio at 1090 MHz and runs the gr-adsb decode branch."""
        prev = self._module_mode
        if prev is not None and prev != name:
            self._module_mode = None
            m, spec = self._mode_specs[prev]
            if spec.leave is not None:
                self._call_module(m, "leave " + prev, spec.leave, self)
        if name in self._mode_specs:
            if name == prev:
                return
            m, spec = self._mode_specs[name]
            ok, why = self._mode_available(spec)
            if not ok:
                self.dig_status.setText(f"{name}: {why}")
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentText(self.radio.mode.name)
                self.mode_combo.blockSignals(False)
                return
            if spec.demod is not None:
                self._enter_builtin_mode(spec.name if not isinstance(spec.demod, str) else spec.demod)
            self._module_mode = name
            if spec.enter is not None:
                self._call_module(m, "enter " + name, spec.enter, self)
            self.bus.emit("mode_changed", name=name, picker="mode")
            return
        self._enter_builtin_mode(name)
        if name in MODES or name == "ADS-B":
            self.bus.emit("mode_changed", name=name, picker="mode")

    def _enter_builtin_mode(self, name):
        # leaving ADS-B for any other mode: stop the branch, return to prior tune
        if self._in_adsb and name != "ADS-B":
            self._stop_adsb()
            self._in_adsb = False
            self.tuner.release("ADS-B")
            self._update_ptt_enabled()
            if self._pre_adsb:
                # back to the tune from before ADS-B, through _set_freq so the frequency display
                # (and anything listening for "tuned") follows -- radio.set_frequency alone moved
                # the band but left the display on 1090 MHz
                self._set_freq(self._pre_adsb[0], how="restore")
                self._pre_adsb = None
        if name == "ADS-B":
            if self._in_adsb:
                return
            if not ADSB_AVAILABLE:
                self.dig_status.setText(
                    "ADS-B needs gr-adsb on the path (set ADSB_PATH; see README)")
                self.mode_combo.blockSignals(True)     # revert the selection
                self.mode_combo.setCurrentText(self.radio.mode.name)
                self.mode_combo.blockSignals(False)
                return
            if not self.tuner.acquire("ADS-B", TUNE_PARK, "parked at 1090 MHz",
                                      on_preempt=lambda by: self._adsb_preempted(by)):
                self.dig_status.setText("ADS-B: " + self.tuner.may_tune("ADS-B")[1])
                self.mode_combo.blockSignals(True)
                self.mode_combo.setCurrentText(self.radio.mode.name)
                self.mode_combo.blockSignals(False)
                return
            self._pre_adsb = (int(self.radio.demod_hz), self.radio.mode.name)
            if self.radio.mode.name == "WFM":
                self._stop_rds()
            self._in_adsb = True
            self._update_ptt_enabled()
            self.radio.mute()                          # no analog audio at 1090
            self.radio.set_frequency(ADSB_FREQ)
            self._show_freq()
            self._start_adsb()
            return
        if name not in MODES and name not in self.radio.extra_modes:
            return
        if self.radio.mode.name == "WFM" and name != "WFM":
            self._stop_rds()
        self.radio.set_mode(name)
        self._update_ptt_enabled()          # TX-capable set differs per mode
        if name in ("USB", "LSB"):
            self._set_snap_khz(1.0)         # SSB tunes/snaps in 1 kHz steps
        elif name == "CW":
            self._set_snap_khz(0.1)         # CW: 100 Hz steps, onto the carrier
        if name == "WFM":
            self._start_rds()
        # a decoder running in parallel can hold a claim on the speaker (a false
        # lock on an FSK signal, say) that would otherwise persist across the mode
        # change and leave you with no audio regardless of squelch. End it and
        # re-apply the gate from squelch immediately.
        if not self._scanning:
            self._leave_channel()
            self._update_signal()

    def _start_rds(self):
        if self.rds_monitor is not None:
            return
        self.radio.start_rds()
        q = getattr(self.radio, "rds_q", None)
        if q is not None:
            self.rds_monitor = RdsMonitor(q, self)
            self.rds_monitor.page.connect(self._on_rds_update)
            self.rds_monitor.start()
            self.dig_status.setText("RDS decode active (gr-rds)")
        else:
            self.dig_status.setText(
                "RDS needs gr-rds on the path (see README)")

    def _stop_rds(self):
        if self.rds_monitor is not None:
            self.rds_monitor.stop()
            self.rds_monitor.wait(1000)
            self.rds_monitor = None
        self.radio.stop_rds()
        self._last_rds = None
        self._rds_ps = ""
        self._show_freq()               # drop any RDS PS from the subtitle

    # -- ADS-B aircraft reception (gr-adsb) ----------------------------------
    def _start_adsb(self):
        if self.adsb_monitor is not None:
            return
        try:
            self.radio.start_adsb()
        except Exception as e:
            # the radio put its flowgraph back as it was; leave ADS-B for the mode it was entered from
            print(f"[sdrexplorer] ADS-B failed to start: {e}", file=sys.stderr)
            prev = self._pre_adsb[1] if self._pre_adsb else "NFM"
            QtCore.QTimer.singleShot(0, lambda: self.mode_combo.setCurrentText(prev))
            msg = f"ADS-B failed to start: {e}"
            QtCore.QTimer.singleShot(0, lambda: self.statusBar().showMessage(msg, 15000))
            return
        q = getattr(self.radio, "adsb_q", None)
        if q is None:
            self.dig_status.setText("ADS-B: start the SDR first (needs it running)")
            return
        self.adsb_monitor = AdsbMonitor(q, self)
        self.adsb_monitor.aircraft.connect(self._on_aircraft)
        self.adsb_monitor.start()
        self._show_aircraft_window()
        # Pre-warm the map (hidden): spin up Chromium and pay the one-time cold
        # start + IPv6-fallback tile fetch now, so opening View → Live map later
        # is instant instead of an ~8 s black screen. Harmless if it's never
        # opened, and skipped entirely when PyQtWebEngine isn't installed.
        self._ensure_map_window()
        self.dig_status.setText("ADS-B decode active (1090 MHz)")

    def _adsb_preempted(self, by):
        """Something of higher priority took the band (the user tuning by hand): leave ADS-B for the
        mode it was entered from, without going back to the old frequency (the user just chose one)."""
        if not self._in_adsb:
            return
        self._pre_adsb = None
        prev = self.radio.mode.name
        self._in_adsb = False
        self._stop_adsb()
        self._update_ptt_enabled()
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentText(prev)
        self.mode_combo.blockSignals(False)
        self.radio.unmute()

    def _stop_adsb(self):
        if self.adsb_monitor is not None:
            self.adsb_monitor.stop()
            self.adsb_monitor.wait(1000)
            self.adsb_monitor = None
        self.radio.stop_adsb()

    def _ensure_aircraft_window(self):
        """Build the modeless aircraft table if it doesn't exist yet. Does NOT
        show or raise it — per-message updates call this so a decode never
        steals focus/z-order from whatever the user has on top (e.g. the map)."""
        if self._adsb_win is None:
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle("ADS-B aircraft")
            dlg.resize(760, 380)
            v = QtWidgets.QVBoxLayout(dlg)
            # detection threshold — gr-adsb's framer power threshold, very
            # gain-dependent; tune it while watching the Msgs column climb.
            ctl = QtWidgets.QHBoxLayout()
            ctl.addWidget(QtWidgets.QLabel("Detect threshold:"))
            thr = QtWidgets.QDoubleSpinBox()
            thr.setDecimals(4)
            thr.setRange(0.0005, 2.0)
            thr.setSingleStep(0.005)
            thr.setValue(getattr(self.radio, "_adsb_threshold", 0.010))
            thr.setToolTip("gr-adsb framer power threshold. Lower = more sensitive "
                           "(more decodes + more noise); raise if you get garbage. "
                           "Adjust while watching Msgs climb.")
            thr.valueChanged.connect(self.radio.set_adsb_threshold)
            ctl.addWidget(thr)
            ctl.addStretch(1)
            self._adsb_status = QtWidgets.QLabel("")
            ctl.addWidget(self._adsb_status)
            v.addLayout(ctl)
            cols = ["ICAO", "Callsign", "Class", "Alt ft", "Speed kt", "Track°",
                    "Lat", "Lon", "Msgs", "Age s"]
            t = QtWidgets.QTableWidget(0, len(cols))
            t.setHorizontalHeaderLabels(cols)
            t.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            t.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
            v.addWidget(t)
            self._adsb_win, self._adsb_table = dlg, t
            self._adsb_timer = QtCore.QTimer(self)
            self._adsb_timer.timeout.connect(self._adsb_tick)
            self._adsb_timer.start(1000)

    def _show_aircraft_window(self):
        """Build (if needed), show, and bring the aircraft table to the front.
        For explicit user actions only — the menu item and ADS-B mode entry."""
        self._ensure_aircraft_window()
        self._adsb_win.show()
        self._adsb_win.raise_()
        self._adsb_win.activateWindow()

    @staticmethod
    def _fmt(v, fmt="{}"):
        # blank for unknown: None, "", or NaN (gr-adsb uses np.nan before a field
        # resolves). v != v is the NaN test (works for float and np.float64).
        if v is None or v == "" or (isinstance(v, float) and v != v):
            return ""
        return fmt.format(v)

    def _on_aircraft(self, d):
        """Upsert an aircraft row (keyed by ICAO) from a gr-adsb decoded dict."""
        icao = str(d.get("icao") or "").strip()
        if not icao:
            return
        self._ensure_aircraft_window()      # build if needed; never raises/steals focus
        t = self._adsb_table
        row = self._adsb_rows.get(icao)
        if row is None:
            row = t.rowCount()
            t.insertRow(row)
            self._adsb_rows[icao] = row
        cells = [icao, str(d.get("callsign") or "").strip(),
                 str(d.get("emitter_category") or ""),
                 self._fmt(d.get("altitude"), "{:.0f}"),
                 self._fmt(d.get("speed"), "{:.0f}"),
                 self._fmt(d.get("heading"), "{:.0f}"),
                 self._fmt(d.get("latitude"), "{:.4f}"),
                 self._fmt(d.get("longitude"), "{:.4f}"),
                 self._fmt(d.get("num_msgs")), "0"]
        for c, val in enumerate(cells):
            t.setItem(row, c, QtWidgets.QTableWidgetItem(val))
        self._adsb_seen[icao] = QtCore.QDateTime.currentMSecsSinceEpoch() / 1000.0
        # feed the live map (upsert ignores NaN lat/lon until CPR resolves)
        self.targets.upsert(
            icao, "aircraft",
            lat=d.get("latitude"), lon=d.get("longitude"),
            label=(str(d.get("callsign") or "").strip() or icao),
            altitude=d.get("altitude"), speed=d.get("speed"),
            heading=d.get("heading"), num_msgs=d.get("num_msgs"),
            ac_class=d.get("ac_class"), category=d.get("emitter_category"))

    def _adsb_tick(self):
        """Refresh the Age column each second; prune aircraft unseen for 90 s."""
        if self._adsb_win is None:
            return
        now = QtCore.QDateTime.currentMSecsSinceEpoch() / 1000.0
        t = self._adsb_table
        stale = []
        for icao, row in self._adsb_rows.items():
            age = int(now - self._adsb_seen.get(icao, now))
            if age > 90:
                stale.append(icao)
            elif t.item(row, t.columnCount() - 1) is not None:
                t.item(row, t.columnCount() - 1).setText(str(age))
        if stale:
            for r in sorted((self._adsb_rows[i] for i in stale), reverse=True):
                t.removeRow(r)
            for i in stale:
                self._adsb_seen.pop(i, None)
            self._adsb_rows = {t.item(r, 0).text(): r        # rebuild ICAO→row
                               for r in range(t.rowCount()) if t.item(r, 0)}
        if getattr(self, "_adsb_status", None) is not None:
            self._adsb_status.setText(f"{t.rowCount()} aircraft")

    # -- Live map (aircraft + modules' radio positions, QtWebEngine + Leaflet) ---
    def _ensure_map_window(self):
        """Build the map window + QWebEngineView and start loading map.html, but
        do NOT show it. Split out from _show_map_window so it can be called when
        ADS-B mode starts to *pre-warm* the map: spinning up Chromium and paying
        the one-time cold-start + IPv6-fallback tile fetch (see map.html) while
        the window is still hidden, so the first Live-map open is instant with
        tiles already cached. No-op when PyQtWebEngine is absent or already built."""
        if not WEBENGINE_AVAILABLE or self._map_win is not None:
            return
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Live map — aircraft & radios")
        dlg.resize(900, 640)
        lay = QtWidgets.QVBoxLayout(dlg)
        lay.setContentsMargins(0, 0, 0, 0)
        view = QtWebEngineWidgets.QWebEngineView(dlg)
        # Paint the *page* the theme's map backdrop from the start, so it
        # opens straight to the dark (or light) background instead of flashing
        # the QWebEngine default white before the page's CSS has rendered.
        # Use page().setBackgroundColor ONLY — a Qt setStyleSheet() on the
        # QWebEngineView gives it a styled background, which forces QtWebEngine
        # onto its software compositor (laggy pan/zoom + slow tiles, both
        # themes). The remaining wait before the map appears is OSM tile fetch.
        backdrop = "#0b0f14" if self._map_theme == "dark" else "#e8e8e8"
        view.page().setBackgroundColor(QtGui.QColor(backdrop))
        html = os.path.join(os.path.dirname(__file__), "web", "map.html")
        self._map_ready = False
        view.loadFinished.connect(self._on_map_load)
        view.load(QtCore.QUrl.fromLocalFile(html))
        lay.addWidget(view)
        self._map_win, self._map_view = dlg, view
        self._map_timer = QtCore.QTimer(self)
        self._map_timer.timeout.connect(self._map_tick)
        self._map_timer.start(1000)

    def _show_map_window(self):
        """Build (if needed) and show the modeless map window. No-op (menu
        disabled) when PyQtWebEngine is not installed."""
        if not WEBENGINE_AVAILABLE:
            return
        self._ensure_map_window()
        self._map_win.show()
        self._map_win.raise_()

    def _on_map_load(self, ok):
        """Page loaded: mark ready, apply the current theme, and push the
        existing targets at once so aircraft/radios already received *before*
        the map was opened appear immediately (not on the next 1 s tick)."""
        self._map_ready = bool(ok)
        if not ok or self._map_view is None:
            return
        self._map_view.page().runJavaScript(
            f"window.setTheme({json.dumps(self._map_theme)});")
        self._push_map_label_style()      # apply the saved label size/color
        self._map_push()

    def _map_push(self):
        """Send the current positioned-target snapshot to the Leaflet page."""
        if self._map_view is None:
            return
        data = json.dumps(self.targets.snapshot())
        self._map_view.page().runJavaScript(f"window.updateTargets({data});")

    def _map_tick(self):
        """Refresh the map once a second while it's visible and loaded."""
        if (not self._map_ready or self._map_view is None
                or self._map_win is None or not self._map_win.isVisible()):
            return
        self.targets.prune(300)          # forget targets unseen for 5 min
        self._map_push()

    def _close_map_window(self):
        """Destroy the map's QWebEngineView *while the QApplication still exists*.
        A web view left to be garbage-collected during interpreter shutdown tears
        down its render process (an internal QThread) out of order and aborts with
        'QThread: Destroyed while thread is still running'."""
        if self._map_timer is not None:
            self._map_timer.stop()
            self._map_timer = None
        self._map_ready = False
        if self._map_view is not None:
            view = self._map_view
            self._map_view = None             # stop _map_push/_on_map_load re-entry
            try:
                view.loadFinished.disconnect()   # no callbacks during teardown
            except Exception:
                pass
            view.stop()                       # cancel any in-flight page load
            page = view.page()
            if page is not None:
                page.deleteLater()            # release the render process first
            view.setParent(None)
            view.deleteLater()
        if self._map_win is not None:
            self._map_win.close()
            self._map_win.deleteLater()
            self._map_win = None
        # QtWebEngine tears its render process down asynchronously over several
        # event-loop passes; spin the loop (running DeferredDelete each time) so
        # it completes BEFORE the QApplication is destroyed. A single
        # processEvents() isn't enough — the abort then surfaces at exit.
        app = QtWidgets.QApplication.instance()
        if app is not None:
            for _ in range(5):
                app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
                app.processEvents(QtCore.QEventLoop.AllEvents, 50)

    def _on_rds_update(self, d):
        """Update the live RDS station-name readout and log PS/RadioText
        changes (deduped) in the digital decode window. ``d`` is the full
        current merged station-field dict (radio.RdsMsgSink re-sends it
        whenever any field changes), not just the one that changed."""
        if self._rec_labels is not None:
            self._rec_labels.feed_rds(d)
        ps = (d.get("ps") or "").strip()
        if ps != self._rds_ps:
            self._rds_ps = ps
            self._show_freq()           # refresh the tuning-display subtitle
        rt = (d.get("rt") or "").strip()
        key = (ps, rt)
        if key == self._last_rds or not (ps or rt):
            return
        self._last_rds = key
        detail = f'RT "{rt}"' if rt else f'PS "{ps}"'
        self._dig_append(f"{self._decode_station('RDS')}  RDS  {detail}")

    def _on_bandwidth_drag(self, bw):
        """Marker edge dragged: apply the new demod bandwidth + repaint."""
        self.radio.set_bandwidth(int(bw))
        self.spectrum.update()

    def _on_snap_khz_change(self, text):
        try:
            self._snap_khz = float(text)
        except ValueError:
            return
        self._resnap_current()        # realign the tuned freq to the new grid

    def _set_snap_khz(self, khz):
        """Programmatically change the snap grid (e.g. auto-selecting 1 kHz
        on entering SSB, where channels aren't on a fixed spacing) and keep
        the combo box in sync — same effect as picking it from Settings."""
        if self._snap_khz == khz:
            return
        self._snap_khz = float(khz)
        if hasattr(self, "snap_combo"):
            self.snap_combo.blockSignals(True)
            self.snap_combo.setCurrentText(_fmt_khz(khz))
            self.snap_combo.blockSignals(False)
        self._resnap_current()

    def _on_snap_toggle(self, on):
        self._snap_enabled = on
        if on:                        # turning Snap on aligns the current tune
            self._resnap_current()

    def _resnap_current(self):
        """Re-snap the currently tuned frequency onto the channel grid — used when
        Snap is switched on or the grid spacing changes, so the marker and later
        clicks stay aligned without a manual retune. No-op while scanning or when
        Snap is off."""
        if not self._snap_enabled or self._scanning:
            return
        snapped = self._snap(self.radio.demod_hz)
        if snapped != self.radio.demod_hz:
            if not self._scanning:
                self._leave_channel()
            self.radio.set_demod(snapped)
            self._show_freq()

    def _snap(self, hz):
        """Round ``hz`` to the channel grid when Snap is on (so a click lands on
        channel center); pass through otherwise."""
        if not self._snap_enabled:
            return int(hz)
        step = self._snap_khz * 1000.0
        return int(round(hz / step) * step)

    def _on_spectrum_click(self, hz):
        """Click on the waterfall tunes the demod WITHIN the current band (the
        marker moves onto the signal; the band stays centered where it is).
        With Snap on, the click lands on the nearest channel-grid center."""
        if not self._scanning:
            self._leave_channel()
        self.radio.set_demod(self._snap(hz))
        self._show_freq()
        self.bus.emit("tuned", freq_hz=int(self.radio.demod_hz), primary=False, how="click")

    def annotate(self, source, items, trail=True):
        """A module's labeled signals (api.Annotation): drawn on the waterfall, found by the right-click
        menu, and published on the "annotations" topic. Replaces that source's previous set."""
        self.spectrum.set_annotations(source, items, trail=trail)
        self.bus.publish("annotations", source=source, items=list(items))

    def annotation_near(self, freq_hz):
        """The annotation (dict) under / near a frequency, from any source, or None."""
        return self.spectrum.detection_near(freq_hz)

    # =====================================================================
    #  Sources & live filter
    # =====================================================================
    def _on_source_change(self, source):
        p = importers.get(source)
        for name, ws in self._importer_widgets.items():
            for w in ws:
                w.setVisible(name == source)
        self.csv_btn.setVisible(bool(p and p.file_filter))
        self.dl_bar.setVisible(source == "FCC ULS" and self.dl_worker is not None)

    def _dataset_key(self):
        return self.dataset_combo.currentText().split()[0]     # "LMcomm"

    def _choose_source_path(self):
        p = importers.get(self.source_combo.currentText())
        flt = (p.file_filter if p and p.file_filter else "All files (*)")
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, f"Select a file for {p.name if p else 'import'}",
                                                        "", flt)
        if path:
            self._csv_path = path
            self.fetch_status.setText(f"selected: {os.path.basename(path)}")

    # -- live filter ---------------------------------------------------------
    def _on_all_services(self, checked):
        if checked:                             # "All" clears the specific boxes
            for cb in self.filter_cbs.values():
                cb.blockSignals(True)
                cb.setChecked(False)
                cb.blockSignals(False)
        self._apply_filter()

    def _on_service_cb(self, checked):
        if checked and self.filter_all_cb.isChecked():
            self.filter_all_cb.blockSignals(True)
            self.filter_all_cb.setChecked(False)
            self.filter_all_cb.blockSignals(False)
        self._apply_filter()

    def _filter_services(self):
        if self.filter_all_cb.isChecked():
            return []                           # empty => all services
        return [t for t, cb in self.filter_cbs.items() if cb.isChecked()]

    def _on_where_apply(self):
        text = self.loc_edit.text().strip()
        if text == self._applied_loc_text:      # already handled this text
            return                              # (returnPressed + editingFinished)
        self._applied_loc_text = text
        if not text:                            # blank => no radius filter
            self._filter_center = None
            self._filter_label = ""
            self._apply_filter()
            return
        if self.geo_worker is not None:
            return
        self.fetch_status.setText(f"resolving “{text}” …")
        self.geo_worker = GeocodeWorker(text)
        self.geo_worker.done.connect(self._on_geocode_done)
        self.geo_worker.failed.connect(self._on_geocode_failed)
        self.geo_worker.finished.connect(lambda: self._retire("geo_worker"))
        self.geo_worker.start()

    def _on_geocode_done(self, lat, lon, label):
        self._filter_center = (lat, lon)
        self._filter_label = label
        self._apply_filter()

    def _on_geocode_failed(self, msg):
        self._applied_loc_text = None           # allow a retry
        self._filter_center = None              # don't filter against a stale center
        self._filter_label = ""
        self.fetch_status.setText(msg)
        self._apply_filter()

    def _clear_filter(self):
        self.loc_edit.clear()
        self._applied_loc_text = ""
        self._filter_center = None
        self._filter_label = ""
        self.mode_filter.blockSignals(True)
        self.mode_filter.setCurrentText("All")
        self.mode_filter.blockSignals(False)
        self.src_search.blockSignals(True)      # also clear the text search
        self.src_search.clear()
        self.src_search.blockSignals(False)
        self._src_search = ""
        self.filter_all_cb.setChecked(True)     # -> _on_all_services -> refresh
        self._apply_filter()

    # keywords each service checkbox matches in a channel's Service/Tag text,
    # so ticking "Police" catches "Law Dispatch" etc. and Fire ≠ EMS ≠ Police.
    _SERVICE_KEYWORDS = {
        "Police": ("police", "law", "sheriff", "constable", "pd", "trooper"),
        "Fire": ("fire", "fireground"),
        "EMS": ("ems", "ambulance", "medical", "rescue", "cmed"),
        "Public Safety": ("police", "law", "sheriff", "fire", "ems", "ambulance",
                          "emergency", "public safety"),
        "Business": ("business", "industrial"),
        "Ham": ("ham", "amateur"),
        "Aircraft": ("aircraft", "tower", "aviation"),
        "Schools": ("school", "university", "college", "campus"),
    }

    def _service_match(self, chan_service, ticked):
        if not ticked:
            return True
        s = (chan_service or "").lower()
        for lbl in ticked:
            for kw in self._SERVICE_KEYWORDS.get(lbl, (lbl.lower(),)):
                if kw in s:
                    return True
        return False

    def _passes_filter(self, ch):
        """The Memories view's mode / service / radius / text filter. It narrows
        only the *view* of the active bank — never what a scan iterates (see
        _scan_channels)."""
        mode_sel = self.mode_filter.currentText()
        if mode_sel != "All" and ch.mode != mode_sel:
            return False
        if not self._service_match(ch.service, self._filter_services()):
            return False
        if self._src_search:
            hay = " ".join((ch.label or "", ch.service or "",
                            getattr(ch, "city", "") or "",
                            getattr(ch, "notes", "") or "",
                            f"{ch.freq/1e6:.4f}")).lower()
            if self._src_search not in hay:
                return False
        if self._filter_center is not None:
            rkm = self.radius_spin.value() * 1.60934
            if ch.lat is not None and ch.lon is not None:
                if haversine_km(self._filter_center[0], self._filter_center[1],
                                ch.lat, ch.lon) > rkm:
                    return False
            elif not self.nogps_cb.isChecked():
                return False
        return True

    def _shown_channels(self):
        """The active bank after the Memories view's mode/service/radius/text
        filter. Drives the merged table only — scanning iterates the full bank
        (see _scan_channels), so filtering the view never restricts a scan."""
        return [(i, ch) for i, ch in enumerate(self.store.bank().channels)
                if self._passes_filter(ch)]

    def _apply_filter(self):
        self._refresh_table()

    def _known_modes(self, with_adsb=False):
        """Channel modes to offer: the core's analog modes, modules' modes and the labels modules decode
        (request_decode), then any other mode a bank holds (imported data names modes no module may
        handle; they are kept as they are)."""
        out = list(ANALOG_MODES) + (["ADS-B"] if with_adsb else []) + list(getattr(self, "_mode_specs", {}))
        for lab in self.decodable_labels():
            if lab not in out:
                out.append(lab)
        store = getattr(self, "store", None)
        for bank in (store.banks.values() if store is not None else ()):
            for ch in bank.channels:
                if ch.mode and ch.mode not in out:
                    out.append(ch.mode)
        return out

    def _refresh_mode_filter(self):
        """The Mode filter's entries follow the banks and the loaded modules; the choice is kept."""
        if not hasattr(self, "mode_filter"):
            return
        cur = self.mode_filter.currentText()
        items = ["All"] + self._known_modes()
        if [self.mode_filter.itemText(i) for i in range(self.mode_filter.count())] == items:
            return
        self.mode_filter.blockSignals(True)
        self.mode_filter.clear()
        self.mode_filter.addItems(items)
        self.mode_filter.setCurrentText(cur if cur in items else "All")
        self.mode_filter.blockSignals(False)

    def _update_filter_status(self, shown, total):
        bits = []
        if self._filter_center is not None:
            where = self._filter_label.split(",")[0] or "location"
            bits.append(f"{self.radius_spin.value()} mi of {where}")
        svcs = self._filter_services()
        if svcs:
            bits.append("/".join(svcs))
        desc = ("  ·  ".join(bits)) if bits else "no filter"
        self.fetch_status.setText(f"showing {shown} / {total}   [{desc}]")

    # -- import a full dataset into a bank -----------------------------------
    def _where_from_station(self):
        loc = station.location()
        if loc is None:
            self.fetch_status.setText("no station location yet (Settings ▸ Station location…)")
            return
        self.loc_edit.setText(f"{loc.lat:.5f},{loc.lon:.5f}")
        self._on_where_apply()

    def _open_station_settings(self):
        """Where the receiver is (api.station): lat / lon / altitude, or a place to look up. Modules use it
        for distances, bearings, satellite passes."""
        cur = station.location()
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Station location")
        form = QtWidgets.QFormLayout(dlg)
        place = QtWidgets.QLineEdit(cur.name if cur else "")
        place.setPlaceholderText("zip · city, ST · lat,lon")
        find = QtWidgets.QPushButton("Look up")
        row = QtWidgets.QHBoxLayout()
        row.addWidget(place, 1)
        row.addWidget(find)
        form.addRow("Place:", row)
        lat, lon = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
        for w, lim in ((lat, 90), (lon, 180)):
            w.setRange(-lim, lim)
            w.setDecimals(5)
        alt = QtWidgets.QDoubleSpinBox()
        alt.setRange(-500, 9000)
        alt.setSuffix(" m")
        if cur:
            lat.setValue(cur.lat)
            lon.setValue(cur.lon)
            alt.setValue(cur.alt_m)
        form.addRow("Latitude:", lat)
        form.addRow("Longitude:", lon)
        form.addRow("Altitude:", alt)
        msg = QtWidgets.QLabel("")
        msg.setStyleSheet("color:#8b95a1;")
        form.addRow(msg)

        def _look_up():
            text = place.text().strip()
            if not text:
                return
            msg.setText(f"looking up “{text}” …")
            w = GeocodeWorker(text, dlg)
            w.done.connect(lambda la, lo, lab: (lat.setValue(la), lon.setValue(lo), msg.setText(lab)))
            w.failed.connect(msg.setText)
            dlg._geo = w
            w.start()

        find.clicked.connect(_look_up)
        place.returnPressed.connect(_look_up)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel
                                        | QtWidgets.QDialogButtonBox.Reset)
        bb.button(QtWidgets.QDialogButtonBox.Reset).setText("Clear")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        bb.button(QtWidgets.QDialogButtonBox.Reset).clicked.connect(lambda: dlg.done(2))
        form.addRow(bb)
        r = dlg.exec_()
        if getattr(dlg, "_geo", None) is not None:
            dlg._geo.wait(10000)
        if r == 2:
            station.set_location(None, None)
            self.statusBar().showMessage("station location cleared", 4000)
        elif r == QtWidgets.QDialog.Accepted:
            station.set_location(lat.value(), lon.value(), alt.value(), place.text().strip())
            self.statusBar().showMessage(f"station at {lat.value():.4f}, {lon.value():.4f}", 4000)

    def _open_rr_account(self):
        """RadioReference Premium account: kept in the keystore (api.secrets), not in a config file."""
        cur = rr.creds() or {}
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("RadioReference account")
        form = QtWidgets.QFormLayout(dlg)
        note = QtWidgets.QLabel("Only for a paid RadioReference Premium subscription (their official API). "
                                "Stored in the local keystore (secrets.json, readable only by you). Leave "
                                "both blank to forget it.")
        note.setWordWrap(True)
        form.addRow(note)
        user = QtWidgets.QLineEdit(cur.get("username", ""))
        pw = QtWidgets.QLineEdit(cur.get("password", ""))
        pw.setEchoMode(QtWidgets.QLineEdit.Password)
        key = QtWidgets.QLineEdit(cur.get("app_key", ""))
        form.addRow("Username:", user)
        form.addRow("Password:", pw)
        form.addRow("App key (optional):", key)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        rr.set_creds(user.text().strip(), pw.text(), key.text().strip())
        self._refresh_importers()
        self.statusBar().showMessage("RadioReference account " + ("saved" if rr.available() else "removed"), 5000)

    def _import_source(self):
        """Run the picked import provider (api.importers) off the UI thread; its channels become a bank."""
        p = importers.get(self.source_combo.currentText())
        if p is None or self.fetch_worker is not None:
            return
        ok, why = p.check()
        if not ok:
            self.fetch_status.setText(f"{p.name}: {why}")
            return
        if p.file_filter and not self._csv_path:
            self.fetch_status.setText("choose a file first")
            return
        loc = self.loc_edit.text().strip()
        if p.needs_location and not loc:
            self.fetch_status.setText(f"enter a Where location to import {p.name}")
            return
        self.import_btn.setEnabled(False)
        self.fetch_status.setText(f"importing {p.name}" + (f" near {loc}" if p.needs_location else "") + " …")
        # a file import keeps every row (no Where filter); a regional import is unfiltered by service (services
        # are a live filter) over a generous region the live radius filter can tighten afterwards
        self.fetch_worker = FetchWorker(loc if p.needs_location else "", p,
                                        self._csv_path if p.file_filter else "",
                                        self.radius_spin.value() * 1.60934,
                                        [], keep_unlocated=True)
        self.fetch_worker.done.connect(self._on_fetch_done)
        self.fetch_worker.failed.connect(self._on_fetch_failed)
        self.fetch_worker.finished.connect(lambda: self._retire("fetch_worker"))
        self.fetch_worker.start()

    def _channels_to_bank(self, found, label, activate=True):
        if not found:
            self.fetch_status.setText(f"no channels imported ({label})")
            return 0
        name = self._unique_bank_name(label)
        self.store.create_bank(name)
        seen = set()
        for fc in found:
            ch = fc.to_channel() if hasattr(fc, "to_channel") else fc
            key = (ch.freq, ch.mode)
            if key in seen:
                continue
            seen.add(key)
            self.store.add_channel(ch, name)
        self.store.save()
        if activate:                          # make the import the active/edited
            self.store.set_active(name)       # bank so it shows in the table
        self._refresh_bank_combo()
        self._refresh_table()
        self.fetch_status.setText(f"imported {len(seen)} → bank “{name}”")
        return len(seen)

    def _export_filtered(self):
        vis = self._shown_channels()
        if not vis:
            self.fetch_status.setText("nothing to export (filter shows 0)")
            return
        # suggested new-bank name from the current filter
        default = self._filter_label.split(",")[0].strip() if self._filter_label \
            else (self.store.active or "Data set")
        if self._filter_center is not None:
            default = f"{default} {self.radius_spin.value()}mi"
        default = default[:40]
        self._copy_channels_dialog([ch for _i, ch in vis], "Export all shown → bank",
                                   default)

    def _copy_channels_dialog(self, channels, title, default_name):
        """Shared 'copy these channels into a new/existing bank' flow (dedup by
        freq+mode). Used by Export-all-shown and Add-checked."""
        if not channels:
            self.fetch_status.setText("nothing to copy")
            return
        items = [default_name] + [b for b in self.store.banks if b != default_name]
        name, ok = QtWidgets.QInputDialog.getItem(
            self, title,
            "New bank name, or pick an existing bank to add these channels to:",
            items, 0, True)
        if not ok or not name.strip():
            return
        name = name.strip()
        appending = name in self.store.banks
        bank = self.store.create_bank(name)      # returns the existing bank if any
        seen = {(c.freq, c.mode) for c in bank.channels}   # de-dup vs what's there
        added = 0
        for ch in channels:
            key = (ch.freq, ch.mode)
            if key in seen:
                continue
            seen.add(key)
            bank.add(dataclasses.replace(ch))    # copy so banks stay independent
            added += 1
        self.store.save()
        self._refresh_bank_combo()
        verb = "added to" if appending else "created"
        self.fetch_status.setText(
            f"{verb} bank “{name}”: +{added} channels "
            f"({len(bank.channels)} total). Pick it as the active bank to scan it.")

    # -- FCC download ---------------------------------------------------------
    def _reap(self, w):
        """Retire a download worker once its thread has actually finished."""
        if self.dl_worker is w:
            self.dl_worker = None
        if w in self._reaping:
            self._reaping.remove(w)
        w.deleteLater()

    def _toggle_download(self):
        if self.dl_worker is not None:            # running -> cancel + detach
            w = self.dl_worker
            w.cancel()
            # Free the UI immediately; the thread may still be blocked in a slow
            # connect/read and will exit on its own once it unblocks. Keep a
            # reference in _reaping so it isn't destroyed while still running.
            self._reaping.append(w)
            self.dl_worker = None
            self._end_download()
            self.fetch_status.setText("download canceled")
            return
        self._dl_dataset = self._dataset_key()
        self.dl_bar.setRange(0, 100)
        self.dl_bar.setValue(0)
        self.dl_bar.setVisible(True)
        self.download_btn.setText("Cancel")
        self.dataset_combo.setEnabled(False)
        self.fetch_status.setText("starting download…")
        self.dl_worker = DownloadWorker(self._dl_dataset, _uls_dir())
        self.dl_worker.progress.connect(self._on_dl_progress)
        self.dl_worker.done.connect(self._on_dl_done)
        self.dl_worker.failed.connect(self._on_dl_failed)
        self.dl_worker.finished.connect(lambda w=self.dl_worker: self._reap(w))
        self.dl_worker.start()

    def _on_dl_progress(self, pct, got_mb, total_mb):
        if total_mb > 0:
            if self.dl_bar.maximum() == 0:
                self.dl_bar.setRange(0, 100)
            self.dl_bar.setValue(pct)
            self.fetch_status.setText(
                f"downloading {self._dl_dataset}: "
                f"{got_mb:.1f}/{total_mb:.0f} MB ({pct}%)")
        else:                                     # unknown size -> busy bar
            self.dl_bar.setRange(0, 0)
            self.fetch_status.setText(
                f"downloading {self._dl_dataset}: {got_mb:.1f} MB…")

    def _end_download(self):
        self.download_btn.setText("Download FCC data")
        self.download_btn.setEnabled(True)
        self.dataset_combo.setEnabled(True)
        self.dl_bar.setRange(0, 100)
        self.dl_bar.setValue(0)
        self.dl_bar.setVisible(False)

    def _on_dl_done(self, _dir):
        self._end_download()
        # the download only stores the data: open the search over it (which
        # builds its index); Import → bank still copies a region into a bank
        self.fetch_status.setText(
            f"FCC {self._dl_dataset} data ready — search it in the FCC license "
            "search window, or Import → bank with a Where location")
        self._show_uls_window()

    # -- FCC license search (ulsui.UlsSearchWindow) ---------------------------
    def _show_uls_window(self):
        if self._uls_win is None:
            from .ulsui import UlsSearchWindow
            self._uls_win = UlsSearchWindow(self, _uls_dir())
            self._uls_win.setWindowIcon(self.windowIcon())
        if self._uls_win.isVisible():
            self._uls_win.refresh_data()      # a new download: rebuild the index
        self._uls_win.show()
        self._uls_win.raise_()
        self._uls_win.activateWindow()

    def _on_dl_failed(self, msg):
        self._end_download()
        self.fetch_status.setText(msg)

    def _retire(self, attr):
        """Safely drop a finished QThread. MUST be driven by the thread's
        built-in finished signal (fired after run() returns) — dropping the
        reference from a slot the thread emits mid-run() destroys the QThread
        while it is still running and aborts the process."""
        w = getattr(self, attr, None)
        if w is not None:
            w.deleteLater()
            setattr(self, attr, None)

    def _on_fetch_failed(self, msg):
        self.fetch_status.setText(f"import failed — {msg}")
        self.import_btn.setEnabled(True)

    def _on_fetch_done(self, found, label):
        self.import_btn.setEnabled(True)
        self._channels_to_bank(found, label, activate=True)

    def _unique_bank_name(self, label):
        base = (label.split(",")[0].strip() or "Location")[:24]
        name = base
        i = 2
        while name in self.store.banks and self.store.bank(name).channels:
            name = f"{base} {i}"
            i += 1
        return name

    def _update_signal(self):
        """Poll channel power at the tuned freq; drive BOTH the SIG readout and
        the audio squelch gate from the same measurement, so the squelch knob
        acts in the units shown on the meter.

        Runs while idle AND while HOLDING on a channel during a scan (a parked
        state) — so the squelch keeps gating through the hang time and you don't
        hear static once the transmission ends. Only the active SWEEP skips it
        (the worker owns retuning there, and audio is muted anyway)."""
        if self._scanning and not self._scan_holding:
            return                      # sweeping: worker owns tuning + gate
        sig, floor = self.radio.channel_power(self.radio.freq_hz,
                                               self.radio.current_bw())
        open_ = self.radio.squelch_open(sig, floor)
        # ALWAYS decide the analog gate here (don't cede it to a decoder's claim —
        # that could leave it stuck open = static the squelch can't touch).
        # Analog is audible only when squelch is open AND no decoder holds the
        # speaker (api.AudioArbiter claims) and analog isn't switched off.
        analog_on = open_ and self.audio.analog_allowed()
        # a module that asked for analog off (api.AudioArbiter.mute_analog, e.g. a data mode's
        # warble) is honored only while Auto-mute analog is on
        if not self._auto_mute and self.audio._muted_by and not self.audio.claimed():
            analog_on = open_ and not self.audio.analog_off
        # CTCSS tone squelch: an extra gate on top of the power squelch above —
        # only unmute when the selected tone is present too (a no-op outside
        # NFM, or when the RX-Sq checkbox is off).
        if self._ctcss_rx:
            analog_on = analog_on and self.radio.ctcss_unmuted()
        self.radio.set_gate(analog_on)
        color = "#39ff88" if open_ else "#5a636e"
        state = "OPEN" if open_ else "muted"
        if self.audio.claimed():
            state = self.audio.label() or "DECODER"
        tag = f" auto+{int(self.radio._auto_margin)}" if self.radio._auto_squelch \
            else ""
        tone_tag = ""
        if open_:
            tones = self.radio.detected_ctcss()   # blind decode; [] outside NFM/no tone
            if tones:
                tone_tag = f"  Tone {tones[0]:g}"
        self.sig_label.setText(f"SIG {sig:4.0f}/{floor:4.0f}dB {state}{tag}{tone_tag}")
        self.sig_label.setStyleSheet(f"color:{color};")

    # =====================================================================
    #  Banks
    # =====================================================================
    def _fill_bank_combo(self, combo, selected=None):
        combo.blockSignals(True)
        combo.clear()
        for name, bank in self.store.banks.items():
            # show the channel count so the full imported source is obvious
            combo.addItem(f"{name}  ({len(bank.channels)})", name)
        idx = combo.findData(selected if selected is not None else self.store.active)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.blockSignals(False)

    def _refresh_bank_combo(self):
        self._fill_bank_combo(self.bank_combo, self.store.active)
        # keep the main-UI scan-bank multi-select in step (exclude ★ Priority —
        # it's scanned via the Priority feature, not the normal bank set)
        if hasattr(self, "scan_bank_combo"):
            names = [n for n in self.store.banks if n != PRIORITY_BANK]
            self._scan_banks = [b for b in self._scan_banks if b in names]
            self.scan_bank_combo.set_items(names, self._scan_banks)

    def _on_scan_banks_change(self):
        self._scan_banks = self.scan_bank_combo.checked_items()

    def _scan_bank_names(self):
        """The bank(s) a scan covers: the ticked set, or the active bank when
        nothing is ticked (so ▶ Scan always does something sensible)."""
        names = [n for n in self._scan_banks if n in self.store.banks]
        if not names and self.store.active in self.store.banks:
            names = [self.store.active]
        return names

    def _on_bank_change(self, _idx):
        # the Memories Active selector: the bank being edited
        name = self.bank_combo.currentData()
        if name and name != self.store.active:
            self.store.set_active(name)
            self._refresh_bank_combo()
            self._refresh_table()

    def _new_bank(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "New bank", "Name:")
        if ok and name.strip():
            self.store.create_bank(name.strip())
            self.store.set_active(name.strip())
            self._refresh_bank_combo()
            self._refresh_table()

    def _delete_bank(self):
        name = self.store.active
        if name == PRIORITY_BANK:
            self.scan_status.setText("the ★ Priority bank can't be deleted")
            return
        if len(self.store.banks) <= 1:
            self.scan_status.setText("cannot delete the only bank")
            return
        self.store.delete_bank(name)
        self._refresh_bank_combo()
        self._refresh_table()

    # -- remove individual channels ------------------------------------------
    def _remove_channels(self, bank_name, indices):
        """Delete the given channel indices from ``bank_name`` (indices into that
        bank's channel list), persist, and refresh the table + the markers."""
        if bank_name not in self.store.banks or not indices:
            return 0
        bank = self.store.bank(bank_name)
        for i in sorted(set(indices), reverse=True):   # high→low so indices hold
            if 0 <= i < len(bank.channels):
                del bank.channels[i]
        self.store.save()
        self._refresh_bank_combo()
        self._refresh_table()
        self._update_markers()
        return len(set(indices))

    def _selected_bank_indices(self, table, row_bankidx):
        rows = {mi.row() for mi in table.selectionModel().selectedRows()}
        return [row_bankidx[r] for r in sorted(rows)
                if 0 <= r < len(row_bankidx)]

    def _confirm_remove(self, n, bank_name):
        return QtWidgets.QMessageBox.question(
            self, "Remove channels",
            f"Remove {n} channel(s) from “{bank_name}”? This can't be undone.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No) == QtWidgets.QMessageBox.Yes

    def _remove_selected_scan(self):
        if self._scanning:
            self.scan_status.setText("stop the scan before removing channels")
            return
        idxs = self._selected_bank_indices(self.table, self._row_bankidx)
        if not idxs:
            self.scan_status.setText("select channel row(s) to remove")
            return
        if not self._confirm_remove(len(idxs), self.store.active):
            return
        n = self._remove_channels(self.store.active, idxs)
        self.scan_status.setText(f"removed {n} channel(s) from “{self.store.active}”")

    # -- edit an existing channel's metadata ---------------------------------
    def _edit_selected_channel(self):
        idxs = self._selected_bank_indices(self.table, self._row_bankidx)
        if not idxs:
            self.scan_status.setText("select a channel row to edit")
            return
        self._edit_channel(self.store.active, idxs[0])

    def _edit_channel(self, bank_name, index):
        """Edit an existing channel's frequency / mode / name / service / tone /
        notes in place — for correcting a mistuned entry or filling in what
        you've learned about a channel over time."""
        bank = self.store.bank(bank_name) if bank_name else None
        if not bank or not (0 <= index < len(bank.channels)):
            return
        ch = bank.channels[index]
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Edit memory")
        form = QtWidgets.QFormLayout(dlg)

        freq_edit = QtWidgets.QLineEdit(f"{ch.freq/1e6:.6f}")
        form.addRow("Frequency (MHz):", freq_edit)

        mode_combo = QtWidgets.QComboBox()
        mode_combo.addItems(self._known_modes(with_adsb=True))
        if mode_combo.findText(ch.mode) < 0:
            mode_combo.addItem(ch.mode)   # keep an unrecognized/imported mode selectable
        mode_combo.setCurrentText(ch.mode)
        form.addRow("Mode:", mode_combo)

        name_edit = QtWidgets.QLineEdit(ch.label)
        form.addRow("Name:", name_edit)

        svc_combo = QtWidgets.QComboBox()
        svc_combo.setEditable(True)
        svc_combo.addItem("")
        svc_combo.addItems(SERVICE_TAGS)
        svc_combo.setCurrentText(ch.service)
        form.addRow("Service:", svc_combo)

        tone_edit = QtWidgets.QLineEdit(ch.tone)
        tone_edit.setPlaceholderText(
            "131.8 PL · 023 DPL · 307 NAC · CC 6 TG 9   (optional)")
        form.addRow("Tone/ID:", tone_edit)

        notes_edit = QtWidgets.QPlainTextEdit(ch.notes)
        notes_edit.setFixedHeight(56)
        form.addRow("Notes:", notes_edit)

        for title, text in self._module_channel_summary(ch):   # module fields: read-only here
            lab = QtWidgets.QLabel(text)
            lab.setWordWrap(True)
            lab.setStyleSheet("color:#8b95a1;")
            lab.setToolTip("Stored by the module when the channel was saved; change it in the "
                           "module's own controls, then save the channel again.")
            form.addRow(f"{title}:", lab)

        err = QtWidgets.QLabel("")
        err.setStyleSheet("color:#ff6b6b;")
        form.addRow(err)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Save | QtWidgets.QDialogButtonBox.Cancel)

        def _try_accept():
            try:
                freq_hz = int(round(float(freq_edit.text().strip()) * 1e6))
            except ValueError:
                err.setText("Frequency must be a number (MHz)")
                return
            if freq_hz <= 0:
                err.setText("Frequency must be positive")
                return
            dlg._freq_hz = freq_hz
            dlg.accept()

        buttons.accepted.connect(_try_accept)
        buttons.rejected.connect(dlg.reject)
        form.addRow(buttons)
        name_edit.setFocus()
        name_edit.selectAll()

        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        self._commit_channel_edit(ch, dlg._freq_hz, mode_combo.currentText(),
                                   name_edit.text().strip(),
                                   svc_combo.currentText().strip(),
                                   tone_edit.text().strip(),
                                   notes_edit.toPlainText().strip())

    def _module_channel_fields(self, ch):
        """Every module's fields for a channel being saved now (Module.channel_fields) -> ch.ext."""
        for m in self.modules.loaded:
            f = self._call_module(m, "channel fields", m.channel_fields, ch)
            if f:
                ch.ext[m.name] = dict(f)

    def _module_channel_summary(self, ch):
        """[(module title, text)] for the modules with fields on ``ch`` (Edit memory shows them)."""
        out = []
        by_name = {m.name: m for m in self.modules.loaded}
        for name, fields in sorted((ch.ext or {}).items()):
            m = by_name.get(name)
            text = (self._call_module(m, "channel summary", m.channel_summary, dict(fields)) if m else None)
            if text is None:                 # module not loaded: the stored fields as they are
                text = ", ".join(f"{k} {v}" for k, v in dict(fields).items())
            out.append(((m.title or m.name) if m else f"{name} (not loaded)", str(text)))
        return out

    def _notify_channel_recalled(self, ch):
        """A bank channel was tuned: each loaded module gets its stored fields (channel_recalled)."""
        self._recalled_channel = ch
        for m in self.modules.loaded:
            self._call_module(m, "channel recalled", m.channel_recalled, ch, ch.fields_for(m.name))

    def channel_fields(self, owner):
        """The fields module ``owner`` stored on the channel recalled last ({} if none)."""
        ch = getattr(self, "_recalled_channel", None)
        return ch.fields_for(owner) if ch is not None else {}

    def set_channel_fields(self, owner, fields):
        """Replace module ``owner``'s fields on the channel recalled last (and save the banks). False if
        no channel was recalled since the last manual tune."""
        ch = getattr(self, "_recalled_channel", None)
        if ch is None:
            return False
        if fields:
            ch.ext[owner] = dict(fields)
        else:
            ch.ext.pop(owner, None)
        self.store.save()
        return True

    def _commit_channel_edit(self, ch, freq, mode, label, service, tone, notes):
        """Apply the edited fields to ``ch`` (already the live object inside a
        bank's channel list), persist, and refresh the views. Factored out of
        the dialog so it's unit-testable."""
        ch.freq, ch.mode = freq, mode
        ch.label, ch.service, ch.tone, ch.notes = label, service, tone, notes
        self.store.save()
        self._refresh_table()
        self._refresh_bank_combo()
        self._update_markers()
        self.scan_status.setText(
            f"updated “{ch.label or f'{ch.freq/1e6:.4f} MHz'}”")

    def _add_current(self):
        # A module transmit mode's own ID code rides in the tone field, the same slot
        # RadioReference-style imports use -- offered back to it (apply_tone) when the
        # channel is recalled. Blank otherwise (CTCSS has its own fields regardless).
        tone = ""
        spec = self._tx_spec()
        if spec is not None and spec.channel_tone is not None:
            tone = str(self._call_module(self._tx_specs[spec.name][0], "channel tone", spec.channel_tone) or "")
        ch = Channel(freq=self.radio.freq_hz, mode=self.radio.mode.name,
                     source="manual", offset_hz=self._current_offset_hz(),
                     ctcss_hz=self._ctcss_hz, ctcss_tx=self._ctcss_tx,
                     ctcss_rx=self._ctcss_rx, tone=tone)
        self._module_channel_fields(ch)
        self.store.add_channel(ch)
        self._refresh_table()
        self._refresh_bank_combo()      # keep the count in the dropdown current

    def _save(self):
        self.store.save()
        self.scan_status.setText(f"saved → {_memory_path()}")

    # =====================================================================
    #  Table
    # =====================================================================
    def _refresh_table(self):
        """The merged Memories table: the active bank after the view filter
        (col-0 checkbox = scan-enable). Filtering narrows only the view."""
        self._refresh_mode_filter()
        vis = self._shown_channels()            # (bank_index, channel) — filtered
        self._row_bankidx = [i for i, _ in vis]
        self._suppress_row_tune = True          # rebuild shouldn't auto-tune
        try:
            self._fill_channel_table(self.table, vis, lambda ch: ch.enabled)
        finally:
            self._suppress_row_tune = False
        self._update_markers()               # refresh the waterfall name overlay
        self._sync_header_check()            # reflect the rows' aggregate tick
        if hasattr(self, "fetch_status"):    # "showing N / total  [filter]" line
            self._update_filter_status(len(vis), len(self.store.bank().channels))

    def _sync_enabled_from_table(self):
        # INVARIANT: only rows present in the (possibly filtered) view are
        # touched, via _row_bankidx. Channels filtered OUT of the view keep their
        # enabled flag, so filtering the table never disables hidden channels and
        # a scan (which iterates the full bank) is unaffected. Do not "optimize"
        # this into iterating self.table's rows without the _row_bankidx mapping.
        bank = self.store.bank()
        for r, i in enumerate(self._row_bankidx):
            item = self.table.item(r, 0)
            if item is not None and 0 <= i < len(bank.channels):
                bank.channels[i].enabled = item.checkState() == QtCore.Qt.Checked

    def _sync_header_check(self):
        """Set the header check-all box on only when every visible row is ticked."""
        n = self.table.rowCount()
        all_on = n > 0 and all(
            (self.table.item(r, 0) is not None
             and self.table.item(r, 0).checkState() == QtCore.Qt.Checked)
            for r in range(n))
        self._table_header.setChecked(all_on)

    def _on_check_all(self, checked):
        """Header checkbox: tick / untick every row currently shown."""
        state = QtCore.Qt.Checked if checked else QtCore.Qt.Unchecked
        self._suppress_row_tune = True       # bulk change: no per-row side effects
        try:
            for r in range(self.table.rowCount()):
                it = self.table.item(r, 0)
                if it is not None:
                    it.setCheckState(state)
        finally:
            self._suppress_row_tune = False
        self._table_header.setChecked(checked)   # keep the box in step
        self._sync_enabled_from_table()
        self._update_markers()

    def _on_table_item_changed(self, item):
        """A row's include-tick changed by hand — keep the header box in sync."""
        if self._suppress_row_tune or item.column() != 0:
            return
        self._sync_header_check()

    def _on_src_search_change(self, text):
        self._src_search = text.strip().lower()
        self._refresh_table()

    def _add_checked_to_bank(self):
        """Copy the checked (☑) channels shown in the merged table into a
        new/existing bank — the quick way to assemble a small 'Test' bank of
        known-busy channels."""
        picks = []
        bank = self.store.bank()
        for r, i in enumerate(self._row_bankidx):
            it = self.table.item(r, 0)
            if it is not None and it.checkState() == QtCore.Qt.Checked \
                    and 0 <= i < len(bank.channels):
                picks.append(bank.channels[i])
        if not picks:
            self.fetch_status.setText("no checked channels to add "
                                      "(tick rows, or the header box)")
            return
        self._copy_channels_dialog(picks, "Add checked → bank", "Test")

    def _row_for_index(self, idx):
        try:
            return self._row_bankidx.index(idx)
        except ValueError:
            return -1

    def _on_current_cell(self, cur_row, cur_col, prev_row, prev_col):
        # tune to the newly-selected row (keyboard arrows or a click). Ignored
        # for programmatic changes (table rebuild) and while a scan drives the
        # selection itself.
        if self._suppress_row_tune or self._scanning:
            return
        if cur_row < 0 or cur_row == prev_row:
            return                   # column-only move -> don't retune
        self._tune_row(cur_row)

    def _tune_row(self, row):
        bank = self.store.bank()
        if 0 <= row < len(self._row_bankidx):
            ch = bank.channels[self._row_bankidx[row]]
            self._user_tunes()
            if not self._scanning:
                self._leave_channel()
            self._apply_channel_mode(ch.mode, ch.freq)
            self._set_freq(ch.freq)
            self._apply_offset(ch.offset_hz)
            self._apply_ctcss(ch.ctcss_hz, ch.ctcss_tx, ch.ctcss_rx)
            self._apply_tx_tone(ch.tone)
            self._notify_channel_recalled(ch)

    # =====================================================================
    #  Channel scan
    # =====================================================================
    def _scan_channels(self):
        """Build the flat scan list across the selected bank(s) — every enabled
        channel in each ticked bank (full bank, not the filtered table view). The
        worker gets (position, channel) tuples and echoes the position back, so
        _scan_list maps a reported index to its (bank, channel) regardless of how
        many banks are combined."""
        self._sync_enabled_from_table()      # commit live edits to the shown bank
        self._scan_list = []
        for bn in self._scan_bank_names():
            for ch in self.store.bank(bn).channels:
                if ch.enabled:
                    self._scan_list.append((bn, ch))
        return [(pos, ch) for pos, (_bn, ch) in enumerate(self._scan_list)]

    def _toggle_scan(self):
        if self._scanning:
            self._stop_scan()
            return
        channels = self._scan_channels()
        self._prio_enabled = self.prio_cb.isChecked()
        self._prio_interval = self.prio_spin.value()
        prio_chans = list(self.store.priority().channels) if self._prio_enabled \
            else []
        if not channels and not prio_chans:
            self.scan_status.setText(
                "no channels match the filter (and no priority channels)")
            return
        if not self.tuner.acquire("scan", TUNE_SCAN, "scanning channels",
                                  on_preempt=lambda by: self._stop_scan()):
            self.scan_status.setText("can't scan: " + self.tuner.may_tune("scan")[1])
            return
        if self._rec_path is not None:        # can't record a moving band
            self._stop_record()
        self.rec_btn.setEnabled(False)
        self._scanning = True
        self._update_ptt_enabled()
        self._scan_holding = False
        self.scan_btn.setText("■ Stop")
        self._apply_waterfall_state()         # pause unless 'Show during scan'
        self.radio.mute()
        self._scan_state("sweep")
        self.table.clearSelection()          # list stays static while sweeping
        self._show_scanning(True)            # LCD -> "SCANNING …"
        self.scan_worker = ChannelScanWorker(
            self.radio, channels,
            dwell_ms=self.dwell_spin.value(),
            hang_ms=int(self.hang_spin.value() * 1000),
            priority=prio_chans,
            priority_ms=int(self._prio_interval * 1000))
        self.scan_worker.active.connect(self._on_scan_active)
        self.scan_worker.checked.connect(self._on_scan_checked)
        self.scan_worker.resumed.connect(self._on_scan_resumed)
        self.scan_worker.priority_hit.connect(self._on_priority_hit)
        self.scan_worker.finished_scan.connect(self._on_scan_finished)
        self.scan_worker.finished.connect(lambda: self._retire("scan_worker"))
        self.scan_worker.start()
        pmsg = f" + {len(prio_chans)} priority" if prio_chans else ""
        banks = self._scan_bank_names()
        bmsg = f" across {len(banks)} banks" if len(banks) > 1 else ""
        self.scan_status.setText(
            f"scanning {len(channels)} channels{bmsg}{pmsg}…")

    def _stop_scan(self):
        if self.scan_worker:
            self.scan_worker.stop()

    def _scan_entry(self, idx):
        """(bank_name, Channel) for a worker-reported scan index, or None."""
        if 0 <= idx < len(self._scan_list):
            return self._scan_list[idx]
        return None

    def _highlight_scan_row(self, bank_name, ch):
        """Select the scanned channel's row IF it's in the bank the Memories table
        is currently showing (multi-bank: other banks aren't on screen)."""
        self.table.clearSelection()
        if bank_name != self.store.active:
            return
        try:
            bidx = self.store.bank(bank_name).channels.index(ch)
        except ValueError:
            return
        row = self._row_for_index(bidx)
        if row >= 0:
            self.table.selectRow(row)

    def _on_scan_checked(self, idx, sig, floor):
        # live feedback in the SCANNING banner: which channel # / freq is being
        # measured, plus the sig/floor it read — so a stop on a "dead" channel is
        # diagnosable (you can see whether the measured level was really high).
        entry = self._scan_entry(idx)
        if entry is None:
            return
        bn, ch = entry
        num = idx + 1
        total = len(self._scan_list)         # scan covers the full selected set
        lbl = (ch.label or "")[:18]
        multi = len(self._scan_bank_names()) > 1
        tag = f"  [{bn[:12]}]" if multi else ""     # show which bank in a multi-scan
        self.scan_banner.setText(
            f"<span style='font-size:24px;'>◦ SCAN {ch.freq/1e6:.4f}</span>"
            f"<br><span style='font-size:12px;'>ch {num}/{total}"
            f"{('  ' + lbl) if lbl else ''}{tag}"
            f"  ·  {sig:.0f}/{floor:.0f} dB</span>")

    def _set_active_banner(self, freq, subtitle, snr, prefix, color):
        """Two-line rich-text banner shown when the scan stops on a channel
        (same look as the sweeping banner)."""
        sub = (subtitle or "")[:26]
        self.scan_banner.setText(
            f"<span style='font-size:24px; color:{color};'>{prefix} "
            f"{freq/1e6:.4f}</span><br>"
            f"<span style='font-size:12px; color:#d6dae0;'>{sub}"
            f"  +{snr:.1f} dB</span>")
        self._show_scanning(True)            # show the banner page

    def _on_scan_active(self, idx, snr):
        entry = self._scan_entry(idx)
        if entry is None:
            return
        bn, ch = entry
        self._highlight_scan_row(bn, ch)     # only if that bank is shown
        # a channel whose mode a module decodes is handed to it (request_decode); analog
        # stays audible until that decoder locks and claims the speaker (no hand-off)
        self._apply_channel_mode(ch.mode, ch.freq)
        self._set_freq(ch.freq, how="scan")
        self._notify_channel_recalled(ch)
        self._scan_holding = True
        self._apply_waterfall_state()        # show the waterfall on the hit
        self.radio.unmute()
        self._scan_state("hold")
        self.resume_btn.setEnabled(True)
        sub = ch.label or ch.mode
        if len(self._scan_bank_names()) > 1:
            sub = f"{sub} · {bn}"
        self._set_active_banner(ch.freq, sub, snr, "▶", "#39ff88")
        self.scan_status.setText(
            f"ACTIVE {ch.freq/1e6:.4f} MHz ({ch.label or ch.mode}) "
            f"+{snr:.1f} dB — holding (auto-resumes when clear)")

    def _apply_channel_mode(self, mode, freq_hz=None):
        """Point the demod at a channel's mode: a core analog mode, a module's mode, or a label a
        module decodes (request_decode, told the channel's frequency ``freq_hz``: the radio is
        still on the old one when this runs)."""
        if mode in MODES and mode != self.radio.mode.name:
            self._enter_mode(mode)           # analog (manages the RDS branch)
            self.mode_combo.blockSignals(True)
            self.mode_combo.setCurrentText(mode)
            self.mode_combo.blockSignals(False)
        elif mode in self._mode_specs and mode != self.mode_combo.currentText():
            self.mode_combo.setCurrentText(mode)          # a module's mode (-> _enter_mode)
        elif mode not in MODES:
            self.request_decode(mode, int(freq_hz if freq_hz is not None else self.radio.freq_hz),
                                {"source": "channel"})

    def _on_priority_hit(self, freq, mode, label, snr):
        """A ★ Priority-bank channel went active — interrupt to it."""
        self._apply_channel_mode(mode, freq)
        self._set_freq(freq, how="scan")
        ch = next((c for c in self.store.priority().channels if c.freq == freq and c.mode == mode), None)
        if ch is not None:
            self._notify_channel_recalled(ch)
        self._scan_holding = True
        self._apply_waterfall_state()
        self.radio.unmute()
        self._scan_state("hold")
        self.resume_btn.setEnabled(True)
        # highlight the row if this priority freq is also visible in the view
        self.table.clearSelection()
        bank = self.store.bank()
        for r, bidx in enumerate(self._row_bankidx):
            if bank.channels[bidx].freq == freq:
                self.table.selectRow(r)
                break
        self._set_active_banner(freq, "★ PRIORITY  " + (label or mode), snr,
                                "★", "#f0b429")
        self.scan_status.setText(
            f"★ PRIORITY {freq/1e6:.4f} MHz ({label or mode}) +{snr:.1f} dB")

    def _add_to_priority(self):
        """Copy the selected table channel(s) — or the current tuned freq if
        nothing is selected — into the ★ Priority bank (deduped by freq+mode)."""
        import copy
        prio = self.store.priority()
        existing = {(c.freq, c.mode) for c in prio.channels}
        bank = self.store.bank()
        picks = []
        for mi in self.table.selectionModel().selectedRows():
            r = mi.row()
            if 0 <= r < len(self._row_bankidx):
                picks.append(bank.channels[self._row_bankidx[r]])
        if not picks:                         # nothing selected -> current tune
            picks = [Channel(freq=int(self.radio.freq_hz),
                             mode=self.radio.mode.name,
                             label=_fmt_mhz(self.radio.freq_hz))]
        added = 0
        for ch in picks:
            if (ch.freq, ch.mode) not in existing:
                prio.channels.append(copy.deepcopy(ch))
                existing.add((ch.freq, ch.mode))
                added += 1
        self.store.save()
        self._refresh_bank_combo()
        if self.store.active == PRIORITY_BANK:
            self._refresh_table()
        self.scan_status.setText(
            f"added {added} → {PRIORITY_BANK} ({len(prio.channels)} total)")

    def _apply_waterfall_state(self):
        """Run the waterfall when the Radio page is showing AND (idle, parked on
        an active channel, or 'Waterfall during scan' is ticked). It's paused on
        the Memories page (hidden anyway) and while actively sweeping. This is the
        single decision point, so the scan pause/resume calls and the view switch
        never fight each other."""
        show = self._on_radio_page() and (
            (not self._scanning) or self._scan_holding
            or self.act_wf_scan.isChecked())
        if show:
            self.spectrum.resume()
        else:
            self.spectrum.pause()

    def _on_wf_scan_toggle(self, _on):
        self._apply_waterfall_state()   # takes effect immediately, either way

    def _show_annotations(self):
        """A module's annotations should be seen: Label source "memories" becomes "both"."""
        if self._label_source == "memories":
            self._label_src_actions["both"].setChecked(True)
            self._label_source = "both"
            self.spectrum.set_label_source("both")

    def _set_app_icon(self, icon):
        """Window / taskbar icon: ``icon`` (a module's, while it runs something), or None for the usual one."""
        icon = icon or make_scanner_icon()
        self.setWindowIcon(icon)
        QtWidgets.QApplication.instance().setWindowIcon(icon)

    def _set_label_source(self, source):
        self._label_source = source
        self.spectrum.set_label_source(source)

    def _on_show_recorded_toggle(self, on):
        self._show_recorded = on
        self.spectrum.show_recorded = on
        if on:
            self._refresh_recorded()
        self.spectrum.update()

    def _refresh_recorded(self):
        """Rebuild the recorded-frequency index (recordings/ sidecars + the real-data store)."""
        if not self._show_recorded:
            return
        try:
            self.spectrum.set_recorded(recindex.build(_recordings_dir(), self._realstore()))
        except Exception as e:                 # never let a bad sidecar break the UI
            print(f"[sdrexplorer] recorded-frequency index: {e}", flush=True)

    def _realstore(self):
        """The real-data store (sdrexplorer.api.store; its directory is paths key store_dir)."""
        from .api import store
        return store

    def _on_spectrum_context(self, freq_hz, gpos, det):
        menu = QtWidgets.QMenu(self)
        title = (f"{det['label']} @ {det['freq_hz']/1e6:.5f} MHz" if det
                 else f"{freq_hz/1e6:.5f} MHz")
        hdr = menu.addAction(title)
        hdr.setEnabled(False)
        menu.addSeparator()
        f = det["freq_hz"] if det else freq_hz
        menu.addAction("Tune here", lambda: self._on_spectrum_click(int(f)))
        self._add_module_context_entries(menu, f, det)
        menu.exec_(gpos)

    def _add_module_context_entries(self, menu, freq_hz, det):
        """Modules' right-click entries (api.Module.context_entries) for the signal at freq_hz."""
        for m in self.modules.loaded:
            try:
                entries = m.context_entries(int(freq_hz), det) or []
            except Exception:
                print(f"[sdrexplorer] module {m.name}: context_entries failed:", file=sys.stderr)
                import traceback
                traceback.print_exc()
                continue
            if entries:
                menu.addSeparator()
            for e in entries:
                self._add_context_entry(menu, m, e)

    def _add_context_entry(self, menu, module, e):
        if e.submenu:
            sub = menu.addMenu(e.text)
            sub.setEnabled(e.enabled)
            if e.tooltip:
                sub.setToolTip(e.tooltip)
            for s in e.submenu:
                self._add_context_entry(sub, module, s)
            return
        a = menu.addAction(e.text)
        a.setEnabled(e.enabled)
        if e.tooltip:
            a.setToolTip(e.tooltip)
        if e.triggered is not None:
            a.triggered.connect(lambda _c=False, fn=e.triggered: self._call_module(module, e.text, fn))

    def _on_wf_labels_toggle(self, on):
        self._wf_labels = on
        self.spectrum.show_markers = on
        self.spectrum.update()

    def _update_markers(self):
        """Push every known channel (all banks, deduped by freq) to the spectrum
        so their names overlay the waterfall. The visible-span filter in the
        widget keeps only the ones currently on screen."""
        if not hasattr(self, "spectrum"):
            return
        seen, markers = set(), []
        for bank in self.store.banks.values():
            for ch in bank.channels:
                key = round(ch.freq / 1000)          # dedup ~1 kHz
                if key in seen:
                    continue
                seen.add(key)
                markers.append((ch.freq, ch.label or ch.mode))
        self.spectrum.set_markers(markers)

    def _on_scan_resumed(self):
        """Worker left an active channel (auto after hang, or manual skip)."""
        if self._scanning:
            self.audio.release_all()         # leaving the channel: every decoder's claim ends
            self._scan_holding = False
            self._apply_waterfall_state()    # back to sweeping -> pause unless opted in
            self.radio.mute()
            self._scan_state("sweep")
            self.table.clearSelection()      # back to a static list
            self._show_scanning(True)        # back to "SCANNING …"
            self.resume_btn.setEnabled(False)
            self.scan_status.setText("scanning…")

    def _scan_state(self, state):
        """The scan moved to ``state`` ("sweep" | "hold" | "idle"): modules' audio is off while it sweeps,
        and they hear it on the "scan" topic."""
        self.radio.set_inputs_muted(state == "sweep")
        self.bus.publish("scan", state=state)

    def _resume_scan(self):
        # manual skip: the worker leaves the channel and emits `resumed`
        if self.scan_worker and self._scanning:
            self.scan_worker.resume()

    def _on_scan_finished(self):
        self.tuner.release("scan")
        self._scanning = False
        self._scan_holding = False
        self.audio.release_all()             # every decoder's claim ends with the scan
        self.scan_btn.setText("▶ Scan")
        self.resume_btn.setEnabled(False)
        self.rec_btn.setEnabled(True)        # recording allowed again
        self._update_ptt_enabled()
        # the sweep moved the hardware band center; recenter on the demod freq
        self.radio.set_frequency(self.radio.demod_hz)
        self._show_scanning(False)           # back to the frequency LCD
        self._show_freq()
        self._apply_waterfall_state()        # scan stopped -> waterfall back on
        self.radio.unmute()
        self._scan_state("idle")
        self.scan_status.setText("idle")
        # scan_worker is retired via its finished signal (_retire)

    # =====================================================================
    #  Discover sweep
    # =====================================================================
    def _start_discover(self):
        if self.disc_worker is not None:
            return
        try:
            lo = float(self.lo_edit.text()) * 1e6
            hi = float(self.hi_edit.text()) * 1e6
        except ValueError:
            self.disc_status.setText("bad range")
            return
        if hi <= lo:
            self.disc_status.setText("hi must exceed lo")
            return
        self.disc_btn.setEnabled(False)
        self.disc_list.clear()
        self.radio.mute()
        self.disc_worker = DiscoverWorker(self.radio, lo, hi,
                                          prom_thresh=self.disc_thresh.value())
        self.disc_worker.progress.connect(
            lambda p: self.disc_status.setText(f"sweeping… {p}%"))
        self.disc_worker.done.connect(self._on_discover_done)
        self.disc_worker.finished.connect(lambda: self._retire("disc_worker"))
        self.disc_worker.start()

    def _on_discover_done(self, peaks):
        for hz, prom in peaks:
            item = QtWidgets.QListWidgetItem(
                f"{hz/1e6:10.4f} MHz   +{prom:.1f} dB")
            item.setData(QtCore.Qt.UserRole, hz)
            self.disc_list.addItem(item)
        self.disc_status.setText(f"found {len(peaks)} signals")
        self.disc_btn.setEnabled(True)
        self.radio.set_frequency(self.radio.freq_hz)  # restore tuning + spectrum
        self.radio.unmute()
        # disc_worker is retired via its finished signal (_retire)

    def _on_disc_activate(self, item):
        self._user_tunes()
        self._on_disc_activate_tune(item)

    def _on_disc_activate_tune(self, item):
        hz = item.data(QtCore.Qt.UserRole)
        self.mode_combo.setCurrentText(self.disc_mode.currentText())
        self._set_freq(hz)

    def _save_discovered(self):
        mode = self.disc_mode.currentText()
        n = 0
        for item in self.disc_list.selectedItems():
            hz = item.data(QtCore.Qt.UserRole)
            self.store.add_channel(Channel(freq=int(hz), mode=mode,
                                           source="discover"))
            n += 1
        if n:
            self._refresh_table()
            self.disc_status.setText(f"saved {n} → {self.store.active}")

    # =====================================================================
    #  Decode strip: every decoder's log (api.Decode), payloads, modules' tabs
    # =====================================================================
    def _build_digital_group(self):
        box = QtWidgets.QGroupBox("Decode")
        box.setMinimumWidth(300)
        outer = QtWidgets.QVBoxLayout(box)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        opts = QtWidgets.QHBoxLayout()
        self.dig_automute = QtWidgets.QCheckBox("Auto-mute analog")
        self.dig_automute.setChecked(self._auto_mute)
        self.dig_automute.setToolTip("Mute the analog audio a mode asks to hide (e.g. a data mode's "
                                     "warble). A decoder that has a lock always takes the speaker.")
        self.dig_automute.toggled.connect(self._on_automute_toggle)
        opts.addWidget(self.dig_automute)
        opts.addStretch(1)
        self.dig_hex_cb = QtWidgets.QCheckBox("Payloads")
        self.dig_hex_cb.setChecked(self._show_payload_hex)
        self.dig_hex_cb.setToolTip("Show decoders' raw payloads as a hexdump -C (hex + ASCII) beside the log.")
        self.dig_hex_cb.toggled.connect(self._on_payload_pane_toggle)
        opts.addWidget(self.dig_hex_cb)
        self.dig_save_btn = QtWidgets.QPushButton("Save payloads…")
        self.dig_save_btn.setToolTip("Write every captured raw payload (hex + ASCII) "
                                     "to a file for offline analysis.")
        self.dig_save_btn.clicked.connect(self._save_payloads)
        opts.addWidget(self.dig_save_btn)
        self.dig_pcap_btn = QtWidgets.QPushButton("Export .pcap…")
        self.dig_pcap_btn.setToolTip("Write captured payloads that carry a pcap link type (e.g. raw IP) "
                                     "as .pcap files for Wireshark, one per link type.")
        self.dig_pcap_btn.clicked.connect(self._export_pcap)
        opts.addWidget(self.dig_pcap_btn)
        self.dig_clear = QtWidgets.QPushButton("Clear")
        self.dig_clear.clicked.connect(self._clear_digital)
        opts.addWidget(self.dig_clear)
        outer.addLayout(opts)

        self.dig_status = QtWidgets.QLabel("idle")
        self.dig_status.setStyleSheet("color:#8b95a1;")
        outer.addWidget(self.dig_status)

        # live text filters for the two panes (show only matching lines/blocks)
        filt = QtWidgets.QHBoxLayout()
        self.dig_search = QtWidgets.QLineEdit()
        self.dig_search.setPlaceholderText("filter decode log… (any text)")
        self.dig_search.setClearButtonEnabled(True)
        self.dig_search.textChanged.connect(self._apply_dig_filter)
        filt.addWidget(self.dig_search, 1)
        self.hex_search = QtWidgets.QLineEdit()
        self.hex_search.setPlaceholderText("filter payloads…")
        self.hex_search.setClearButtonEnabled(True)
        self.hex_search.textChanged.connect(self._apply_hex_filter)
        self.hex_search.setVisible(self._show_payload_hex)
        filt.addWidget(self.hex_search, 1)
        self.hex_follow_cb = QtWidgets.QCheckBox("Follow log filter")
        self.hex_follow_cb.setToolTip(
            "Filter the payloads with the decode-log filter: show the raw payloads of the log lines it "
            "matches (the same decoded burst), and any payload that contains the text itself.")
        self.hex_follow_cb.setChecked(self._hex_follow)
        self.hex_follow_cb.toggled.connect(self._on_hex_follow_toggle)
        self.hex_follow_cb.setVisible(self._show_payload_hex)
        self.hex_search.setEnabled(not self._hex_follow)
        filt.addWidget(self.hex_follow_cb)
        outer.addLayout(filt)

        # decode log (left) + optional payload hexdump (right), side by side
        mono = ("font-family:'DejaVu Sans Mono'; font-size:11px; "
                "background:#0a0c0f; color:#39ff88;")
        self.dig_log = QtWidgets.QPlainTextEdit()
        self.dig_log.setReadOnly(True)
        self.dig_log.setMaximumBlockCount(2000)        # bounded scroll history
        self.dig_log.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.dig_log.setStyleSheet(mono)
        self.dig_hex = QtWidgets.QPlainTextEdit()
        self.dig_hex.setReadOnly(True)
        self.dig_hex.setMaximumBlockCount(20000)       # hexdumps are multi-line
        self.dig_hex.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.dig_hex.setStyleSheet(mono.replace("#39ff88", "#b6c2cf"))
        self.dig_hex.setVisible(self._show_payload_hex)
        split = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        split.addWidget(self.dig_log)
        split.addWidget(self.dig_hex)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        outer.addWidget(split, 1)
        # modules' panels (api.Panel, area "decode"): one tab each, hidden until a module adds one
        self.module_tabs = QtWidgets.QTabWidget()
        self.module_tabs.setDocumentMode(True)
        self.module_tabs.setVisible(False)
        outer.addWidget(self.module_tabs)
        return box












    def _on_automute_toggle(self, on):
        self._auto_mute = bool(on)
        if not self._scanning:                  # re-apply the gate now
            self._update_signal()

    def _decode_station(self, proto=None, ids=None):
        """The frequency + channel label currently feeding the decoder, so each
        log line says which station produced it. Co-channel entries (same freq,
        different agency) are resolved by the decoded ids (a record's ``ids`` against
        the channel's tone code) and the protocol as the channel's mode, and the
        station's tone/ID code is shown to confirm the match."""
        hz = int(self.radio.freq_hz)
        m = self._match_channel(hz, prefer_mode=proto, ids=ids)
        tag = f"{hz/1e6:.4f}"
        if m:
            _bank, ch = m
            if ch.label:
                tag += f" {ch.label}"
            if getattr(ch, "tone", ""):
                tag += f" [{ch.tone}]"
        return tag













    # -- raw payload capture / hexdump ---------------------------------------
    @staticmethod
    def _hexdump(data):
        """`hexdump -C`-style: offset, two 8-byte hex groups, then |ASCII|."""
        out = []
        for off in range(0, len(data), 16):
            chunk = data[off:off + 16]
            hexb = [f"{b:02x}" for b in chunk]
            cols = (" ".join(hexb[:8]) + "  " + " ".join(hexb[8:16])).ljust(50)
            txt = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            out.append(f"{off:08x}  {cols}|{txt}|")
        return "\n".join(out) if out else "        (empty)"

    def _payload_block(self, e):
        head = f"{e['station']}  {e['proto']} {e['label']}".rstrip()
        return f"{head}\n{self._hexdump(e['raw'])}"

    def _capture_payload(self, station, proto, label, raw, linktype=_pcap.LINKTYPE_RAW, ref=None):
        e = {"station": station, "proto": proto, "label": label,
             "raw": bytes(raw), "linktype": linktype, "ref": ref,
             "ts": QtCore.QDateTime.currentMSecsSinceEpoch()}   # for pcap export
        self._payloads.append(e)
        if len(self._payloads) > self._payload_cap:
            del self._payloads[:len(self._payloads) - self._payload_cap]
        if self.dig_hex_cb.isChecked() and self._payload_shown(e):
            self.dig_hex.appendPlainText(self._payload_block(e))

    def _payload_shown(self, e):
        """Whether a payload passes the payload pane's filter: its own filter box, or with "Follow log
        filter" the decode-log filter (its text, or a matching log line of the same burst)."""
        if not self._hex_follow:
            return self._match(self._payload_block(e), self._hex_filter)
        if not self._dig_filter:
            return True
        return (e.get("ref") is not None and e["ref"] in self._match_refs) \
            or self._match(self._payload_block(e), self._dig_filter)

    def _refill_payloads(self):
        if self.dig_hex_cb.isChecked():
            self.dig_hex.setPlainText("\n".join(
                self._payload_block(e) for e in self._payloads if self._payload_shown(e)))
            self.dig_hex.moveCursor(QtGui.QTextCursor.End)

    def _on_hex_follow_toggle(self, on):
        self._hex_follow = bool(on)
        self.hex_search.setEnabled(not on)
        self._refill_payloads()

    # -- decode-log filter ----------------------------------------------------
    @staticmethod
    def _match(text, filt):
        return (not filt) or (filt in text.lower())

    def _dig_append(self, line, ref=None):
        """Append a decode-log line to the backing store and, if it passes the
        current filter, to the visible pane. Every line is timestamped so a
        long, deduped-quiet stretch (see _on_decoded) doesn't leave you
        guessing how stale the last line is."""
        line = f"{datetime.now():%H:%M:%S}  {line}"
        self._dig_lines.append(line)
        self._dig_refs.append(ref)
        if len(self._dig_lines) > 5000:
            del self._dig_lines[:len(self._dig_lines) - 5000]
            del self._dig_refs[:len(self._dig_refs) - 5000]
        if self._match(line, self._dig_filter):
            self.dig_log.appendPlainText(line)
            if ref is not None and self._dig_filter and ref not in self._match_refs:
                self._match_refs.add(ref)
                if self._hex_follow and any(e.get("ref") == ref for e in self._payloads):
                    self._refill_payloads()         # its payload came before the line

    def _apply_dig_filter(self, text):
        self._dig_filter = text.strip().lower()
        self.dig_log.setPlainText(
            "\n".join(l for l in self._dig_lines if self._match(l, self._dig_filter)))
        self.dig_log.moveCursor(QtGui.QTextCursor.End)
        self._match_refs = ({r for l, r in zip(self._dig_lines, self._dig_refs)
                             if r is not None and self._match(l, self._dig_filter)}
                            if self._dig_filter else set())
        if self._hex_follow:
            self._refill_payloads()

    def _apply_hex_filter(self, text):
        self._hex_filter = text.strip().lower()
        self._refill_payloads()




    def _on_payload_pane_toggle(self, on):
        self._show_payload_hex = bool(on)
        self.dig_hex.setVisible(on)
        self.hex_search.setVisible(on)
        self.hex_follow_cb.setVisible(on)
        if on:
            self._refill_payloads()               # backfill from the buffer (filtered)

    def _clear_digital(self):
        self.dig_log.clear()
        self.dig_hex.clear()
        self._dig_lines.clear()
        self._dig_refs.clear()
        self._match_refs.clear()
        self._payloads.clear()
        self._last_payload = None
        self._decode_seen.clear()

    def _save_payloads(self):
        if not self._payloads:
            self.dig_status.setText("no payloads captured yet")
            return
        os.makedirs(_recordings_dir(), exist_ok=True)
        ts = QtCore.QDateTime.currentDateTime().toString("yyyyMMdd_hhmmss")
        default = os.path.join(_recordings_dir(), f"payloads_{ts}.txt")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save decoded payloads", default,
            "Hexdump text (*.txt);;All files (*)")
        if not path:
            return
        try:
            with open(path, "w") as fh:
                for e in self._payloads:
                    fh.write(self._payload_block(e) + "\n\n")
            self.dig_status.setText(
                f"saved {len(self._payloads)} payloads → {os.path.basename(path)}")
        except OSError as ex:
            self.dig_status.setText(f"save failed: {ex}")

    def _export_pcap(self):
        """Write the captured payloads that carry a pcap link type to .pcap files for Wireshark,
        one file per link type (pcap.write_by_linktype)."""
        if not self._payloads:
            QtWidgets.QMessageBox.information(
                self, "Export .pcap",
                "No payloads captured yet: a decoder that captures data bursts "
                "has to deliver some first.")
            return
        os.makedirs(_recordings_dir(), exist_ok=True)
        ts = QtCore.QDateTime.currentDateTime().toString("yyyyMMdd_hhmmss")
        default = os.path.join(_recordings_dir(), f"payloads_{ts}.pcap")
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export payloads to pcap", default,
            "pcap capture (*.pcap);;All files (*)")
        if not path:
            return
        try:
            files = _pcap.write_by_linktype(path, self._payloads)
        except Exception as ex:      # surface any failure instead of swallowing it
            QtWidgets.QMessageBox.warning(
                self, "Export .pcap", f"pcap export failed:\n{ex}")
            return
        if not files:
            self.dig_status.setText("none of the captured payloads is pcap-exportable")
            return
        n = sum(c for _p, _lt, c in files)
        msg = f"exported {n} frames → " + ", ".join(os.path.basename(p) for p, _lt, _c in files)
        self.dig_status.setText(msg + "  (open in Wireshark)")
        self.statusBar().showMessage(msg, 6000)   # visible on the Radio view too

    # =====================================================================
    #  IQ recording (save the live band to a file for offline replay)
    # =====================================================================
    def _toggle_record(self):
        if self._trigger is not None:          # ■ DISARM / ■ STOP while a trigger is armed
            self._disarm_trigger()
        elif self._rec_path is not None:
            self._stop_record()
        else:
            self._start_record()

    def _start_record(self):
        if self._scanning:
            self.rec_status.setText("stop the scan first")
            return
        scope = self.rec_scope_combo.currentData() or "band"
        self._begin_recording(scope)

    def _on_spectrum_selection(self, center_hz, bw_hz):
        """Right-click-drag on the waterfall: ask whether to record that
        sub-band from now on, or snapshot it from the buffered past."""
        if self._scanning:
            self.rec_status.setText("stop the scan first")
            return
        menu = QtWidgets.QMenu(self)
        hdr = menu.addAction(f"{center_hz/1e6:.4f} MHz  ·  {bw_hz/1e3:.4g} kHz")
        hdr.setEnabled(False)
        menu.addSeparator()
        rec = menu.addAction("● Record this sub-band")
        if self._rec_path is not None:
            rec.setEnabled(False)
            rec.setText("● Record this sub-band (already recording)")
        snap = menu.addAction(self._snapshot_label("◷ Snapshot this sub-band"))
        snap.setEnabled(self._snapshot_ready())
        trig = menu.addAction(f"◎ Record when a signal appears  (≥ {self._trigger_db:.0f} dB, "
                              f"hang {self._trigger_hang:.0f} s)")
        if self._trigger is not None:
            trig.setText("◎ Record when a signal appears (replaces the armed window)")
        if not self.radio.running:
            trig.setEnabled(False)
            trig.setText("◎ Record when a signal appears (radio stopped)")
        elif self._rec_path is not None and self._trigger is None:
            trig.setEnabled(False)
            trig.setText("◎ Record when a signal appears (already recording)")
        menu.addAction("Cancel")
        # the drag highlight disappears on release; keep it on screen while the menu is up
        self.spectrum.set_pending_selection((center_hz, bw_hz))
        try:
            chosen = menu.exec_(QtGui.QCursor.pos())
        finally:
            self.spectrum.set_pending_selection(None)
        if chosen is rec:
            self._begin_recording("subband", center_hz, bw_hz)
        elif chosen is snap:
            self._take_snapshot((center_hz, bw_hz))
        elif chosen is trig:
            self._arm_trigger(center_hz, bw_hz)

    # =====================================================================
    #  Signal-triggered sub-band recording
    # =====================================================================
    TRIGGER_POLL_MS = 100

    def _arm_trigger(self, center_hz, bw_hz):
        """Watch (center_hz ± bw_hz/2) and record it as a sub-band recording each time a signal
        appears there (rectrigger.TriggerMonitor; level = radio.window_level). Re-arms after
        every recording, so each transmission is its own file, until disarmed (■ STOP, Esc,
        or a retune that moves the window out of the band)."""
        self._disarm_trigger(quiet=True)
        mon = TriggerMonitor(self._trigger_db, self._trigger_hang, self._trigger_max)
        self._trigger = {"center": int(center_hz), "bw": int(bw_hz), "mon": mon, "level": None}
        if not hasattr(self, "_trigger_timer"):
            self._trigger_timer = QtCore.QTimer(self)
            self._trigger_timer.timeout.connect(self._trigger_tick)
        self._trigger_timer.start(self.TRIGGER_POLL_MS)
        self.spectrum.set_trigger_window((int(center_hz), int(bw_hz)), armed=True)
        self.rec_btn.setText("■ DISARM")
        self.rec_btn.setStyleSheet("color:#f0b429; font-weight:bold;")
        self._show_trigger_status()

    def _disarm_trigger(self, quiet=False):
        """Stop watching. A recording the trigger started is finished (kept), not deleted."""
        if self._trigger is None:
            return False
        if self._trigger["mon"].state == "recording" and self._rec_path is not None:
            self._stop_record()
        n = self._trigger["mon"].count
        self._trigger = None
        if hasattr(self, "_trigger_timer"):
            self._trigger_timer.stop()
        self.spectrum.set_trigger_window(None)
        if self._rec_path is None:
            self.rec_btn.setText("● REC")
            self.rec_btn.setStyleSheet("")
        if not quiet:
            self.rec_status.setText(f"trigger disarmed ({n} recording{'s' if n != 1 else ''})")
        return True

    def _trigger_tick(self):
        t = self._trigger
        if t is None:
            return
        if not self.radio.running or self._scanning:
            self._disarm_trigger()
            return
        level = self.radio.window_level(t["center"], t["bw"])
        if level is None and t["mon"].state == "armed":
            # the window left the captured band (retune / bandwidth change): nothing to watch
            self._disarm_trigger()
            self.rec_status.setText("trigger disarmed: the window is outside the band")
            return
        t["level"] = level
        ev = t["mon"].feed(level, time.monotonic())
        if ev == "start":
            self._begin_recording("subband", t["center"], t["bw"], trigger=True)
            self.spectrum.set_trigger_window((t["center"], t["bw"]), armed=False)
        elif ev == "stop":
            if self._rec_path is not None:
                self._stop_record()
            self.spectrum.set_trigger_window((t["center"], t["bw"]), armed=True)
            self.rec_btn.setText("■ DISARM")
            self.rec_btn.setStyleSheet("color:#f0b429; font-weight:bold;")
        self._show_trigger_status()

    def _show_trigger_status(self):
        t = self._trigger
        if t is None:
            return
        mon = t["mon"]
        lvl = "–" if t["level"] is None else f"{t['level']:.0f}"
        if mon.state == "armed":
            self.rec_status.setText(
                f"◎ armed {t['center']/1e6:.4f} MHz ±{t['bw']/2e3:.3g} kHz · {lvl}/{mon.threshold_db:.0f} dB"
                f" · {mon.count} rec")
        # while recording, _update_rec_status shows "● REC Ns  MB"

    def _open_trigger_settings(self):
        """Settings ▸ Recording trigger…: threshold, hang time, max length, pre-roll."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("Recording trigger")
        form = QtWidgets.QFormLayout(dlg)
        thr = QtWidgets.QDoubleSpinBox(); thr.setRange(3, 40); thr.setSuffix(" dB")
        thr.setValue(self._trigger_db)
        thr.setToolTip("Peak level in the window over the noise floor that starts a recording.")
        hang = QtWidgets.QDoubleSpinBox(); hang.setRange(0.2, 30); hang.setSuffix(" s")
        hang.setValue(self._trigger_hang)
        hang.setToolTip("Keep recording this long after the signal drops (speech pauses, TDMA gaps).")
        mx = QtWidgets.QDoubleSpinBox(); mx.setRange(5, 3600); mx.setSuffix(" s")
        mx.setValue(self._trigger_max)
        mx.setToolTip("Longest single triggered recording; it re-arms after.")
        pre = QtWidgets.QCheckBox("Save the seconds before the trigger (iq_pretrig_*)")
        pre.setChecked(self._trigger_pre)
        pre.setToolTip("Saves the window's last few seconds before the signal crossed the "
                       "threshold, from the snapshot buffer, as a separate iq_pretrig_* file, so "
                       "the start of each transmission isn't lost. Needs Settings ▸ IQ snapshot > 0 s.")
        pre_s = QtWidgets.QDoubleSpinBox(); pre_s.setRange(0.5, 60); pre_s.setSuffix(" s")
        pre_s.setValue(self._trigger_pre_s)
        pre_s.setToolTip("How much of the time before the trigger to keep. The channel is usually "
                         "idle until then, so a couple of seconds covers the transmission's start; "
                         "longer is limited by the snapshot buffer.")
        pre_s.setEnabled(pre.isChecked())
        pre.toggled.connect(pre_s.setEnabled)
        form.addRow("Threshold", thr)
        form.addRow("Hang time", hang)
        form.addRow("Max recording", mx)
        form.addRow(pre)
        form.addRow("Pre-roll length", pre_s)
        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        form.addRow(btns)
        if dlg.exec_() == QtWidgets.QDialog.Accepted:
            self._trigger_db, self._trigger_hang = thr.value(), hang.value()
            self._trigger_max, self._trigger_pre = mx.value(), pre.isChecked()
            self._trigger_pre_s = pre_s.value()
            if self._trigger is not None:          # apply to the armed window now
                m = self._trigger["mon"]
                m.threshold_db, m.hang_s, m.max_s = self._trigger_db, self._trigger_hang, self._trigger_max

    # =====================================================================
    #  IQ snapshot (the last few seconds, from the radio's ring buffer)
    # =====================================================================
    def _snapshot_ready(self):
        return (self.radio.running and self.radio.snapshot_seconds > 0
                and self._snap_writer is None)

    def _snapshot_label(self, text):
        if not self.radio.running:
            return f"{text} (radio stopped)"
        if self.radio.snapshot_seconds <= 0:
            return f"{text} (off in Settings)"
        if self._snap_writer is not None:
            return f"{text} (saving previous…)"
        have = min(self.radio.snapshot_buffered_s(), self.radio.snapshot_seconds)
        return f"{text} (last {have:.0f} s)"

    def _take_snapshot(self, subband=None, post_s=None, pre_s=None):
        """Save the buffered band (or a (center_hz, bw_hz) slice of it) plus
        SNAPSHOT_POST_S (or ``post_s``) after the click, on a worker thread. post_s=0 is the
        recording trigger's pre-roll: just the seconds before, as iq_pretrig_* (quiet, so the
        recording's own "● REC" status stays on screen)."""
        if not self._snapshot_ready():
            if post_s is None:
                self.rec_status.setText(self._snapshot_label("snapshot unavailable"))
            return
        os.makedirs(_recordings_dir(), exist_ok=True)
        ts = QtCore.QDateTime.currentDateTime().toString("yyyyMMdd_hhmmss")
        pre = post_s is not None

        def path_for(meta):
            if subband is None:
                name = f"iq_snap_{meta['center_hz']/1e6:.4f}MHz_{ts}.cs16"
            else:
                c, bw = subband
                kind = "pretrig" if pre else "snapsub"
                name = f"iq_{kind}_{c/1e6:.4f}MHz_{bw/1e3:.0f}kHz_{ts}.cs16"
            return os.path.join(_recordings_dir(), name)

        w = SnapshotWriter(self.radio, path_for, self.spectrum.waterfall_rows(),
                           subband, self, post_s=post_s, pre_s=pre_s)
        w.done.connect(self._on_snapshot_done)
        self._snap_writer = w
        self._snap_quiet = pre
        self.snap_btn.setEnabled(False)
        if not pre:
            self.rec_status.setText("◷ snapshot: capturing…")
        w.start()

    def _on_snapshot_done(self, path, status):
        w, self._snap_writer = self._snap_writer, None
        if w is not None:
            w.wait()
        self.snap_btn.setEnabled(True)
        if not getattr(self, "_snap_quiet", False):
            self.rec_status.setText(status)
        self._snap_quiet = False
        self._refresh_recorded()

    def _open_snapshot_settings(self):
        sec, ok = QtWidgets.QInputDialog.getInt(
            self, "IQ snapshot",
            "Seconds before the click that ◷ SNAP saves (0 = off); 2 s after\n"
            "the click are added. RAM ≈ 4 bytes × sample rate × (seconds + 3):\n"
            "at 19.2 Msps 10 s ≈ 1.0 GB, 5 s ≈ 610 MB; a 10 s band snapshot\n"
            "file is ~920 MB (12 s). Sub-band snapshots are far smaller.",
            int(self.radio.snapshot_seconds), 0, 60, 1)
        if ok:
            self.radio.set_snapshot_seconds(sec)
            self.rec_status.setText(f"snapshot buffer: {sec} s" if sec else "snapshot off")

    def _begin_recording(self, scope, center_hz=None, bw_hz=None, trigger=False):
        try:
            os.makedirs(_recordings_dir(), exist_ok=True)
            ts = QtCore.QDateTime.currentDateTime().toString("yyyyMMdd_hhmmss")
            if scope == "subband":
                tag = "trig" if trigger else "sub"
                path = os.path.join(
                    _recordings_dir(),
                    f"iq_{tag}_{center_hz/1e6:.4f}MHz_{bw_hz/1e3:.0f}kHz_{ts}.cs16")
            else:
                tag = "chan" if scope == "demod" else "band"
                path = os.path.join(
                    _recordings_dir(),
                    f"iq_{tag}_{self.radio.demod_hz/1e6:.4f}MHz_{ts}.cs16")
            meta = self.radio.start_record(path, scope=scope,
                                           center_hz=center_hz, bw_hz=bw_hz)
        except Exception as e:
            self.rec_status.setText(f"record failed: {e}")
            return
        self._rec_path = path
        self._rec_elapsed = 0
        # tally decoder output for the sidecar's "decoded" block (written on stop)
        self._rec_labels = RecordingLabels(self.radio.demod_hz)
        # sidecar JSON so the capture is self-describing for replay
        meta["file"] = os.path.basename(path)
        if trigger:
            t = self._trigger or {}
            meta["trigger"] = {"threshold_db": self._trigger_db, "hang_s": self._trigger_hang,
                               "level_db": None if t.get("level") is None else round(t["level"], 1)}
        try:
            with open(path + ".json", "w") as fh:
                json.dump(meta, fh, indent=2)
        except OSError:
            pass
        self.bus.publish("recording_started", path=path, scope=meta.get("scope", scope),
                         start_time=meta.get("start_time"))
        self.rec_btn.setText("■ STOP")
        self.rec_btn.setStyleSheet("color:#ff5555; font-weight:bold;")
        self.rec_status.setText("● REC 0s  0 MB")
        self._rec_timer.start(1000)
        if trigger and self._trigger_pre and self._snapshot_ready():
            # the seconds before the trigger are still in the snapshot ring: save that slice of
            # the window too (its own iq_pretrig_* file, written in the background, post 0 s)
            self._take_snapshot((center_hz, bw_hz), post_s=0.0, pre_s=self._trigger_pre_s)

    def _stop_record(self):
        try:
            self.radio.stop_record()
        except Exception:
            pass
        self._rec_timer.stop()
        path, self._rec_path = self._rec_path, None
        labels, self._rec_labels = self._rec_labels, None
        if self._trigger is not None:          # still armed: back to waiting, not to ● REC
            self.rec_btn.setText("■ DISARM")
            self.rec_btn.setStyleSheet("color:#f0b429; font-weight:bold;")
        else:
            self.rec_btn.setText("● REC")
            self.rec_btn.setStyleSheet("")
        if path and os.path.exists(path):
            self.bus.emit("recording_done", path=path, scope=getattr(self.radio, "_rec_scope", "band"))
            mb = os.path.getsize(path) / 1e6
            label = self._write_rec_labels(path, labels)
            self.rec_status.setText(f"saved {os.path.basename(path)} ({mb:.0f} MB)"
                                    + (f" · {label}" if label else ""))
            self._refresh_recorded()
        else:
            self.rec_status.setText("recording stopped")

    @staticmethod
    def _write_rec_labels(path, labels):
        """Merge the decoder summary into the capture's sidecar as "decoded"
        (the label source for a training real-data set -- see
        reclabel.py). Written on stop rather than start so it covers the whole
        capture. Returns the label (or None) for the status line."""
        if labels is None:
            return None
        summary = labels.summary()
        side = path + ".json"
        try:
            with open(side) as fh:
                meta = json.load(fh)
        except (OSError, ValueError):
            meta = {"file": os.path.basename(path)}
        meta["decoded"] = summary
        try:
            with open(side, "w") as fh:
                json.dump(meta, fh, indent=2)
        except OSError:
            return None
        return summary["label"]

    def _cancel_subband_recording(self):
        """Esc while a right-click-drag sub-band recording is running: stop it
        and delete the (incomplete, unwanted) file + sidecar. Returns True if
        a recording was actually canceled, so callers can decide whether to
        let the key event fall through to other Esc handling."""
        if self._rec_path is None or self.radio.subband_recording_range() is None:
            return False
        path = self._rec_path
        self._stop_record()
        for p in (path, path + ".json"):
            try:
                os.remove(p)
            except OSError:
                pass
        self.rec_status.setText("selection recording canceled")
        return True

    def keyPressEvent(self, ev):
        if ev.key() == QtCore.Qt.Key_Escape:
            # accidental right-click-drag: Esc aborts a live drag, or stops +
            # deletes a sub-band recording already started from one
            if self.spectrum.cancel_selection_drag() or self._disarm_trigger() \
                    or self._cancel_subband_recording():
                ev.accept()
                return
        super().keyPressEvent(ev)

    def _update_rec_status(self):
        self._rec_elapsed += 1
        mb = os.path.getsize(self._rec_path) / 1e6 \
            if self._rec_path and os.path.exists(self._rec_path) else 0.0
        self.rec_status.setText(f"● REC {self._rec_elapsed}s  {mb:.0f} MB")

    # =====================================================================
    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { background:#15181d; color:#d6dae0;
                      font-family:'DejaVu Sans'; font-size:12px; }
            QLCDNumber { background:#0a0c0f; color:#39ff88;
                         border:2px solid #2a2f38; border-radius:6px; }
            QLabel#scanBanner { background:#0a0c0f; color:#f0b429;
                         border:2px solid #2a2f38; border-radius:6px;
                         font-size:30px; font-weight:bold;
                         letter-spacing:3px; }
            QGroupBox { border:1px solid #2a2f38; border-radius:6px;
                        margin-top:8px; padding-top:10px; }
            QGroupBox::title { subcontrol-origin:margin; left:8px; color:#8aa0b4; }
            QPushButton { background:#232833; border:1px solid #39414d;
                          border-radius:5px; padding:5px 10px; }
            QPushButton:hover { background:#2c333f; }
            QPushButton:disabled { color:#5a636e; }
            QPushButton:checked { background:#1e6f3f; border-color:#39ff88;
                                  color:#eafff2; font-weight:bold; }
            QTableWidget, QListWidget { background:#0f1216; border:1px solid #2a2f38;
                          font-family:monospace; }
            QListWidget::item:selected,
            QTableWidget::item:selected { background:#2f6df6; color:white; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                          background:#0f1216; border:1px solid #2a2f38;
                          border-radius:4px; padding:2px 4px; }
            QSlider::groove:horizontal { height:6px; border-radius:3px;
                          background:#2a2f38; }
            QSlider::sub-page:horizontal { height:6px; border-radius:3px;
                          background:qlineargradient(x1:0, y1:0, x2:1, y2:0,
                                     stop:0 #2f8f5c, stop:1 #39ff88); }
            QSlider::add-page:horizontal { height:6px; border-radius:3px;
                          background:#2a2f38; }
            QSlider::handle:horizontal { width:16px; height:16px;
                          margin:-6px 0; border-radius:8px;
                          background:qradialgradient(cx:0.5, cy:0.4, radius:0.6,
                                     stop:0 #ffffff, stop:1 #b8c0cc);
                          border:1px solid #0a0c0f; }
            QSlider::handle:horizontal:hover { background:#39ff88; }
            /* check boxes: the default indicator was a dark box on the dark UI and an
               unticked one (e.g. Snap) was easy to miss -- a clear light border, a green
               fill + border when ticked */
            QCheckBox { spacing:6px; }
            QCheckBox::indicator, QGroupBox::indicator {
                          width:14px; height:14px; border-radius:3px;
                          border:2px solid #9aa5b1; background:#0f1216; }
            QCheckBox::indicator:hover, QGroupBox::indicator:hover { border-color:#d6dae0; }
            QCheckBox::indicator:checked, QGroupBox::indicator:checked {
                          border-color:#39ff88; background:#2f8f5c;
                          image:url(%(check)s); }
            QCheckBox::indicator:disabled, QGroupBox::indicator:disabled {
                          border-color:#3a4350; background:#15181d; }
            QCheckBox:disabled { color:#5a636e; }
        """ % {"check": _checkmark_icon()})

    def closeEvent(self, event):
        _exit_trace("closeEvent: begin")
        self.radio.silence_audio()        # kill audio first: no static burst as
                                          # the ADS-B branch/flowgraph tear down
        self.sig_timer.stop()
        self._replay_eof_timer.stop()
        self._pb_timer.stop()
        self.state_timer.stop()
        self._rec_timer.stop()
        self._disarm_trigger(quiet=True)  # a triggered recording in progress is finished, kept
        if self._rec_path is not None:    # finish/flush any active IQ recording
            self._stop_record()
        if self._snap_writer is not None:  # let a snapshot that is being saved finish
            self._snap_writer.wait(30_000)
        if self._uls_win is not None:     # let an FCC index build / search finish
            self._uls_win.shutdown()
        if not self._resetting:           # a reset intentionally discards state
            self._save_state()
        self.modules.shutdown()           # module threads / branches, while the radio exists
        self.spectrum.timer.stop()
        self._stop_rds()                  # stop the RDS monitor + gr-rds branch
        self._stop_adsb()                 # stop the ADS-B monitor + gr-adsb branch
        _exit_trace("closeEvent: before _close_map_window")
        self._close_map_window()          # tear down QWebEngineView while app alive
        _exit_trace("closeEvent: after _close_map_window")
        # stop + join every worker before the widgets are torn down, so no
        # QThread is destroyed while still running.
        if self.scan_worker is not None:
            self.scan_worker.stop()
            self.scan_worker.wait(2000)
        if self.disc_worker is not None:
            self.disc_worker.stop()
            self.disc_worker.wait(2000)
        if self.dl_worker is not None:
            self.dl_worker.cancel()
            self.dl_worker.wait(3000)
        for w in list(self._reaping):
            w.cancel()
            w.wait(3000)
        if self.fetch_worker is not None:
            self.fetch_worker.wait(3000)
        if self.geo_worker is not None:
            self.geo_worker.wait(3000)
        if not self._resetting:           # keep banks unless the reset wipes them
            self.store.save()
        if self._rec_path is not None:    # finish the file and its sidecar
            self._stop_record()
        self.radio.stop()
        self.radio.wait()
        _exit_trace("closeEvent: end (event.accept)")
        event.accept()


def main():
    import argparse
    parser = argparse.ArgumentParser(
        prog="sdrexplorer",
        description="Explore and analyze digital and analog signals using "
                    "software defined radio (HackRF / RTL-SDR).")
    parser.add_argument(
        "--portable", action="store_true",
        help="Keep settings, banks and recordings under <install>/data instead of the "
             "user's home directory (same as SDREXPLORER_PORTABLE=1).")
    mod_group = parser.add_mutually_exclusive_group()
    mod_group.add_argument(
        "--modules", metavar="A,B",
        help="Run only these modules this time (comma-separated names; see Settings ▸ Modules), "
             "whatever is switched on there.")
    mod_group.add_argument(
        "--no-modules", action="store_true",
        help="Run the core program only, with no modules, this time.")
    parser.add_argument(
        "--module-option", action="append", default=[], metavar="NAME.KEY=VALUE",
        help="An option for one module, this run only (repeatable); each module's README lists its own.")
    parser.add_argument(
        "--paths", action="store_true",
        help="Print where sdrexplorer finds its data and external builds, and exit.")
    gpu_group = parser.add_mutually_exclusive_group()
    gpu_group.add_argument(
        "--no-gpu", action="store_true",
        help="Disable CUDA GPU offload (channelizer + FFT probes) and run "
             "everything on CPU, even if gr-sdrexplorer_gpu is available.")
    gpu_group.add_argument(
        "--gpu", action="store_true",
        help="Require CUDA GPU offload. The default already uses the GPU "
             "whenever gr-sdrexplorer_gpu is available; this only has an "
             "effect if you want to be explicit.")
    args, qt_args = parser.parse_known_args()
    if args.portable:
        os.environ["SDREXPLORER_PORTABLE"] = "1"
    if args.paths:
        print(_paths.report())
        from . import features as _features
        print("\n" + _features.report())
        return
    try:
        _modules.set_options(args.module_option)
    except ValueError as e:
        parser.error(str(e))
    if args.no_modules:
        _modules.set_run_override(set())
    elif args.modules is not None:
        _modules.set_run_override({n.strip() for n in args.modules.split(",") if n.strip()})
    try:
        _paths.migrate_legacy()            # data kept inside the package by older versions
    except OSError as e:
        print(f"sdrexplorer: couldn't take over old data: {e}", file=sys.stderr)
    use_gpu = False if args.no_gpu else (True if args.gpu else None)
    if args.gpu and not CUDA_AVAILABLE:
        print("sdrexplorer: --gpu requested but gr-sdrexplorer_gpu is not "
             "available — falling back to CPU.", file=sys.stderr)

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    if hasattr(signal, "SIGUSR1"):           # a hang: `kill -USR1 <pid>` prints every thread's stack
        import faulthandler
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    # QtWebEngine (live map) needs shared GL contexts set before the app exists.
    if WEBENGINE_AVAILABLE:
        try:
            QtCore.QCoreApplication.setAttribute(
                QtCore.Qt.AA_ShareOpenGLContexts)
        except Exception:
            pass
    app = QtWidgets.QApplication(sys.argv[:1] + qt_args)   # keep only Qt's args
    # Force Qt's built-in Fusion style. Some Linux platform styles anchor the
    # QComboBox drop-down via SH_ComboBox_Popup positioning that goes stale after
    # a window-manager move (edge-snap/tile), so the list appears detached from
    # the combo. Fusion drops the list directly below the widget and re-anchors
    # reliably. The app's dark stylesheet still controls the look on top of it.
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    app.setApplicationName("sdrexplorer")
    # helps some Linux desktops group/label the taskbar entry
    app.setDesktopFileName("sdrexplorer")
    app.setWindowIcon(make_scanner_icon())   # taskbar / window icon
    win = ScannerApp(use_gpu=use_gpu)
    win.show()
    rc = app.exec_()
    # QtWebEngine (the live map) aborts on exit with "QThread: Destroyed while
    # thread is still running / Aborted (core dumped)". Its global browser/GPU
    # context owns an in-process QThread released only when the *C++* QApplication
    # is destroyed — and that happens at interpreter shutdown (Py_Finalize), in an
    # order that kills the thread while it is still running. Python can't force it
    # earlier: `del app` is a no-op because PyQt keeps its own reference (reachable
    # via QApplication.instance()), so the C++ object outlives every Python ref.
    #
    # closeEvent has already run to completion here — state saved, IQ recording
    # flushed, the SDR stopped, and every app-owned QThread join()ed — so there is
    # nothing left to clean up gracefully. Exit the process immediately, before the
    # fatal C++ static-teardown phase runs at all. os._exit skips Python atexit and
    # C++ destructors by design; the kernel reclaims the (already-flushed) fds and
    # the USB device. Only needed once QtWebEngine has initialized.
    #
    # Without QtWebEngine the same teardown can still crash: PyQt5 destroys the QApplication inside
    # Py_FinalizeEx after objects it points at are gone (seen in test_modules: Py_FinalizeEx ->
    # QtCore.abi3.so -> sip -> a freed address, about every other run). So always exit this way.
    _exit_trace("main: event loop returned")
    _exit_trace("main: os._exit (bypass C++ static teardown)")
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)


if __name__ == "__main__":
    main()
