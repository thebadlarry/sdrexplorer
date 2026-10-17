# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""FCC license search window (View ▸ FCC license search…, or Search FCC data… next to Download).

Searches the downloaded FCC ULS data (providers.fcc_uls) without importing it: Where + radius,
frequency range, licensee / call sign / city text, service and emission type. Results show the
emission type by name ("NFM voice (12.5 kHz)", … from fcc_uls.emission_kind; the
raw designators are in their own column and in the tooltip), licensee, place and distance.
Double-click tunes a row; "Add selected → bank…" copies rows into a bank (the app's usual
copy dialog). The first search builds the SQLite index next to the .dat files (~30 s for LMpriv);
after that each search takes about a second at most."""
from __future__ import annotations

import os

from PyQt5 import QtCore, QtWidgets

from .providers import fcc_uls, geocode

LIMIT = 5000                      # rows shown; the status line gives the full match count
COLS = ["Freq (MHz)", "Emission type", "Licensee", "Place", "Call", "Service", "Designators",
        "Dist (mi)"]


class _NumItem(QtWidgets.QTableWidgetItem):
    """Shows its text, sorts by the number in UserRole+1 (blank sorts last)."""

    def __lt__(self, other):
        a, b = self.data(QtCore.Qt.UserRole + 1), other.data(QtCore.Qt.UserRole + 1)
        if a is None or b is None:
            return a is not None
        return a < b


class IndexWorker(QtCore.QThread):
    """Build (or rebuild) the search index off the UI thread."""
    progress = QtCore.pyqtSignal(str)
    done = QtCore.pyqtSignal()
    failed = QtCore.pyqtSignal(str)

    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.path = path

    def run(self):
        try:
            fcc_uls.build_index(self.path, log=self.progress.emit)
        except Exception as e:
            self.failed.emit(f"building the search index failed: {e}")
            return
        self.done.emit()


class SearchWorker(QtCore.QThread):
    """Resolve Where (Nominatim can be slow) and run one search."""
    done = QtCore.pyqtSignal(list, int, str)       # results, total matches, Where label
    failed = QtCore.pyqtSignal(str)

    def __init__(self, path, where, radius_km, fmin_hz, fmax_hz, text, service, emission,
                 parent=None):
        super().__init__(parent)
        self.path, self.where, self.radius_km = path, where, radius_km
        self.fmin_hz, self.fmax_hz, self.text, self.service = fmin_hz, fmax_hz, text, service
        self.emission = emission

    def run(self):
        lat = lon = None
        label = ""
        if self.where:
            loc = geocode.resolve(self.where)
            if not loc:
                self.failed.emit(f"could not resolve “{self.where}”")
                return
            lat, lon, label = loc
        try:
            res, total = fcc_uls.search(self.path, lat, lon, self.radius_km if self.where else None,
                                        self.fmin_hz, self.fmax_hz, self.text, self.service,
                                        self.emission, limit=LIMIT)
        except Exception as e:
            self.failed.emit(f"search failed: {e}")
            return
        self.done.emit(res, total, label)


class UlsSearchWindow(QtWidgets.QWidget):
    """Owned by the ScannerApp (``app``); searches ``path`` (the app's ULS_DIR)."""

    def __init__(self, app, path):
        super().__init__(app, QtCore.Qt.Window)
        self.app = app
        self.path = path
        self.setWindowTitle("FCC license search")
        self.resize(1000, 600)
        self._results = []
        self._index_worker = None
        self._search_worker = None
        self._search_after_index = False
        self._build()

    # -- UI ------------------------------------------------------------------------
    def _build(self):
        lay = QtWidgets.QVBoxLayout(self)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Where:"))
        self.where = QtWidgets.QLineEdit()
        self.where.setPlaceholderText("zip · city, ST · lat,lon  (blank = anywhere)")
        self.where.setText(self.app.loc_edit.text().strip())
        self.where.returnPressed.connect(self.search)
        row.addWidget(self.where, 2)
        row.addWidget(QtWidgets.QLabel("within"))
        self.radius = QtWidgets.QSpinBox()
        self.radius.setRange(1, 250)
        self.radius.setValue(self.app.radius_spin.value())
        self.radius.setSuffix(" mi")
        row.addWidget(self.radius)
        row.addWidget(QtWidgets.QLabel("Freq:"))
        self.fmin = QtWidgets.QLineEdit()
        self.fmin.setPlaceholderText("from MHz")
        self.fmin.setFixedWidth(90)
        self.fmin.returnPressed.connect(self.search)
        self.fmax = QtWidgets.QLineEdit()
        self.fmax.setPlaceholderText("to MHz")
        self.fmax.setFixedWidth(90)
        self.fmax.setToolTip("Blank: only the From frequency (±1 kHz), or no limit if From is blank too.")
        self.fmax.returnPressed.connect(self.search)
        row.addWidget(self.fmin)
        row.addWidget(QtWidgets.QLabel("–"))
        row.addWidget(self.fmax)
        lay.addLayout(row)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Text:"))
        self.text = QtWidgets.QLineEdit()
        self.text.setPlaceholderText("licensee, call sign or city (e.g. police, WQAA001, Boston)")
        self.text.setClearButtonEnabled(True)
        self.text.returnPressed.connect(self.search)
        row.addWidget(self.text, 2)
        row.addWidget(QtWidgets.QLabel("Service:"))
        self.service = QtWidgets.QComboBox()
        self.service.addItem("All", "")
        self.service.setToolTip("FCC radio-service group. Codes without a friendly name are shown as "
                                "the raw ULS radio service code.")
        row.addWidget(self.service)
        row.addWidget(QtWidgets.QLabel("Type:"))
        self.emission = QtWidgets.QComboBox()
        self.emission.addItem("All", "")
        for name in fcc_uls.EMISSION_FILTERS:
            self.emission.addItem(name, name)
        self.emission.setToolTip("Only channels licensed for this emission type (from the FCC emission "
                                 "designators). A license can list several types; what is actually on the "
                                 "air may be any of them.")
        row.addWidget(self.emission)
        self.search_btn = QtWidgets.QPushButton("Search")
        self.search_btn.setDefault(True)
        self.search_btn.clicked.connect(self.search)
        row.addWidget(self.search_btn)
        lay.addLayout(row)

        self.table = QtWidgets.QTableWidget(0, len(COLS))
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, QtCore.Qt.AscendingOrder)
        self.table.verticalHeader().setVisible(False)
        for c, w in enumerate((95, 230, 230, 150, 80, 100, 150, 70)):
            self.table.setColumnWidth(c, w)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellDoubleClicked.connect(lambda r, _c: self._tune(r))
        lay.addWidget(self.table, 1)

        row = QtWidgets.QHBoxLayout()
        self.status = QtWidgets.QLabel("")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        tune_btn = QtWidgets.QPushButton("Tune")
        tune_btn.setToolTip("Tune the radio to the selected row (or double-click a row).")
        tune_btn.clicked.connect(lambda: self._tune(self.table.currentRow()))
        row.addWidget(tune_btn)
        self.add_btn = QtWidgets.QPushButton("Add selected → bank…")
        self.add_btn.setToolTip("Copy the selected rows (all rows if none are selected) into a new or "
                                "existing bank.")
        self.add_btn.clicked.connect(self._add_to_bank)
        row.addWidget(self.add_btn)
        lay.addLayout(row)

    # -- index ---------------------------------------------------------------------
    def showEvent(self, ev):
        super().showEvent(ev)
        self.refresh_data()

    def refresh_data(self):
        """Check the data/index state; (re)build the index when the .dat files are newer."""
        if self._index_worker is not None:
            return
        if not fcc_uls.has_data(self.path):
            self._set_busy(False, ready=False)
            self.status.setText("No FCC data yet — use Download FCC data in Memories ▸ Import first.")
            return
        if fcc_uls.index_fresh(self.path):
            self._index_ready()
            return
        self._set_busy(True)
        self.status.setText("Building the search index from the downloaded data (once per download, "
                            "~30 s for LMpriv) …")
        self._index_worker = IndexWorker(self.path, self)
        self._index_worker.progress.connect(lambda m: self.status.setText(f"Building the search index: {m}"))
        self._index_worker.done.connect(self._index_ready)
        self._index_worker.failed.connect(self._index_failed)
        self._index_worker.finished.connect(self._index_finished)
        self._index_worker.start()

    def _index_finished(self):
        w, self._index_worker = self._index_worker, None
        if w is not None:
            w.deleteLater()

    def _index_failed(self, msg):
        self._search_after_index = False
        self._set_busy(False, ready=False)
        self.status.setText(msg)

    def _index_ready(self):
        info = fcc_uls.index_info(self.path)
        cur = self.service.currentData()
        self.service.blockSignals(True)
        self.service.clear()
        self.service.addItem("All", "")
        for svc, n in info["services"]:
            self.service.addItem(f"{svc} ({n:,})", svc)
        i = self.service.findData(cur)
        self.service.setCurrentIndex(max(0, i))
        self.service.blockSignals(False)
        self._set_busy(False)
        self.status.setText(f"{info['rows']:,} licensed channels (FCC data of {info['created'] or '?'}). "
                            "Set Where / Freq / Text and press Search.")
        if self._search_after_index:
            self._search_after_index = False
            self.search()

    def _set_busy(self, busy, ready=True):
        self.search_btn.setEnabled(not busy and ready)

    # -- search --------------------------------------------------------------------
    def _freq_range(self):
        """(fmin_hz, fmax_hz) from the Freq boxes; raises ValueError on bad input."""
        lo, hi = self.fmin.text().strip(), self.fmax.text().strip()
        fmin = float(lo) * 1e6 if lo else None
        fmax = float(hi) * 1e6 if hi else None
        if fmin is not None and fmax is None:          # a single frequency: ±1 kHz
            fmin, fmax = fmin - 1e3, fmin + 1e3
        if fmin is not None and fmax is not None and fmax < fmin:
            fmin, fmax = fmax, fmin
        return fmin, fmax

    def search(self):
        if self._search_worker is not None:
            return
        if self._index_worker is not None or not fcc_uls.index_fresh(self.path):
            self._search_after_index = fcc_uls.has_data(self.path)   # run it once the index is built
            self.refresh_data()
            return
        try:
            fmin, fmax = self._freq_range()
        except ValueError:
            self.status.setText("Freq: enter MHz, e.g. 453.1 and 453.9")
            return
        where = self.where.text().strip()
        self.status.setText(f"searching{' near ' + where if where else ''} …")
        self._search_worker = SearchWorker(self.path, where, self.radius.value() * 1.60934, fmin, fmax,
                                           self.text.text().strip(), self.service.currentData() or "",
                                           self.emission.currentData() or "", self)
        self._search_worker.done.connect(self._on_results)
        self._search_worker.failed.connect(self._on_search_failed)
        self._search_worker.finished.connect(self._search_finished)
        self._set_busy(True)
        self._search_worker.start()

    def _search_finished(self):
        w, self._search_worker = self._search_worker, None
        if w is not None:
            w.deleteLater()
        self._set_busy(False)

    def _on_search_failed(self, msg):
        self.status.setText(msg)

    def _on_results(self, results, total, where_label):
        self._results = results
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(results))
        for r, c in enumerate(results):
            m = c.meta
            dist = m.get("dist_km")
            cells = [f"{c.freq / 1e6:.5f}", m.get("kind", ""), m.get("name", ""), m.get("place", ""),
                     m.get("call", ""), c.service, m.get("emission", ""),
                     "" if dist is None else f"{dist / 1.60934:.1f}"]
            tip = fcc_uls.emission_details(m.get("emission", ""))
            for col, txt in enumerate(cells):
                if col in (0, 7):                      # numeric sort on freq / distance
                    it = _NumItem(txt)
                    if txt:
                        it.setData(QtCore.Qt.UserRole + 1, float(txt))
                else:
                    it = QtWidgets.QTableWidgetItem(txt)
                if col == 0:
                    it.setData(QtCore.Qt.UserRole, r)  # index into self._results
                if col in (1, 6) and tip:              # each designator and its name
                    it.setToolTip(tip)
                t.setItem(r, col, it)
        t.setSortingEnabled(True)
        where = where_label.split(",")[0] if where_label else ""
        more = f" — showing the first {len(results):,}; narrow the search" if total > len(results) else ""
        self.status.setText(f"{total:,} match{'es' if total != 1 else ''}"
                            f"{f' within {self.radius.value()} mi of {where}' if where else ''}{more}. "
                            "Double-click a row to tune it.")

    # -- actions -------------------------------------------------------------------
    def _row_result(self, row):
        it = self.table.item(row, 0) if row >= 0 else None
        if it is None:
            return None
        return self._results[it.data(QtCore.Qt.UserRole)]

    def _tune(self, row):
        c = self._row_result(row)
        if c is None:
            return
        if not self.app._scanning:
            self.app._leave_channel()
        self.app._apply_channel_mode(c.mode, c.freq)
        self.app._set_freq(c.freq)
        self.status.setText(f"tuned {c.freq / 1e6:.5f} MHz — {c.label}")

    def _add_to_bank(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        picked = [self._row_result(r) for r in rows] if rows else list(self._results)
        chans = [c.to_channel() for c in picked if c is not None]
        default = (self.where.text().strip().split(",")[0] or self.text.text().strip() or "FCC")[:40]
        self.app._copy_channels_dialog(chans, "FCC results → bank", f"FCC {default}")
        self.status.setText(self.app.fetch_status.text())

    def shutdown(self):
        """Wait for a running index build / search (called from the app's closeEvent)."""
        for w in (self._index_worker, self._search_worker):
            if w is not None:
                w.wait(120_000 if w is self._index_worker else 15_000)
