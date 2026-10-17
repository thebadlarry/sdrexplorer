# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The core's side of a module's attach_ui(): the registry it adds menus, modes and overlays through, and
the Settings > Modules dialog."""
from __future__ import annotations

import os

from PyQt5 import QtCore, QtWidgets

from . import modules as _modules
from . import paths
from .api.module import MenuEntry, ModeSpec, Overlay, Panel
from .api.records import Decode, Position


class ModuleUI:
    """What a module's attach_ui(app, ui) gets as ``ui``: its handle on the core's paths. One per loaded
    module, so whatever it adds is recorded as that module's."""

    def __init__(self, app, module):
        self.app = app
        self.module = module
        self.bus = app.bus
        self.audio = app.audio

    # -- decode data / map ----------------------------------------------------------------------------
    def publish(self, record: Decode):
        """An api.Decode: the decode log, payload pane, recording labels and the "decode" topic."""
        if not record.source:
            record.source = self.module.name
        self.app.publish(record, module=self.module)

    def publish_position(self, record: Position):
        if not record.source:
            record.source = self.module.name
        self.app.publish_position(record)

    def log(self, text):
        """A plain line for the decode log (timestamped there); prefer publish(Decode)."""
        self.app._dig_append(text)

    def status(self, text, ms=6000):
        self.app.statusBar().showMessage(text, ms)

    # -- control --------------------------------------------------------------------------------------
    def request_decode(self, label, freq_hz, info=None):
        """Ask whichever module decodes ``label`` to take the signal at freq_hz (False: none does)."""
        return self.app.request_decode(label, freq_hz, info)

    def tuned_hz(self):
        return int(self.app.radio.freq_hz)

    def tx_mode(self):
        """The mode PTT would transmit now: a module TX mode's name, or the tuned analog mode."""
        return self.app._tx_mode()

    def channel_fields(self):
        """This module's fields on the bank channel recalled last ({} if none, or the user has
        tuned by hand since)."""
        return self.app.channel_fields(self.module.name)

    def set_channel_fields(self, fields):
        """Replace this module's fields on the bank channel recalled last, and save the banks.
        False when no channel is recalled."""
        return self.app.set_channel_fields(self.module.name, fields)

    @property
    def tuner(self):
        """The tuner lease (api.Tuner): acquire it before moving the band on your own."""
        return self.app.tuner

    def tune(self, freq_hz, recenter=True, user=False):
        """Tune as this module: moving the band (recenter) needs the tuner lease, or nobody holding it.
        ``user=True``: the user asked for it in this module's window (a table row, a button), so it tunes as
        the user would, pre-empting any lease. Returns (ok, reason)."""
        return self.app.tune(freq_hz, recenter=recenter, owner="user" if user else self.module.name)

    def tune_band(self, center_hz, owner=None):
        """Put the band's center at ``center_hz`` (the demod lands where a primary retune puts it), as
        ``owner`` (default this module; it needs the lease or nobody holding it). Returns (ok, reason)."""
        if self.app.radio.is_replay():
            return False, "replaying a file: its band can't be moved"
        from .radio import OFFSET
        return self.app.tune(int(center_hz) + OFFSET, recenter=True, owner=owner or self.module.name)

    def samp_rates(self, all_rates=False):
        """(rates, default): the capture rates (Hz) the current device offers, or with ``all_rates`` every
        rate the core can run at, whatever the device."""
        from .radio import SAMP_RATE, SAMP_RATE_OPTIONS
        rates = SAMP_RATE_OPTIONS if all_rates else self.app._rates_for(self.app.radio.device)
        return list(rates), int(SAMP_RATE)

    def set_samp_rate(self, rate):
        """Change the capture rate as the bandwidth picker does (the radio restarts). Returns (ok, reason)."""
        rate = int(rate)
        if rate == int(self.app.radio.samp_rate):
            return True, ""
        i = self.app.bw_combo.findData(rate)
        if i < 0:
            return False, f"{rate / 1e6:g} Msps is not available on {self.app._device_label()}"
        was = self.app.radio.running
        self.app.bw_combo.setCurrentIndex(i)
        if was and not self.app.radio.running:
            return False, f"the SDR did not restart at {rate / 1e6:g} Msps"
        return True, ""

    def is_scanning(self):
        """A channel scan is running (it holds the band; some features stay out of its way)."""
        return bool(self.app._scanning)

    def current_mode(self):
        """The Mode picker's entry (a core mode or any module's)."""
        return self.app.mode_combo.currentText()

    def set_mode(self, name):
        """Pick a Mode-picker entry (a core mode or any module's); the core enters it as if clicked."""
        self.app.mode_combo.setCurrentText(name)
        return self.app.mode_combo.currentText() == name

    # -- UI placement ---------------------------------------------------------------------------------
    def add_menu_entry(self, e: MenuEntry):
        return self.app._add_module_menu_entry(self.module, e)

    def add_overlay(self, o: Overlay):
        self.app.spectrum.add_overlay(o)

    def add_mode(self, spec: ModeSpec):
        self.app._register_mode(self.module, spec)

    def annotate(self, items, trail=True):
        """This module's labeled signals ([api.Annotation]) on the waterfall; replaces its previous set.
        trail=False drops tags of signals that stopped (the band moved, the module stopped)."""
        for it in items:
            if hasattr(it, "source") and not it.source:
                it.source = self.module.name
        self.app.annotate(self.module.name, items, trail=trail)

    def annotation_near(self, freq_hz):
        return self.app.annotation_near(freq_hz)

    def snapshot(self, n, timeout=5.0):
        """The next n raw band samples (api.radio.snapshot), off the Qt thread."""
        from .api import radio as _r
        return _r.snapshot(self.app.radio, n, timeout)

    def set_status_box(self, lines):
        """[(text, "#rrggbb")] in a box at the waterfall's bottom right (e.g. a module's performance), or
        None to hide it."""
        self.app.spectrum.set_status_box(lines)

    def label_style(self):
        """The waterfall label style (Settings ▸ Label style…): {"font_px", "text_color", "tick_color"}."""
        sp = self.app.spectrum
        return {"font_px": int(sp.marker_font_px), "text_color": sp.marker_text_color,
                "tick_color": sp.marker_tick_color}

    def repaint(self):
        """Redraw the waterfall now (after an overlay's data changed)."""
        self.app.spectrum.update()

    def show_annotations(self):
        """Make annotations visible: View ▸ Label source "memories" becomes "both" (other choices stay)."""
        self.app._show_annotations()

    def set_app_icon(self, icon):
        """The main window's / taskbar icon (a QIcon) while something runs, or None for the usual one."""
        self.app._set_app_icon(icon)

    def label_alias(self, store_label, shown):
        """A store label is shown (and its recordings folder named) as ``shown``, e.g. a variant
        label under its family name. Affects the "recorded" marks and the waterfall."""
        from . import recindex
        recindex.DISPLAY[str(store_label)] = str(shown)

    def recordings_changed(self):
        """New files in the recordings folders or the store: the waterfall's "recorded" marks are rebuilt."""
        self.app._refresh_recorded()

    def recorded_channels(self):
        """[(freq_hz, label)] for every channel that already has recordings (recordings/ folders and the
        real-data store; a recording with no label gives "rec"), as the waterfall's "recorded" marks."""
        from . import recindex
        from .api import store
        return recindex.known_channels(recindex.build(paths.recordings_dir(), store))

    def option(self, key, default=None):
        """A value given for this module on the command line (``--module-option <name>.<key>=<value>``,
        this run only), else ``default``."""
        return _modules.options(self.module.name).get(key, default)

    def save_state(self):
        """Save the session state now (every module's save_state too), e.g. after a settings dialog's OK."""
        self.app._save_state()

    def add_panel(self, panel: Panel):
        self.app._add_module_panel(self.module, panel)

    def service(self, name):
        return self.app.service(name)


