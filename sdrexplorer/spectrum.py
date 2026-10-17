# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Combined spectrum + waterfall display widget.

Reads the FFT the engine already computes (``radio.probe``), so it adds no
flowgraph blocks. Top third is a live spectrum trace with a dB grid; the rest is
a scrolling color waterfall (newest row at the top). Shared across both:

  * frequency axis with labels along the bottom
  * scroll-wheel zoom on the frequency axis (anchored under the mouse cursor)
  * click anywhere to tune; hover shows the frequency under the cursor
  * a NARROW DC-spike notch so the center spike doesn't dominate either view

The probe FFT is fft-shifted, spanning [center-fs/2, center+fs/2] where
center = freq-OFFSET; the tuned signal sits at +OFFSET from center.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets

from .radio import (SAMP_RATE, OFFSET, NFFT, MAX_SELECTION_BW_HZ,  # SAMP_RATE: default/fallback only
                    SSB_LOW_CUT_HZ)

DISPLAY_DC_GUARD = 6_000         # narrow center notch (Hz) for the DC spike
# floor on the notch width in FFT bins. The probe FFT's window (blackman-harris,
# NFFT bins) leaks the DC spike into a handful of neighboring bins REGARDLESS of
# samp_rate -- that leakage width is fixed in bins, not Hz. DISPLAY_DC_GUARD
# converted via samp_rate/NFFT covers it fine at 2.4/4.8 Msps (5/3 bins each
# side) but collapses to 1 bin at 9.6+ Msps since bin width grows with
# samp_rate while NFFT stays fixed at 2048 -- too narrow to cover the real
# leakage, so the spike shows through. This floor keeps the notch at least as
# wide (in bins) as it is at the lowest capture rate.
DISPLAY_DC_GUARD_MIN_BINS = 5
MARKER_MIN_SNR = 12.0            # dB above noise for a channel label to show
MARKER_HISTORY_ROWS = 10         # recent waterfall rows checked for activity
M_LEFT, M_RIGHT, M_TOP, M_BOTTOM = 48, 8, 6, 20
MIN_SPAN_HZ = 200_000            # most zoomed-in frequency span
SPEC_FRAC = 0.34                 # fraction of the plot height for the trace
WF_ROWS = 320                    # waterfall history depth (rows)
WATERFALL_ROW_S = 0.05           # one row per refresh tick (self.timer, 20 Hz)
READOUT_REC_COLOR = "#40c4ff"    # recorded-frequency markers + the "recorded" readout line
REC_MARK_HALF, REC_MARK_DEPTH = 7, 12   # recorded marker: ▼ 14 px wide, 12 px tall


def _build_colormap():
    pts = [(0.00, (0, 0, 12)), (0.16, (0, 0, 90)), (0.36, (0, 90, 170)),
           (0.56, (0, 180, 120)), (0.72, (225, 210, 0)),
           (0.88, (235, 90, 0)), (1.00, (255, 255, 255))]
    lut = np.zeros((256, 3), np.uint8)
    for i, x in enumerate(np.linspace(0, 1, 256)):
        for (p0, c0), (p1, c1) in zip(pts, pts[1:]):
            if p0 <= x <= p1:
                t = (x - p0) / (p1 - p0) if p1 > p0 else 0.0
                lut[i] = [int(c0[j] + (c1[j] - c0[j]) * t) for j in range(3)]
                break
    return lut


_CMAP = _build_colormap()


def blank_dc(level, samp_rate=SAMP_RATE, guard=DISPLAY_DC_GUARD):
    """Return a copy of the fft-shifted power vector with a narrow notch across
    the center DC spike (linearly interpolated so the trace stays continuous)."""
    lv = np.asarray(level, dtype=np.float64).copy()
    n = lv.size
    if n < 8:
        return lv
    bins = max(DISPLAY_DC_GUARD_MIN_BINS, int(round(guard / (samp_rate / n))))
    c = n // 2
    a, b = max(1, c - bins), min(n - 1, c + bins + 1)
    lv[a:b] = np.linspace(lv[a - 1], lv[b], b - a)
    return lv


# standard demod bandwidths (kHz) a dragged marker edge snaps to
BW_SNAPS_HZ = [int(k * 1000) for k in
               (5, 6.25, 7.5, 8, 10, 12.5, 15, 20, 25, 50, 100, 150, 200)]
EDGE_GRAB_PX = 6                        # cursor-to-edge px to start a resize drag