class ModulesDialog(QtWidgets.QDialog):
    """Settings > Modules: every module found, where it was found, whether it runs and why not; Add module...
    (a folder you cloned a module repo into), Remove (a folder you added), Rescan. Changes apply at the next
    start (a module's branches and menus are built once, when the app starts)."""

    COLS = ("Run", "Module", "Version", "Status", "Folder", "Found via")

    def __init__(self, parent, host):
        super().__init__(parent)
        self.setWindowTitle("Modules")
        self.host = host
        self.found = list(host.found)          # what the table shows (Rescan replaces it)
        lay = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            "Modules add features with their own dependencies. To add one, clone its repo to a folder "
            "and use Add module…; a repo cloned next to sdrexplorer is found by itself. Untick a module "
            "to switch it off. Changes apply the next time sdrexplorer starts.")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.empty = QtWidgets.QLabel("No modules found: this is the core program only.")
        lay.addWidget(self.empty)
        lay.addWidget(self.table, 1)
        row = QtWidgets.QHBoxLayout()
        self.add_btn = QtWidgets.QPushButton("Add module…")
        self.add_btn.setToolTip("Pick the folder you cloned a module repo into (the one with its "
                                "pyproject.toml). It is checked, then remembered in your paths.json.")
        self.add_btn.clicked.connect(self._add)
        self.remove_btn = QtWidgets.QPushButton("Remove")
        self.remove_btn.setToolTip("Forget a folder you added with Add module… (nothing is deleted). "
                                   "Modules found any other way can only be switched off.")
        self.remove_btn.clicked.connect(self._remove)
        self.rescan_btn = QtWidgets.QPushButton("Rescan")
        self.rescan_btn.setToolTip("Look for modules again (new ones load at the next start).")
        self.rescan_btn.clicked.connect(self._rescan)
        for b in (self.add_btn, self.remove_btn, self.rescan_btn):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        # the core's own optional features (sdrexplorer.features): not switchable, just found or not
        from . import features as _features
        lay.addWidget(QtWidgets.QLabel("Core features (each switches itself off when its library is missing):"))
        feats = _features.probe()
        self.features = QtWidgets.QTableWidget(len(feats), 3)
        self.features.setHorizontalHeaderLabels(("Feature", "Needs", "Status"))
        self.features.verticalHeader().setVisible(False)
        self.features.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.features.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
        for r, f in enumerate(feats):
            for col, text in enumerate((f.name, f.needs, "available" if f.ok else f.why)):
                it = QtWidgets.QTableWidgetItem(text)
                it.setToolTip(text)
                self.features.setItem(r, col, it)
        self.features.resizeColumnsToContents()
        self.features.horizontalHeader().setStretchLastSection(True)
        lay.addWidget(self.features, 1)
        self.restart_note = QtWidgets.QLabel("")
        self.restart_note.setWordWrap(True)
        self.restart_note.setStyleSheet("color:#f0b429;")
        lay.addWidget(self.restart_note)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._notes = []                 # what Add / Remove did, until the restart
        self._fill()
        self.resize(900, 500)

    # -- table ------------------------------------------------------------------------------------------
    def _status(self, f):
        if f.loaded:
            return "loaded"
        loaded_names = {x.name for x in self.host.found if x.loaded}
        if not f.error and f.ok and f.enabled and f.name not in loaded_names:
            return "found: loads at the next start"
        return f.status()

    def _fill(self):
        t = self.table
        t.setRowCount(len(self.found))
        self._rows = []
        for row, f in enumerate(self.found):
            cb = QtWidgets.QCheckBox()
            cb.setChecked(f.enabled)
            cb.setEnabled(not f.error and f.ok)
            cb.toggled.connect(lambda on, n=f.name: self._toggle(n, on))
            cell = QtWidgets.QWidget()
            cl = QtWidgets.QHBoxLayout(cell)
            cl.setContentsMargins(6, 0, 6, 0)
            cl.addWidget(cb)
            t.setCellWidget(row, 0, cell)
            for col, text in ((1, f.title or f.name), (2, f.version), (3, self._status(f)),
                              (4, f.folder or "-"), (5, f.source or "-")):
                it = QtWidgets.QTableWidgetItem(text)
                it.setToolTip(text)
                t.setItem(row, col, it)
            self._rows.append(f)
        t.resizeColumnsToContents()
        t.horizontalHeader().setStretchLastSection(True)
        self.empty.setVisible(not self.found)
        self._update_buttons()
        self._update_note()

    def _selected(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self._rows[rows[0].row()] if rows else None

    def _update_buttons(self):
        f = self._selected()
        self.remove_btn.setEnabled(bool(f and f.source == paths.SRC_ADDED))

    def _update_note(self):
        changed = [x.name for x in self.host.found if x.ok and not x.error and (x.enabled != x.loaded)]
        lines = list(self._notes)
        if changed:
            lines.append("Restart sdrexplorer to apply: " + ", ".join(changed))
        self.restart_note.setText("\n".join(lines))

    # -- actions ----------------------------------------------------------------------------------------
    def _toggle(self, name, on):
        _modules.set_enabled(name, on)
        for f in list(self.host.found) + self.found:
            if f.name == name:
                f.enabled = on
        self._fill()

    def add_folder(self, folder):
        """Check ``folder`` and remember it. (ok, message); the dialog's Add module... calls this."""
        ok, msg, names = _modules.check_folder(folder)
        if not ok:
            return False, msg
        if not paths.add_module_dir(folder):
            return False, f"{folder} is already in your module folders"
        self._notes.append(f"Added {folder}" + (f" ({', '.join(names)})" if names else "")
                           + ": it loads at the next start." + (f" Note: {msg}" if msg else ""))
        self._rescan()
        return True, msg

    def remove_folder(self, folder):
        if not paths.remove_module_dir(folder):
            return False
        self._notes.append(f"Removed {folder} from your module folders (nothing deleted): "
                           "its modules stop loading at the next start.")
        self._rescan()
        return True

    def _add(self):
        start = os.path.dirname(paths.REPO_DIR)
        folder = QtWidgets.QFileDialog.getExistingDirectory(self, "Module folder (where you cloned it)", start)
        if not folder:
            return
        ok, msg = self.add_folder(folder)
        if not ok:
            QtWidgets.QMessageBox.warning(self, "Not added", msg)

    def _remove(self):
        f = self._selected()
        if f is None or f.source != paths.SRC_ADDED:
            return
        box = QtWidgets.QMessageBox.question(
            self, "Remove module folder",
            f"Stop loading modules from\n{f.folder}?\n\nThe folder itself is not deleted.")
        if box == QtWidgets.QMessageBox.Yes:
            self.remove_folder(f.folder)

    def _rescan(self):
        """Look again. Modules already running stay as they are until the restart; new ones are listed."""
        fresh = {f.name: f for f in _modules.discover(builtin=())}
        running = {f.name: f for f in self.host.found if f.loaded}
        self.found = [running.get(n, f) for n, f in fresh.items()] + \
                     [f for n, f in running.items() if n not in fresh]
        self._fill()