class SpectrumWidget(QtWidgets.QWidget):
    # frequencies are "qlonglong" (64-bit): pyqtSignal(int) is a C++ 32-bit int and wraps
    # above 2^31 Hz = 2147.48 MHz
    tuned = QtCore.pyqtSignal("qlonglong")  # emitted with a frequency on click
    bandwidth_changed = QtCore.pyqtSignal(int)   # new demod bandwidth on drag-end
    selection_made = QtCore.pyqtSignal("qlonglong", int)  # right-click-drag: (center_hz, bw_hz)
    # right-click (no drag): (freq_hz, global QPoint, nearest annotation dict or None)
    context_requested = QtCore.pyqtSignal("qlonglong", object, object)

    def __init__(self, radio, parent=None):
        super().__init__(parent)
        self.radio = radio
        self.setMinimumHeight(240)
        self.setMouseTracking(True)
        self._data = None
        self._history = deque(maxlen=WF_ROWS)
        self._ymin, self._ymax = -110.0, -20.0
        self._mouse_x = None
        self._mouse_y = None
        self._span_hz = float(radio.samp_rate)
        self._view_center = None            # None => center on the tuned freq
        self._wf_buf = None                 # keep QImage backing bytes alive
        self._markers = []                  # [(freq_hz, label)] known channels
        self.show_markers = True            # overlay channel names on the display
        # annotations: labeled signals from any module (ui.annotate), by source; drawn merged
        # dicts; drawn like the memory labels but in their own color, with a
        # bandwidth bracket. label_source picks what the waterfall shows.
        self._detections = []
        # tags of signals that stopped: [{det..., "row": waterfall row count when last seen}]. Each
        # rides down the waterfall with the row it was last seen on until it scrolls out of view,
        # so a transmission that ended still shows what it was.
        self._trail_tags = []
        self._rows_total = 0                # waterfall rows ever appended (row clock for trail tags)
        self.label_source = "memories"      # "memories" | "annotations" | "both"
        self.det_text_color = "#ffffff"     # annotation label text (white on a dark box)
        self.det_tick_color = "#ff9f43"     # annotation bandwidth bracket
        self._overlays = []                  # module painters (api.Overlay), in .order
        # frequencies that already have recordings (recindex.build): a small badge per channel
        # along the spectrum/waterfall boundary. Settings ▸ Show recorded frequencies.
        self._recorded = []                 # [{freq_hz, labels, recordings, held_out}]
        self.show_recorded = True
        # channel-label style (user-adjustable via Settings ▸ Label style)
        self.marker_font_px = 12            # label + tick text size
        self.marker_text_color = "#ffffff"  # label text (white reads over any wf)
        self.marker_tick_color = "#39ff88"  # the vertical locator tick
        self._drag_edge = None              # 'L'/'R' while dragging a marker edge
        self._drag_bw = None                # live (snapped) bandwidth during a drag
        self._sel_x0 = None                 # right-click-drag sub-band selection
        self._sel_x1 = None                 # (pixel coords; None => no drag active)
        self._pending_sel = None            # (center_hz, bw_hz) while its record/snapshot menu is up
        self._trigger_win = None            # (center_hz, bw_hz, armed) while a recording trigger is set
        self._tx_probe = None               # (probe, samp_rate, center_hz) while transmitting
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._poll)
        self.timer.start(50)                # 20 Hz

    def pause(self):
        """Stop polling/repainting the FFT (e.g. while a scan sweeps, so the
        colormap+waterfall paint doesn't burn CPU on bands jumping around)."""
        self.timer.stop()

    def resume(self):
        if not self.timer.isActive():
            self.timer.start(50)

    def set_tx_display(self, probe, samp_rate, center_hz):
        """Switch the spectrum/waterfall to a TxChain's own FFT probe — while
        the paired ScannerRadio's source is stopped for TX (HackRF is
        half-duplex), so there'd otherwise be nothing but a frozen last-RX
        frame to show. Call clear_tx_display() when PTT releases."""
        self._tx_probe = (probe, float(samp_rate), int(center_hz))
        self._span_hz = float(samp_rate)    # zoom out to the whole TX capture
        self._view_center = None
        self._history.clear()
        self._trail_tags = []
        self._data = None
        self.update()

    def clear_tx_display(self):
        """Revert to the live RX display (no-op if TX display isn't active)."""
        if self._tx_probe is not None:
            self._tx_probe = None
            self._span_hz = float(self.radio.samp_rate)
            self._view_center = None
            self._history.clear()
            self._trail_tags = []
            self._data = None
            self.update()

    def set_recorded(self, entries):
        """Channels with recordings (recindex.build output), for the recorded-frequency badges."""
        self._recorded = list(entries or [])
        self.update()

    def recorded_near(self, freq_hz, px=REC_MARK_HALF):
        """The recorded-channel entry whose badge is within ``px`` pixels of ``freq_hz``."""
        if not (self.show_recorded and self._recorded):
            return None
        x = self._freq_to_x(freq_hz)
        best = None
        for e in self._recorded:
            d = abs(self._freq_to_x(e["freq_hz"]) - x)
            if d <= px and (best is None or d < best[0]):
                best = (d, e)
        return best[1] if best else None

    def _paint_recorded(self, qp, r, wf, vlo, vhi):
        """A ▼ at the top of the waterfall over every channel that already has a recording
        (training or held-out alike -- either way it isn't a new transmitter), so you can see
        what's still missing. Hover one for what was recorded there."""
        if not (self.show_recorded and self._recorded):
            return
        half, depth = REC_MARK_HALF, REC_MARK_DEPTH
        fill = QtGui.QColor(READOUT_REC_COLOR)
        edge = QtGui.QPen(QtGui.QColor(6, 8, 11, 230), 1)     # dark outline: readable on any wf
        y = wf.top()
        last = -1e9
        qp.setBrush(fill)
        qp.setPen(edge)
        for e in self._recorded:
            f = e["freq_hz"]
            if not (vlo <= f <= vhi):
                continue
            x = self._freq_to_x(f)
            if x - last < half:              # dense full-band captures: don't stack them
                continue
            last = x
            qp.drawPolygon(QtGui.QPolygon([QtCore.QPoint(x - half, y), QtCore.QPoint(x + half, y),
                                           QtCore.QPoint(x, y + depth)]))
        qp.setBrush(QtCore.Qt.NoBrush)

    def clear_history(self):
        """Drop the waterfall rows but keep the zoom (e.g. after seeking in a replay)."""
        self._history.clear()
        self._trail_tags = []
        self.update()

    def set_pending_selection(self, sel):
        """Keep showing a released drag selection, (center_hz, bw_hz), or None."""
        self._pending_sel = sel
        self.update()

    def set_trigger_window(self, win, armed=True):
        """Show a recording-trigger window ((center_hz, bw_hz), or None to clear): dashed amber
        edges + "◎ ARMED" while waiting, the usual selection style + "● TRIG REC" while recording."""
        self._trigger_win = None if win is None else (int(win[0]), int(win[1]), bool(armed))
        self.update()

    def waterfall_rows(self):
        """The waterfall history as a [rows, NFFT] float32 dB array, oldest
        first (saved next to an IQ snapshot); None while it is empty or showing TX."""
        if self._tx_probe is not None or not self._history:
            return None
        return np.stack(list(self._history))

    def reset_view(self):
        """Zoom all the way out to the full captured band and clear the
        waterfall history. Call after the radio's capture bandwidth changes —
        the old zoom span/history no longer line up with the new band."""
        self._span_hz = float(self.radio.samp_rate)
        self._view_center = None
        self._history.clear()
        self._trail_tags = []
        self._data = None
        self.update()

    def set_markers(self, markers):
        """markers: iterable of (freq_hz, label) for known channels — drawn as
        thin ticks + vertical name labels over the waterfall, brightened where a
        signal is present so an active line is identifiable."""
        self._markers = sorted(((int(f), str(lbl)) for f, lbl in markers),
                               key=lambda m: m[0])
        self.update()

    def add_overlay(self, overlay):
        """A module's waterfall painter (api.Overlay), drawn after the core's own overlays."""
        self._overlays = sorted(self._overlays + [overlay], key=lambda o: o.order)
        self.update()

    def remove_overlay(self, name):
        self._overlays = [o for o in self._overlays if o.name != name]
        self.update()

    def _paint_overlays(self, qp, r, wf, vlo, vhi):
        for o in self._overlays:
            try:
                if o.visible is None or o.visible():
                    qp.save()
                    try:
                        o.paint(qp, r, wf, vlo, vhi)
                    finally:
                        qp.restore()
            except Exception as e:                  # one bad painter must not blank the waterfall
                if not getattr(o, "_reported", False):
                    o._reported = True
                    print(f"[sdrexplorer] overlay {o.name} failed: {e}", flush=True)

    def set_annotations(self, source, items, trail=True):
        """A module's labeled signals (api.Annotation or their dicts) replace that source's previous set;
        the waterfall shows every source's together."""
        if not hasattr(self, "_ann"):
            self._ann = {}
        self._ann[source] = [a.as_dict() if hasattr(a, "as_dict") else dict(a, source=a.get("source") or source)
                             for a in items]
        merged = [d for lst in self._ann.values() for d in lst]
        self.set_detections(merged, trail=trail)

    def annotations(self):
        """Every source's current annotations, as dicts."""
        return list(self._detections)

    def set_detections(self, dets, trail=True):
        """Every source's annotations, merged (set_annotations): dicts with freq_hz, bw_hz,
        label, conf, confirmed (sync-word verified). Replaces the previous set.
        A labeled signal that is gone from the new set leaves its tag behind as a trail tag,
        which scrolls down with the waterfall row it was last seen on (see _paint_detections);
        ``trail=False`` (a module stopped / the band moved) drops them all instead -- the
        rows under them are no longer the same band."""
        dets = list(dets)
        if not trail:
            self._trail_tags = []
        else:
            for d in self._detections:
                if not self._trailable(d):
                    continue
                if any(self._same_signal(d, n) for n in dets):
                    continue
                self._trail_tags = [t for t in self._trail_tags if not self._same_signal(t, d)]
                self._trail_tags.append(dict(d, row=self._rows_total))
            # a signal that came back gets a fresh tag at the top; its old trail tag keeps scrolling
        self._detections = dets
        self.update()

    @staticmethod
    def _trailable(d):
        lab = d.get("label") or ""
        return d.get("trail", True) and lab not in ("?", "") and not lab.startswith("wide")

    @staticmethod
    def _same_signal(a, b):
        return abs(a["freq_hz"] - b["freq_hz"]) <= max(2_500, a.get("bw_hz", 0) / 2, b.get("bw_hz", 0) / 2)

    def detection_near(self, freq_hz, px=12):
        """The annotation under/near a frequency: inside its bandwidth, or within `px`
        screen pixels of its center. None if nothing close."""
        vlo, vhi = self._visible()
        hz_per_px = (vhi - vlo) / max(1, self._plot_rect().width())
        best, bestd = None, None
        for d in self._detections:
            dist = abs(d["freq_hz"] - freq_hz)
            if dist <= max(d.get("bw_hz", 0) / 2, px * hz_per_px) and (bestd is None or dist < bestd):
                best, bestd = d, dist
        return best

    def set_label_source(self, source):
        self.label_source = source if source in ("memories", "annotations", "both") else "memories"
        self.update()

    def set_label_style(self, font_px=None, text_color=None, tick_color=None):
        """Set the waterfall channel-label appearance (from Settings). Any arg
        left None keeps its current value. Repaints so the change shows at once."""
        if font_px is not None:
            self.marker_font_px = int(font_px)
        if text_color is not None:
            self.marker_text_color = str(text_color)
        if tick_color is not None:
            self.marker_tick_color = str(tick_color)
        self.update()

    # -- data ----------------------------------------------------------------
    def _poll(self):
        if self._tx_probe is not None:
            probe, samp_rate, _ = self._tx_probe
        else:
            probe, samp_rate = self.radio.probe, self.radio.samp_rate
        lv = np.asarray(probe.level(), dtype=np.float64)
        if lv.size != NFFT or not np.isfinite(lv).any():
            return
        self._data = blank_dc(lv, samp_rate=samp_rate)
        self._history.append(self._data.astype(np.float32))
        self._rows_total += 1
        band = (int(getattr(self.radio, "band_center_hz", 0)), int(samp_rate))
        if band != getattr(self, "_trail_band", band):
            self._trail_tags = []           # the band moved: older rows show other frequencies
        self._trail_band = band
        if self._trail_tags:                # drop tags that have scrolled off the bottom
            self._trail_tags = [t for t in self._trail_tags
                                if self._rows_total - t["row"] < len(self._history)]
        finite = self._data[np.isfinite(self._data)]
        dmax = float(finite.max())
        dfloor = float(np.percentile(finite, 20))
        ymax_t = math.ceil((dmax + 6) / 10.0) * 10
        ymin_t = math.floor((dfloor - 10) / 10.0) * 10
        if ymax_t - ymin_t < 50:
            ymin_t = ymax_t - 50
        self._ymax += 0.2 * (ymax_t - self._ymax)
        self._ymin += 0.2 * (ymin_t - self._ymin)
        self.update()

    # -- geometry helpers ----------------------------------------------------
    def _fft_center(self):
        return self._tx_probe[2] if self._tx_probe is not None else self.radio.band_center_hz

    def _demod_freq(self):
        """The frequency the demod marker/edge-hit-test should use: the TX
        chain's actual transmit frequency while its display is active (may
        differ from radio.demod_hz under a repeater split), else the RX
        demod frequency."""
        return self._tx_probe[2] if self._tx_probe is not None else self.radio.demod_hz

    def _active_samp_rate(self):
        return self._tx_probe[1] if self._tx_probe is not None else self.radio.samp_rate

    def _visible(self):
        c = self._fft_center()
        samp_rate = self._active_samp_rate()
        fmin, fmax = c - samp_rate / 2, c + samp_rate / 2
        span = min(self._span_hz, samp_rate)
        vc = self._view_center if self._view_center is not None else c
        half = span / 2
        vc = max(fmin + half, min(fmax - half, vc))
        return vc - half, vc + half

    def _plot_rect(self):
        return QtCore.QRect(M_LEFT, M_TOP,
                            max(1, self.width() - M_LEFT - M_RIGHT),
                            max(1, self.height() - M_TOP - M_BOTTOM))

    def _bands(self):
        r = self._plot_rect()
        spec_h = int(r.height() * SPEC_FRAC)
        spec = QtCore.QRect(r.left(), r.top(), r.width(), spec_h)
        wf = QtCore.QRect(r.left(), r.top() + spec_h, r.width(),
                          r.height() - spec_h)
        return r, spec, wf

    def _x_to_freq(self, x):
        r = self._plot_rect()
        vlo, vhi = self._visible()
        frac = min(max((x - r.left()) / r.width(), 0.0), 1.0)
        return int(round(vlo + frac * (vhi - vlo)))

    def _freq_to_x(self, f):
        """Hz -> pixel x (unclamped, for marker edges + hit-testing)."""
        r = self._plot_rect()
        vlo, vhi = self._visible()
        return int(round(r.left() + (f - vlo) / (vhi - vlo) * r.width()))

    def _marker_bw(self):
        """The bandwidth the marker should draw at (the live dragged value while
        resizing, else the radio's current bandwidth)."""
        return self._drag_bw if self._drag_bw is not None else self.radio.current_bw()

    def _marker_edges(self):
        """(lo_hz, hi_hz) offsets from demod_hz for the marker/edge-drag
        geometry — one-sided for SSB (matches DemodChain's band-pass, see
        SSB_LOW_CUT_HZ), symmetric ± bw/2 for every other mode. Uses the live
        dragged bandwidth while resizing, same as _marker_bw()."""
        bw = self._marker_bw()
        sb = getattr(self.radio.mode, "sideband", None)
        if sb == "usb":
            return SSB_LOW_CUT_HZ, SSB_LOW_CUT_HZ + bw
        if sb == "lsb":
            return -(SSB_LOW_CUT_HZ + bw), -SSB_LOW_CUT_HZ
        return -bw / 2, bw / 2

    def _edge_hit(self, x):
        """Return 'L'/'R' if pixel ``x`` is near a marker edge, else None."""
        demod = self._demod_freq()
        lo, hi = self._marker_edges()
        if abs(x - self._freq_to_x(demod + lo)) <= EDGE_GRAB_PX:
            return 'L'
        if abs(x - self._freq_to_x(demod + hi)) <= EDGE_GRAB_PX:
            return 'R'
        return None

    def _snap_bw(self, bw):
        """Snap a raw bandwidth (Hz) to a resolution appropriate for the
        current mode: SSB's and CW's passbands are a continuously variable filter
        width (not a fixed channel spacing), so they snap to the mode's bw_step
        (1 kHz SSB, 50 Hz CW) instead of the fixed BW_SNAPS_HZ list every other
        mode uses."""
        step = getattr(self.radio.mode, "bw_step", None)
        if step:
            return max(step, int(round(bw / float(step)) * step))
        return min(BW_SNAPS_HZ, key=lambda s: abs(s - bw))

    def _visible_bins(self, vlo, vhi):
        c = self._fft_center()
        samp_rate = self._active_samp_rate()
        binhz = samp_rate / NFFT
        lo0 = c - samp_rate / 2
        i0 = max(0, int((vlo - lo0) / binhz) - 1)
        i1 = min(NFFT, int((vhi - lo0) / binhz) + 2)
        return i0, i1

    # -- painting ------------------------------------------------------------
    def paintEvent(self, _ev):
        qp = QtGui.QPainter(self)
        w, h = self.width(), self.height()
        qp.fillRect(0, 0, w, h, QtGui.QColor("#0a0c0f"))
        r, spec, wf = self._bands()
        if self._data is None:
            qp.setPen(QtGui.QColor("#5a636e"))
            qp.drawText(M_LEFT + 6, M_TOP + 16, "spectrum: waiting for IQ…")
            return

        vlo, vhi = self._visible()
        i0, i1 = self._visible_bins(vlo, vhi)
        span = max(1.0, self._ymax - self._ymin)

        # --- waterfall -------------------------------------------------------
        self._paint_waterfall(qp, wf, i0, i1)

        # --- spectrum grid + dB labels --------------------------------------
        qp.fillRect(spec, QtGui.QColor("#0c0f14"))
        step = 10 if span <= 80 else 20
        db = math.ceil(self._ymin / step) * step
        while db <= self._ymax:
            y = int(spec.bottom() - (db - self._ymin) / span * spec.height())
            qp.setPen(QtGui.QPen(QtGui.QColor("#1c2129"), 1))
            qp.drawLine(spec.left(), y, spec.right(), y)
            qp.setPen(QtGui.QColor("#7c8896"))
            qp.drawText(2, y + 4, f"{int(db):>4}")
            db += step
        qp.setPen(QtGui.QColor("#7c8896"))
        qp.drawText(2, spec.top() + 10, "dB")

        # --- spectrum trace --------------------------------------------------
        samp_rate = self._active_samp_rate()
        binhz = samp_rate / NFFT
        lo0 = self._fft_center() - samp_rate / 2
        pts = []
        for i in range(i0, i1):
            f = lo0 + i * binhz
            x = r.left() + (f - vlo) / (vhi - vlo) * r.width()
            y = spec.bottom() - (self._data[i] - self._ymin) / span * spec.height()
            pts.append(QtCore.QPointF(x, y))
        if len(pts) >= 2:
            qp.setPen(QtGui.QPen(QtGui.QColor("#39ff88"), 1))
            qp.setClipRect(spec)
            qp.drawPolyline(QtGui.QPolygonF(pts))
            qp.setClipping(False)

        # --- frequency grid + labels ----------------------------------------
        ndiv = 6
        dec = 4 if (vhi - vlo) < 1_000_000 else 3
        for k in range(ndiv + 1):
            fx = vlo + (vhi - vlo) * k / ndiv
            x = int(r.left() + r.width() * k / ndiv)
            qp.setPen(QtGui.QPen(QtGui.QColor("#243040"), 1))
            qp.drawLine(x, spec.top(), x, r.bottom())
            qp.setPen(QtGui.QColor("#7c8896"))
            qp.drawText(min(max(r.left(), x - 24), w - 50), h - 6, f"{fx/1e6:.{dec}f}")

        # --- known-channel names / modules' annotations ----------------------
        if self.label_source in ("memories", "both"):
            self._paint_markers(qp, r, wf, vlo, vhi)
        if self.label_source in ("annotations", "both"):
            self._paint_detections(qp, r, wf, vlo, vhi)
        self._paint_recorded(qp, r, wf, vlo, vhi)
        self._paint_overlays(qp, r, wf, vlo, vhi)

        # --- demod marker (bandwidth-wide, draggable edges) + hover ---------
        demod = self._demod_freq()
        bw = self._marker_bw()             # live dragged bw while resizing
        lo, hi = self._marker_edges()      # one-sided for SSB, ± bw/2 otherwise
        if vlo <= demod <= vhi:
            def fx(f):
                return int(r.left() + (f - vlo) / (vhi - vlo) * r.width())
            x0 = max(r.left(), fx(demod + lo))
            x1 = min(r.right(), fx(demod + hi))
            xc = fx(demod)
            # translucent band across the full height showing the demod passband
            qp.fillRect(QtCore.QRect(x0, r.top(), max(2, x1 - x0), r.height()),
                        QtGui.QColor(47, 109, 246, 70))
            # brighten the edge being dragged (or hovered) as a grab hint
            edge_pen = QtGui.QPen(QtGui.QColor("#2f6df6"), 1)
            hot_pen = QtGui.QPen(QtGui.QColor("#9dc0ff"), 3)
            hover = self._drag_edge or (self._edge_hit(self._mouse_x)
                                        if self._mouse_x is not None else None)
            qp.setPen(hot_pen if hover == 'L' else edge_pen)
            qp.drawLine(x0, r.top(), x0, r.bottom())
            qp.setPen(hot_pen if hover == 'R' else edge_pen)
            qp.drawLine(x1, r.top(), x1, r.bottom())
            qp.setPen(QtGui.QPen(QtGui.QColor("#9dc0ff"), 1, QtCore.Qt.DashLine))
            qp.drawLine(xc, r.top(), xc, r.bottom())
            if self._drag_edge:            # show the live bandwidth while dragging
                qp.setPen(QtGui.QColor("#e6edf3"))
                qp.drawText(min(x1 + 4, r.right() - 60), r.top() + 12,
                            f"{bw/1000:.4g} kHz")

        # --- right-click-drag sub-band selection (record / snapshot) --------
        # Shown while actively dragging, while the record-or-snapshot menu for
        # it is open, AND while a sub-band recording made from a prior drag is
        # still running (so the selection doesn't vanish the moment the mouse
        # button is released).
        sel_edges = sel_label = None
        if self._sel_x0 is not None:
            sx0, sx1 = sorted((self._sel_x0, self._sel_x1))
            cap = min(MAX_SELECTION_BW_HZ, self.radio.samp_rate // 2)
            sel_bw = min(self._x_to_freq(sx1) - self._x_to_freq(sx0), cap)
            sel_edges = (sx0, sx1)
            sel_label = f"{sel_bw/1000:.4g} kHz sel"
        else:
            active = self._pending_sel or self.radio.subband_recording_range()
            if active:
                center, bw = active
                x0 = max(r.left(), self._freq_to_x(center - bw / 2))
                x1 = min(r.right(), self._freq_to_x(center + bw / 2))
                if x1 > r.left() and x0 < r.right():   # at least partly visible
                    sel_edges = (x0, x1)
                    trig_rec = self._trigger_win is not None and not self._trigger_win[2]
                    sel_label = (f"{bw/1000:.4g} kHz sel" if self._pending_sel
                                 else f"● TRIG REC {bw/1000:.4g} kHz" if trig_rec
                                 else f"● REC {bw/1000:.4g} kHz")
            elif self._trigger_win is not None and self._trigger_win[2]:
                # armed and waiting: dashed edges, light fill, so it reads as "watching"
                center, bw, _ = self._trigger_win
                x0 = max(r.left(), self._freq_to_x(center - bw / 2))
                x1 = min(r.right(), self._freq_to_x(center + bw / 2))
                if x1 > r.left() and x0 < r.right():
                    qp.fillRect(QtCore.QRect(x0, r.top(), max(2, x1 - x0), r.height()),
                                QtGui.QColor(240, 180, 41, 28))
                    pen = QtGui.QPen(QtGui.QColor("#f0b429"), 2, QtCore.Qt.DashLine)
                    qp.setPen(pen)
                    qp.drawLine(x0, r.top(), x0, r.bottom())
                    qp.drawLine(x1, r.top(), x1, r.bottom())
                    qp.setPen(QtGui.QColor("#f0b429"))
                    qp.drawText(min(x1 + 4, r.right() - 110), r.top() + 12,
                                f"◎ ARMED {bw/1000:.4g} kHz")
        if sel_edges is not None:
            sx0, sx1 = sel_edges
            qp.fillRect(QtCore.QRect(sx0, r.top(), max(2, sx1 - sx0), r.height()),
                        QtGui.QColor(255, 159, 10, 70))
            sel_pen = QtGui.QPen(QtGui.QColor("#ff9f0a"), 2)
            qp.setPen(sel_pen)
            qp.drawLine(sx0, r.top(), sx0, r.bottom())
            qp.drawLine(sx1, r.top(), sx1, r.bottom())
            qp.setPen(QtGui.QColor("#ffb340"))
            qp.drawText(min(sx1 + 4, r.right() - 90), r.top() + 12, sel_label)

        if self._mouse_x is not None and r.left() <= self._mouse_x <= r.right():
            f = self._x_to_freq(self._mouse_x)
            qp.setPen(QtGui.QPen(QtGui.QColor("#f0b429"), 1))
            qp.drawLine(self._mouse_x, r.top(), self._mouse_x, r.bottom())
            self._paint_cursor_readout(qp, r, f)
        # last, so nothing (labels, trail tags, overlays, markers, the cursor) draws over it
        if getattr(self, "_status_box", None):
            self._paint_status_box(qp, wf, self._status_box)

    def _paint_cursor_readout(self, qp, r, f):
        """Frequency (and what's recorded there) in a box beside the cursor, so it's read
        where you're looking. Flips to the cursor's left near the right edge and stays inside
        the plot vertically."""
        lines = [(f"{f/1e6:.4f} MHz", "#ffe08a", True)]
        rec = self.recorded_near(f)
        if rec:
            from .recindex import describe, last_text
            lines.append((f"▼ recorded: {describe(rec)}  @ {rec['freq_hz']/1e6:.4f}",
                          READOUT_REC_COLOR, False))
            when = last_text(rec)
            if when:
                lines.append((f"   {when}", READOUT_REC_COLOR, False))
        if self._span_hz < self._active_samp_rate():
            lines.append((f"span {self._span_hz/1e6:.2f} MHz", "#9aa5b1", False))
        base = QtGui.QFont(qp.font())
        base.setPixelSize(max(13, int(self.marker_font_px) + 2))
        big = QtGui.QFont(base)
        big.setPixelSize(base.pixelSize() + 3)
        big.setBold(True)
        fms = [QtGui.QFontMetrics(big if b else base) for _t, _c, b in lines]
        w = max(fm.horizontalAdvance(t) for fm, (t, _c, _b) in zip(fms, lines)) + 14
        h = sum(fm.height() for fm in fms) + 10
        x = self._mouse_x + 14
        if x + w > r.right():
            x = self._mouse_x - 14 - w
        y = (self._mouse_y if self._mouse_y is not None else r.top()) + 16
        y = max(r.top() + 2, min(r.bottom() - h - 2, y))
        box = QtCore.QRect(int(x), int(y), int(w), int(h))
        qp.fillRect(box, QtGui.QColor(6, 8, 11, 225))
        qp.setPen(QtGui.QPen(QtGui.QColor("#3a4350"), 1))
        qp.drawRect(box)
        ty = box.top() + 5
        for fm, (t, col, b) in zip(fms, lines):
            qp.setFont(big if b else base)
            qp.setPen(QtGui.QColor(col))
            qp.drawText(box.left() + 7, ty + fm.ascent(), t)
            ty += fm.height()

    def _paint_markers(self, qp, r, wf, vlo, vhi):
        """Label a known channel ONLY while a strong signal is present at its
        frequency (checked over the last few waterfall rows so it holds through
        brief gaps and vanishes when the transmission ends). No labels for the
        noise floor."""
        if not self.show_markers or not self._markers or self._data is None:
            return
        samp_rate = self._active_samp_rate()
        binhz = samp_rate / NFFT
        lo0 = self._fft_center() - samp_rate / 2
        noise = float(np.percentile(self._data, 35))
        thresh = noise + MARKER_MIN_SNR
        # recent activity: max over the last few rows so a momentary dip doesn't
        # drop the label, but it clears within ~0.5 s of the signal stopping
        recent = list(self._history)[-MARKER_HISTORY_ROWS:]
        font = QtGui.QFont(qp.font())
        font.setPixelSize(max(6, int(self.marker_font_px)))
        qp.setFont(font)
        hot = QtGui.QColor(self.marker_tick_color)    # locator tick
        text_col = QtGui.QColor(self.marker_text_color)  # label text
        shadow = QtGui.QColor(6, 8, 11, 220)   # dark halo behind any waterfall color
        last_label_x = -1e9
        for freq, label in self._markers:
            if not (vlo <= freq <= vhi):
                continue
            bi = int((freq - lo0) / binhz)
            a, b = max(0, bi - 4), min(NFFT, bi + 5)
            if b <= a:
                continue
            peak = max((float(row[a:b].max()) for row in recent),
                       default=float(self._data[a:b].max()))
            if peak < thresh:                 # only strong, present signals
                continue
            x = int(r.left() + (freq - vlo) / (vhi - vlo) * r.width())
            qp.setPen(QtGui.QPen(hot, 1))
            qp.drawLine(x, wf.top(), x, wf.bottom())
            if x - last_label_x >= 12:        # skip labels that would overlap
                last_label_x = x
                qp.save()
                qp.translate(x + 3, wf.top() + 4)
                qp.rotate(90)                 # vertical text, reading downward
                txt = label[:24]
                qp.setPen(shadow)             # 1px dark halo for legibility
                qp.drawText(1, 1, txt)
                qp.setPen(text_col)
                qp.drawText(0, 0, txt)
                qp.restore()

    def _paint_detections(self, qp, r, wf, vlo, vhi):
        """Annotations: vertical white text on a dark box, placed just
        OUTSIDE the signal's upper band edge so it never sits on the signal's
        own (bright) waterfall trace. A bracket across the top shows the
        occupied bandwidth. Modules only report signals they have seen
        recently, so there is no activity gating here. A trailing '✓' marks a
        sync-word-confirmed label; low-confidence labels get a '?'. Signals that stopped keep
        their tag on the row they were last seen on (_paint_trail_tags)."""
        if not self.show_markers or not (self._detections or self._trail_tags):
            return
        font = QtGui.QFont(qp.font())
        font.setPixelSize(max(6, int(self.marker_font_px)))
        font.setBold(True)
        qp.setFont(font)
        fm = QtGui.QFontMetrics(font)
        tick = QtGui.QColor(self.det_tick_color)
        text_col = QtGui.QColor(self.det_text_color)
        box = QtGui.QColor(6, 8, 11, 200)       # dark backing so white reads over any heat
        span = max(1.0, vhi - vlo)
        gap = 3                                  # px between the band edge and the label
        last_label_right = -1e9
        for d in sorted(self._detections, key=lambda d: d["freq_hz"]):
            f = d["freq_hz"]
            if not (vlo <= f <= vhi):
                continue
            half = d.get("bw_hz", 0) / 2
            x0 = int(r.left() + (f - half - vlo) / span * r.width())
            x1 = int(r.left() + (f + half - vlo) / span * r.width())
            if x1 - x0 >= 3:                   # bandwidth bracket along the waterfall top
                qp.setPen(QtGui.QPen(tick, 2))
                qp.drawLine(x0, wf.top() + 1, x1, wf.top() + 1)
                qp.drawLine(x0, wf.top() + 1, x0, wf.top() + 5)
                qp.drawLine(x1, wf.top() + 1, x1, wf.top() + 5)
            lab = d["label"]
            unsure = d.get("conf", 1) < 0.7 and lab != "?" and not lab.startswith("wide")
            txt = (lab + (" ✓" if d.get("confirmed") else " ?" if unsure else ""))[:24]
            th = fm.height()                     # rotated text: its height is the label's width on screen
            lx = max(x1, int(r.left() + (f - vlo) / span * r.width()) + 1) + gap
            if lx + th > r.right():              # no room right of the signal: use its lower edge
                lx = x0 - gap - th
            if lx < last_label_right + 2:        # would overlap the previous label: skip
                continue
            last_label_right = lx + th
            tw = fm.horizontalAdvance(txt)
            qp.save()
            qp.translate(lx + th, wf.top() + 8)  # rotate 90: text runs downward, occupying [lx, lx+th]
            qp.rotate(90)
            qp.fillRect(QtCore.QRect(-2, 0, tw + 4, th), box)
            qp.setPen(text_col)
            qp.drawText(0, fm.ascent(), txt)
            qp.restore()
        self._paint_trail_tags(qp, r, wf, vlo, vhi, fm, box)

    def _paint_trail_tags(self, qp, r, wf, vlo, vhi, fm, box):
        """Tags of signals that stopped (set_detections): drawn on the waterfall row the signal
        was last seen on, so they scroll down with it and disappear when that row leaves the view.
        Placed like a live tag (just right of the signal), a little dimmer, with a short tick
        across the signal's bandwidth at that row."""
        if not self._trail_tags or not self._history:
            return
        span = max(1.0, vhi - vlo)
        row_h = wf.height() / max(1, len(self._history))
        tick = QtGui.QColor(self.det_tick_color)
        tick.setAlpha(170)
        text_col = QtGui.QColor(self.det_text_color)
        text_col.setAlpha(210)
        th = fm.height()
        for t in self._trail_tags:
            f = t["freq_hz"]
            if not (vlo <= f <= vhi):
                continue
            y = int(wf.top() + (self._rows_total - t["row"]) * row_h)
            if y >= wf.bottom() - 4:
                continue
            half = t.get("bw_hz", 0) / 2
            x0 = int(r.left() + (f - half - vlo) / span * r.width())
            x1 = int(r.left() + (f + half - vlo) / span * r.width())
            if x1 - x0 >= 3:
                qp.setPen(QtGui.QPen(tick, 2))
                qp.drawLine(x0, y, x1, y)
            lab = t["label"]
            txt = (lab + (" ✓" if t.get("confirmed") else ""))[:24]
            tw = fm.horizontalAdvance(txt)
            lx = max(x1, int(r.left() + (f - vlo) / span * r.width()) + 1) + 3
            if lx + th > r.right():
                lx = x0 - 3 - th
            tw = min(tw, max(0, wf.bottom() - y - 6))     # don't run past the waterfall bottom
            if tw <= 0:
                continue
            qp.save()
            qp.translate(lx + th, y + 2)
            qp.rotate(90)
            qp.setClipRect(QtCore.QRect(-2, 0, tw + 4, th))
            qp.fillRect(QtCore.QRect(-2, 0, tw + 4, th), box)
            qp.setPen(text_col)
            qp.drawText(0, fm.ascent(), txt)
            qp.restore()

    def set_status_box(self, lines):
        """A module's status box, bottom-right of the waterfall (ui.set_status_box): [(text, "#rrggbb")],
        or None to hide it."""
        self._status_box = list(lines) if lines else None
        self.update()

    def _paint_status_box(self, qp, wf, lines):
        font = QtGui.QFont("DejaVu Sans Mono")
        font.setPixelSize(11)
        qp.setFont(font)
        fm = QtGui.QFontMetrics(font)
        lh = fm.height()
        w = max(fm.horizontalAdvance(txt) for txt, _ in lines) + 12
        h = lh * len(lines) + 8
        box = QtCore.QRect(wf.right() - w - 4, wf.bottom() - h - 4, w, h)
        if self._mouse_x is not None and self._mouse_x >= box.left() - 8:
            box.moveLeft(wf.left() + 4)            # out of the way while the cursor is over it
        qp.fillRect(box, QtGui.QColor(6, 8, 11, 210))
        qp.setPen(QtGui.QPen(QtGui.QColor("#2a2f38"), 1))
        qp.drawRect(box)
        for i, (txt, col) in enumerate(lines):
            qp.setPen(QtGui.QColor(col or "#e6edf3"))
            qp.drawText(box.left() + 6, box.top() + 4 + fm.ascent() + i * lh, txt)

    def _paint_waterfall(self, qp, wf, i0, i1):
        if not self._history or i1 <= i0 or wf.height() < 2:
            qp.fillRect(wf, QtGui.QColor("#0c0f14"))
            return
        rows = list(self._history)          # oldest..newest
        rows.reverse()                       # newest first (top row)
        cols = i1 - i0
        stride = max(1, cols // 1024)         # cap width for speed
        arr = np.empty((len(rows), len(range(0, cols, stride))), np.float32)
        for ri, row in enumerate(rows):
            arr[ri] = row[i0:i1][::stride]
        span = max(1.0, self._ymax - self._ymin)
        p01 = np.clip((arr - self._ymin) / span, 0.0, 1.0)
        idx = (p01 * 255).astype(np.uint8)
        rgb = _CMAP[idx]                      # (H, W, 3) uint8
        rgb = np.ascontiguousarray(rgb)
        h, w = rgb.shape[0], rgb.shape[1]
        self._wf_buf = rgb.tobytes()          # keep alive during draw
        img = QtGui.QImage(self._wf_buf, w, h, 3 * w,
                           QtGui.QImage.Format_RGB888)
        qp.drawImage(wf, img)                 # scales to the band

    # -- interaction ---------------------------------------------------------
    def mousePressEvent(self, ev):
        if self._tx_probe is not None:
            return    # read-only while showing the TX signal (RX source is stopped)
        r = self._plot_rect()
        if ev.button() == QtCore.Qt.RightButton:
            # start a sub-band selection drag, independent of the demod marker
            if r.left() <= ev.x() <= r.right() and r.top() <= ev.y() <= r.bottom():
                self._sel_x0 = self._sel_x1 = ev.x()
            return
        if not (r.left() <= ev.x() <= r.right() and r.top() <= ev.y() <= r.bottom()):
            return
        edge = self._edge_hit(ev.x())        # grab a marker edge -> resize, not tune
        if edge:
            self._drag_edge = edge
            self._drag_bw = self.radio.current_bw()
            return
        self.tuned.emit(self._x_to_freq(ev.x()))

    def mouseMoveEvent(self, ev):
        self._mouse_x = ev.x()
        self._mouse_y = ev.y()
        if self._sel_x0 is not None and (ev.buttons() & QtCore.Qt.RightButton):
            self._sel_x1 = ev.x()
            self.update()
            return
        if self._drag_edge and (ev.buttons() & QtCore.Qt.LeftButton):
            half = abs(self._x_to_freq(ev.x()) - self.radio.demod_hz)
            self._drag_bw = self._snap_bw(max(1_000, int(2 * half)))
            self.setCursor(QtCore.Qt.SizeHorCursor)
        else:                                # hover feedback near an edge
            self.setCursor(QtCore.Qt.SizeHorCursor if self._edge_hit(ev.x())
                           else QtCore.Qt.ArrowCursor)
        self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == QtCore.Qt.RightButton and self._sel_x0 is not None:
            x0, x1 = sorted((self._sel_x0, self._sel_x1))
            self._sel_x0 = self._sel_x1 = None
            if x1 - x0 >= EDGE_GRAB_PX:       # a real drag: sub-band recording selection
                f0, f1 = self._x_to_freq(x0), self._x_to_freq(x1)
                cap = min(MAX_SELECTION_BW_HZ, self.radio.samp_rate // 2)
                bw = min(f1 - f0, cap)
                self.selection_made.emit(int((f0 + f1) // 2), int(bw))
            else:                              # a plain right-click: context menu (Label as...)
                f = self._x_to_freq(ev.x())
                self.context_requested.emit(f, ev.globalPos(), self.detection_near(f))
            self.update()
            return
        if self._drag_edge is not None:
            bw = self._drag_bw
            self._drag_edge = None
            self._drag_bw = None
            if bw:
                self.bandwidth_changed.emit(int(bw))   # apply on drag-end
            self.update()

    def cancel_selection_drag(self):
        """Abort an in-progress right-click-drag selection without starting a
        recording (e.g. on Esc). Returns True if a drag was actually live."""
        if self._sel_x0 is not None:
            self._sel_x0 = self._sel_x1 = None
            self.update()
            return True
        return False

    def leaveEvent(self, _ev):
        self._mouse_x = None
        self._mouse_y = None
        if not self._drag_edge:
            self.setCursor(QtCore.Qt.ArrowCursor)
        self.update()

    def wheelEvent(self, ev):
        # Zoom in/out on the FREQUENCY axis, anchored under the mouse pointer
        # so the frequency you're hovering over stays put on screen. Scaled by
        # the actual wheel delta (one 120-unit notch = one step) so it's smooth
        # on both notched mice and high-resolution wheels/trackpads.
        delta = ev.angleDelta().y()
        if not delta:
            return
        steps = delta / 120.0                     # up (>0) = zoom in
        factor = 0.9 ** steps                     # gentle 10%/notch (was 20–25%)
        samp_rate = self._active_samp_rate()
        anchor_freq = self._x_to_freq(ev.x())
        r = self._plot_rect()
        frac = min(max((ev.x() - r.left()) / r.width(), 0.0), 1.0)
        self._span_hz = max(MIN_SPAN_HZ, min(float(samp_rate),
                                             self._span_hz * factor))
        # full zoom-out reverts to the band center; otherwise keep the
        # frequency under the cursor fixed on screen
        self._view_center = (None if self._span_hz >= samp_rate
                             else anchor_freq + self._span_hz * (0.5 - frac))
        self.update()
        ev.accept()
