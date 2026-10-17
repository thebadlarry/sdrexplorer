#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Offscreen tests for the Radio/Memories UI restructure.

Run headless (no radio, no modules, no ALSA):

    QT_QPA_PLATFORM=offscreen python3 -m sdrexplorer.tests.test_ui_restructure

CRITICAL: this points every user-data path (sdrexplorer.paths.override) at a
throwaway temp dir BEFORE constructing ScannerApp, so it never touches the
user's real banks.json / state.json (see the sdrexplorer-tests-clobber-repo-state
memory). It also stubs ScannerRadio.start + audio so nothing opens a device,
and loads no modules (module_dirs=[]): the core alone.
"""
import json
import os
import shutil
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                       # noqa: E402
from PyQt5 import QtWidgets, QtCore, QtGui     # noqa: E402

from sdrexplorer import app as A            # noqa: E402
from sdrexplorer import paths as P          # noqa: E402
from sdrexplorer.banks import Channel       # noqa: E402


# ---------------------------------------------------------------------------
_QAPP = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
_TMP = tempfile.mkdtemp(prefix="sdrexplorer-test-")
_REAL_START = A.ScannerRadio.start


def _install_stubs():
    """Redirect all on-disk paths to the temp dir and neuter the hardware."""
    P.override(_TMP, module_dirs=[])    # memory/, recordings/, uls/, config/ under _TMP; the core alone
    # made-up SDRs (a transmit-capable HackRF-like one, a receive-only RTL-like one): the suite never
    # depends on what's plugged in
    from sdrexplorer.api.testing import use_fake_sources
    use_fake_sources()
    # never open a device / build an audio sink
    A.ScannerRadio.start = lambda self: setattr(self, "running", True)
    # audio sink off (ALSA is unavailable in a sandbox)
    _orig_init = A.ScannerRadio.__init__

    def _init(self, *a, **kw):
        kw["audio_out"] = False
        _orig_init(self, *a, **kw)
    A.ScannerRadio.__init__ = _init


def _fresh_state(state=None):
    """Write a state.json (or remove it) before building the window."""
    if state is None:
        try:
            os.remove(A._state_path())
        except OSError:
            pass
    else:
        os.makedirs(os.path.dirname(A._state_path()), exist_ok=True)
        with open(A._state_path(), "w") as fh:
            json.dump(state, fh)


def _build():
    win = A.ScannerApp()
    win.resize(900, 700)
    return win


def _set_bank(win, name, channels):
    """Replace the active bank's channels with the given list, refresh, return."""
    if name not in win.store.banks:
        win.store.create_bank(name)
    win.store.set_active(name)
    win.store.bank().channels[:] = channels
    win._refresh_bank_combo()
    win._refresh_table()


# ---------------------------------------------------------------------------
def test_view_switch():
    _fresh_state()
    win = _build()
    try:
        assert win.stack.count() == 2, "expected Radio + Memories pages"
        assert win.stack.currentIndex() == 0, "should start on Radio"
        assert win.menuBar() is not None
        # Radio page hosts the spectrum + SDR controls
        assert hasattr(win, "spectrum") and hasattr(win, "sdr_btn") \
            and hasattr(win, "sdr_status")
        # Memories page hosts the merged table + bank selector
        assert hasattr(win, "table") and hasattr(win, "bank_combo")

        win._show_memories()
        assert win.stack.currentIndex() == 1
        assert win.spectrum.timer.isActive() is False, "waterfall must pause on Memories"

        win._show_radio()
        assert win.stack.currentIndex() == 0
        assert win.spectrum.timer.isActive() is True, "waterfall resumes on Radio (idle)"

        win._toggle_view()
        assert win.stack.currentIndex() == 1
    finally:
        win.close()
    print("ok  test_view_switch")


def test_menu_dialog_widgets_wired():
    _fresh_state()
    win = _build()
    try:
        for attr in ("dwell_spin", "hang_spin", "prio_cb", "prio_spin",
                     "prio_add_btn", "scan_status", "reset_btn"):
            assert hasattr(win, attr), f"missing {attr}"
        # opening the dialogs must not raise and must re-parent the content
        win._open_scan_settings()
        assert win._scan_dialog is not None
        assert win.dwell_spin.window() is win._scan_dialog
        win._open_rf_gains()
        assert win._rf_dialog is not None

        # the value _toggle_scan reads flows from the (now-dialog) spin box
        win.dwell_spin.setValue(321)
        assert win.dwell_spin.value() == 321
    finally:
        win.close()
    print("ok  test_menu_dialog_widgets_wired")


def test_tx_controls_gated_on_hackrf():
    """PTT, the TX: mode combo, and the whole Settings > Transmit dialog gray out together on a device
    that reports no transmitter, and re-enable on one that does. The SDR picker lists the attached
    devices (by key), the old fixed name "HackRF" still selects the first HackRF, and each device gets
    the gain sliders and capture rates it reports."""
    _fresh_state({"sdr_device": "HackRF"})                    # a state.json from before device keys
    win = _build()
    try:
        assert win.sdr_combo.currentData() == "hackrf:fake1" and win.radio.device == "hackrf:fake1"
        assert [win.sdr_combo.itemData(i) for i in range(win.sdr_combo.count())] == \
            ["hackrf:fake1", "rtlsdr:fake2", A.IQ_FILE_DEVICE]
        assert win.tx_mode_combo.isEnabled() is True
        assert win._tx_ctl_widget.isEnabled() is True
        names = [win._gain_box.itemAt(i).widget().findChild(QtWidgets.QLabel).text()
                 for i in range(win._gain_box.count())]
        assert names == ["RF", "IF", "BB"], names
        assert [win.bw_combo.itemData(i) for i in range(win.bw_combo.count())] == list(A.SAMP_RATE_OPTIONS)

        # devinfo tracks the SELECTED device without starting, so the gain dialog shows the right
        # device's sliders even before Start (regression: devinfo was only set in start())
        assert win.radio.devinfo is not None and win.radio.devinfo.key == "hackrf:fake1"
        win._on_sdr_change("rtlsdr:fake2")
        assert win.radio.device == "rtlsdr:fake2"
        assert win.radio.devinfo.key == "rtlsdr:fake2"
        assert win.ptt_btn.isEnabled() is False
        assert win.tx_mode_combo.isEnabled() is False
        assert win._tx_ctl_widget.isEnabled() is False
        assert win._gain_box.count() == 1                       # its one stage
        assert [win.bw_combo.itemData(i) for i in range(win.bw_combo.count())] == [2_400_000]
        win.radio.set_gain("TUNER", 30)
        win._on_sdr_change("hackrf:fake1")
        assert win.radio.device == "hackrf:fake1"
        assert win.tx_mode_combo.isEnabled() is True
        assert win._tx_ctl_widget.isEnabled() is True
        win._on_sdr_change("rtlsdr:fake2")                       # its gain was kept, per device
        assert win.radio.dev_settings["gains"]["TUNER"] == 30
        win._save_state()
        state = json.load(open(A._state_path()))
        assert state["sdr_device"] == "rtlsdr:fake2"
        assert "gains" not in state          # gains are per device (sources.json), not in state.json
    finally:
        win.close()
    print("ok  test_tx_controls_gated_on_hackrf")


def test_legacy_gains_migrate_to_device():
    """A state.json from before per-device gains (a global {rf,if,bb} block) is migrated once onto the
    startup device's stages (sources.json), then never read again."""
    from sdrexplorer import sources as SRC
    hackrf = SRC.find("hackrf:fake1")
    SRC.save_settings(hackrf, gains={})      # start with nothing saved for the device
    _fresh_state({"sdr_device": "hackrf:fake1",
                  "gains": {"rf": 14, "if": 24, "bb": 30}})
    win = _build()
    try:
        # the global triple was mapped onto the HackRF's RF/IF/BB stages and saved per device
        assert win.radio.dev_settings["gains"] == {"RF": 14, "IF": 24, "BB": 30}
        assert SRC.settings(hackrf)["gains"] == {"RF": 14, "IF": 24, "BB": 30}
    finally:
        win.close()
    # a second launch does NOT re-migrate: change the device's saved gains, drop a fresh legacy block,
    # and confirm the device keeps its own gains (the legacy block is ignored once gains exist)
    SRC.save_settings(hackrf, gains={"RF": 7, "IF": 7, "BB": 7})
    _fresh_state({"sdr_device": "hackrf:fake1", "gains": {"rf": 14, "if": 24, "bb": 30}})
    win = _build()
    try:
        assert win.radio.dev_settings["gains"] == {"RF": 7, "IF": 7, "BB": 7}
    finally:
        win.close()
    print("ok  test_legacy_gains_migrate_to_device")


def test_foreign_gain_stage_pruned():
    """A stray gain stage the device doesn't have (e.g. an old cross-device "LNA" left in a HackRF's
    saved gains) is pruned on load, in memory and on disk. gr-osmosdr would otherwise route the unknown
    name to the overall gain and corrupt the amp -- the "no signal until I toggle RF" bug."""
    from sdrexplorer import sources as SRC
    hackrf = SRC.find("hackrf:fake1")
    SRC.save_settings(hackrf, gains={"RF": 0, "IF": 32, "BB": 48, "LNA": 49})   # stray LNA
    _fresh_state({"sdr_device": "hackrf:fake1"})
    win = _build()
    try:
        assert win.radio.dev_settings["gains"] == {"RF": 0, "IF": 32, "BB": 48}
        assert "LNA" not in SRC.settings(hackrf)["gains"]      # repaired on disk too
    finally:
        win.close()
    print("ok  test_foreign_gain_stage_pruned")


def test_repeater_offset_up_to_50_mhz():
    """The offset box takes 900 MHz (25 MHz) and 800 MHz (45 MHz) splits; its arrow step is 5 kHz below
    1 MHz and 100 kHz above; 25 MHz - on 927.8625 transmits on 902.8625 (TxChain stubbed: no device)."""
    _fresh_state({"sdr_device": "hackrf:fake1"})
    win = _build()
    made = []

    class _Tx:
        def __init__(self, freq_hz, **kw):
            made.append(freq_hz)
            self.probe, self.samp_rate, self.freq_hz = None, 1, freq_hz

        def start(self):
            pass

        def stop(self):
            pass

    orig_tx, orig_disp = A.TxChain, win.spectrum.set_tx_display
    A.TxChain = _Tx
    win.spectrum.set_tx_display = lambda *a: None
    try:
        assert win.offset_spin.maximum() == 50.0
        win.offset_spin.setValue(0.6)
        assert abs(win.offset_spin.singleStep() - 0.005) < 1e-9
        win.offset_dir_combo.setCurrentText("−")
        win.offset_spin.setValue(25.0)
        assert win.offset_spin.value() == 25.0 and abs(win.offset_spin.singleStep() - 0.1) < 1e-9
        win.radio.demod_hz = 927_862_500
        win._tx_source = "mic"
        win._tx_digital_mode = "Analog"
        win._start_tx()
        assert made == [902_862_500], made
        win._stop_tx()
        win._apply_offset(-45_000_000)                       # a recalled 800 MHz channel
        assert win.offset_spin.value() == 45.0 and win._current_offset_hz() == -45_000_000
    finally:
        A.TxChain = orig_tx
        win.spectrum.set_tx_display = orig_disp
        win.close()
    print("ok  test_repeater_offset_up_to_50_mhz")


def test_tx_ham_band_guard():
    """PTT refuses to build a TxChain outside the US amateur bands unless the
    licensed-operator override is set; in-band always keys. The hard stop lives
    in _start_tx, so a stubbed TxChain records only the frequencies that pass."""
    _fresh_state({"sdr_device": "hackrf:fake1"})
    win = _build()
    made = []

    class _Tx:
        def __init__(self, freq_hz, **kw):
            made.append(freq_hz)
            self.probe, self.samp_rate, self.freq_hz = None, 1, freq_hz

        def start(self):
            pass

        def stop(self):
            pass

    orig_tx, orig_disp = A.TxChain, win.spectrum.set_tx_display
    A.TxChain = _Tx
    win.spectrum.set_tx_display = lambda *a: None
    win._tx_source = "mic"
    win._tx_digital_mode = "Analog"
    try:
        # out of band (460 MHz commercial LMR), override off -> blocked, nothing built
        win._tx_ham_override = False
        win.offset_dir_combo.setCurrentText("Simplex")
        win.radio.demod_hz = 460_000_000
        win._start_tx()
        assert made == [], made
        assert win.tx is None

        # in band (2 m) -> keys
        win.radio.demod_hz = 146_520_000
        win._start_tx()
        assert made == [146_520_000], made
        win._stop_tx()

        # out of band but override on -> keys
        win._tx_ham_override = True
        win.radio.demod_hz = 460_000_000
        win._start_tx()
        assert made[-1] == 460_000_000, made
        win._stop_tx()

        # the button's enabled state + tooltip follow the tune (via _set_freq ->
        # _update_ptt_enabled), not just the hard stop in _start_tx
        win._tx_ham_override = False
        win.offset_dir_combo.setCurrentText("Simplex")
        win._set_freq(460_000_000, how="typed")          # out of band
        assert not win.ptt_btn.isEnabled()
        assert "outside the US amateur bands" in win.ptt_btn.toolTip()
        win._set_freq(445_000_000, how="typed")          # 70 cm, in band
        assert win.ptt_btn.isEnabled()
        assert "outside the US amateur bands" not in win.ptt_btn.toolTip()
        win._set_freq(145_000_000, how="typed")          # 2 m, in band
        assert win.ptt_btn.isEnabled()
        # override re-enables an out-of-band tune
        win._set_freq(460_000_000, how="typed")
        assert not win.ptt_btn.isEnabled()
        win._tx_ham_override = True
        win._on_tx_ham_override_toggle(True)             # the checkbox handler refreshes PTT
        assert win.ptt_btn.isEnabled()
    finally:
        A.TxChain = orig_tx
        win.spectrum.set_tx_display = orig_disp
        win.close()
    print("ok  test_tx_ham_band_guard")


def test_vfo_shows_tx_frequency_during_tx():
    """While transmitting, the main VFO readout shows the TX (repeater output) frequency, then reverts
    on release. It is cosmetic only -- the tuned frequency and the radio state never move."""
    _fresh_state({"sdr_device": "hackrf:fake1"})
    win = _build()

    class _Tx:
        def __init__(self, freq_hz, **kw):
            self.probe, self.samp_rate, self.freq_hz = None, 1, freq_hz

        def start(self):
            pass

        def stop(self):
            pass

    orig_tx, orig_disp = A.TxChain, win.spectrum.set_tx_display
    A.TxChain = _Tx
    win.spectrum.set_tx_display = lambda *a: None
    win._tx_source = "mic"
    win._tx_digital_mode = "Analog"
    win._tx_ham_override = True                       # key regardless of band
    try:
        win.offset_dir_combo.setCurrentText("−")
        win.offset_spin.setValue(5.0)                 # 70 cm -5 MHz repeater split
        win._set_freq(449_000_000, how="typed")       # RX (repeater output) -- tunes radio + display
        rx_hz = int(win.radio.freq_hz)
        disp_rx = win.freq_disp.frequency()           # what the VFO reads before TX
        tx_hz = win.radio.demod_hz + win._current_offset_hz()
        assert tx_hz == 444_000_000

        assert win.freq_disp._tx_hz is None           # not transmitting yet
        win._start_tx()
        # the display shows the TX frequency, but the tuned frequency / radio are untouched
        assert win.freq_disp._tx_hz == tx_hz
        assert win.freq_disp._text().startswith("0444.")
        assert win.freq_disp.frequency() == disp_rx   # underlying display state unchanged
        assert int(win.radio.freq_hz) == rx_hz

        win._stop_tx()
        assert win.freq_disp._tx_hz is None           # back to the RX readout
        assert win.freq_disp.frequency() == disp_rx
        assert int(win.radio.freq_hz) == rx_hz
    finally:
        A.TxChain = orig_tx
        win.spectrum.set_tx_display = orig_disp
        win.close()
    print("ok  test_vfo_shows_tx_frequency_during_tx")


def test_repeater_offset_auto_by_band():
    """Tuning (typed) auto-fills the repeater TX offset from the US band convention: 70cm -5 MHz,
    2 m +/-0.6 by sub-band, 10 m -0.1, 1.25 m -1.6, 33 cm -25; out-of-band -> simplex. A manual
    override sticks within the band and the convention re-applies on a band change."""
    _fresh_state({"sdr_device": "hackrf:fake1"})
    win = _build()
    try:
        def off(mhz):
            win._set_freq(int(mhz * 1e6), how="typed")
            return win._offset_dir, round(win._offset_mhz, 4)
        assert off(446.000) == ("−", 5.0)
        assert off(145.230) == ("−", 0.6)
        assert off(147.210) == ("+", 0.6)
        assert off(29.600) == ("−", 0.1)
        assert off(223.500) == ("−", 1.6)
        assert off(927.500) == ("−", 25.0)
        assert off(146.520)[0] == "Simplex"           # 2 m national simplex: no standard offset
        assert off(100.100)[0] == "Simplex"           # out of band
        # a manual override holds across a same-band tune, convention returns on a band change
        off(446.000)
        win.offset_dir_combo.setCurrentText("Simplex")
        win._set_freq(int(446.200e6), how="typed")
        assert win._offset_dir == "Simplex"           # user override kept within 70 cm
        win._set_freq(int(146.940e6), how="typed")
        assert (win._offset_dir, round(win._offset_mhz, 3)) == ("−", 0.6)  # 2 m convention re-applied
        # a bank channel's own offset is not overridden by a later same-band typed tune
        win._apply_offset(1_000_000)                  # e.g. a recalled channel: +1 MHz
        win._set_freq(int(146.900e6), how="typed")
        assert win._offset_dir == "+" and round(win._offset_mhz, 3) == 1.0
    finally:
        win.close()
    print("ok  test_repeater_offset_auto_by_band")


def test_filtered_scan_keeps_hidden_enabled():
    """The crux: filtering the merged table then unchecking must NOT disable
    channels that are filtered out of view, and a scan iterates the full bank."""
    _fresh_state()
    win = _build()
    try:
        chans = [Channel(freq=154_000_000, mode="NFM", label="A", enabled=True),
                 Channel(freq=118_000_000, mode="AM", label="B", enabled=True),
                 Channel(freq=155_000_000, mode="NFM", label="C", enabled=True)]
        _set_bank(win, "T", chans)

        win.mode_filter.setCurrentText("NFM")   # -> _apply_filter -> _refresh_table
        assert win.table.rowCount() == 2, f"expected 2 NFM rows, got {win.table.rowCount()}"
        assert win._row_bankidx == [0, 2], win._row_bankidx

        win._on_check_all(False)                 # untick every VISIBLE row
        bank = win.store.bank()
        assert bank.channels[0].enabled is False, "A (visible) should be off"
        assert bank.channels[2].enabled is False, "C (visible) should be off"
        assert bank.channels[1].enabled is True, "B (hidden AM) must stay ENABLED"

        scan = win._scan_channels()
        freqs = {ch.freq for _i, ch in scan}
        assert freqs == {118_000_000}, f"scan should be just B, got {freqs}"
        assert bank.channels[1].enabled is True
    finally:
        win.close()
    print("ok  test_filtered_scan_keeps_hidden_enabled")


def test_import_export_on_active_bank(monkeypatch=None):
    _fresh_state()
    win = _build()
    try:
        found = [Channel(freq=154_100_000, mode="NFM", label="x"),
                 Channel(freq=155_100_000, mode="NFM", label="y")]
        win._channels_to_bank(found, "CSVtest", activate=True)
        assert win.store.active == "CSVtest", win.store.active
        # table shows the imported (active) bank
        assert win.table.rowCount() == 2

        # export all shown -> a new bank, via a stubbed input dialog
        orig = QtWidgets.QInputDialog.getItem
        QtWidgets.QInputDialog.getItem = staticmethod(
            lambda *a, **k: ("Exported", True))
        try:
            win._export_filtered()
        finally:
            QtWidgets.QInputDialog.getItem = orig
        assert "Exported" in win.store.banks
        assert len(win.store.bank("Exported").channels) == 2

        # add checked -> bank (all rows start ticked = enabled)
        QtWidgets.QInputDialog.getItem = staticmethod(
            lambda *a, **k: ("Picked", True))
        try:
            win._add_checked_to_bank()
        finally:
            QtWidgets.QInputDialog.getItem = orig
        assert "Picked" in win.store.banks
        assert len(win.store.bank("Picked").channels) == 2
    finally:
        win.close()
    print("ok  test_import_export_on_active_bank")


def _write_uls(path):
    """A tiny ULS dump: 3 active licenses (2 near Boston MA, 1 in Chicago) and
    1 expired one, with the HD/EN/FR/LO/EM field positions of the real files."""
    os.makedirs(path, exist_ok=True)

    def put(name, rows):
        with open(os.path.join(path, name), "w") as fh:
            for r in rows:
                fh.write("|".join(r) + "\n")

    def hd(usi, call, status, svc):
        return ["HD", usi, "", "", call, status, svc]

    def en(usi, call, name):
        return ["EN", usi, "", "", call, "L", "", name]

    def fr(usi, call, mhz):
        return ["FR", usi, "", "", call, "", "1", "1", "FB2", "", mhz]

    def lo(usi, call, city, st, lat, lon):
        r = ["LO", usi, "", "", call] + [""] * 22
        r[12], r[14] = city, st
        r[19:23] = [str(int(lat)), str(int(lat % 1 * 60)), "0", "N"]
        r[23:27] = [str(int(lon)), str(int(lon % 1 * 60)), "0", "W"]
        return r

    def em(usi, call, mhz, desig):
        return ["EM", usi, "", "", call, "1", "1", mhz, "", desig]

    put("HD.dat", [hd("1", "WQAA001", "A", "PW"), hd("2", "WQAA002", "A", "IG"),
                   hd("3", "KCHI003", "A", "IG"), hd("4", "WOLD004", "E", "PW")])
    put("EN.dat", [en("1", "WQAA001", "SAMPLE TOWN DPW"), en("2", "WQAA002", "ACME TAXI"),
                   en("3", "KCHI003", "LOOP COURIERS"), en("4", "WOLD004", "OLD PD")])
    put("FR.dat", [fr("1", "WQAA001", "453.25000"), fr("1", "WQAA001", "460.35000"),
                   fr("2", "WQAA002", "464.50000"), fr("3", "KCHI003", "453.25000"),
                   fr("4", "WOLD004", "155.10000")])
    put("LO.dat", [lo("1", "WQAA001", "BOSTON", "MA", 42.36, 71.06),
                   lo("2", "WQAA002", "CAMBRIDGE", "MA", 42.37, 71.11),
                   lo("3", "KCHI003", "CHICAGO", "IL", 41.88, 87.63),
                   lo("4", "WOLD004", "BOSTON", "MA", 42.36, 71.06)])
    put("EM.dat", [em("1", "WQAA001", "453.25000", "11K2F3E"),
                   em("1", "WQAA001", "453.25000", "8K10F1E"),
                   em("2", "WQAA002", "464.50000", "7K60FXE")])
    with open(os.path.join(path, "counts"), "w") as fh:
        fh.write("File Creation Date: Sun Oct  4 09:20:42 EDT 2026\n")


def _wait(cond, timeout=30):
    t = time.time()
    while not cond() and time.time() - t < timeout:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)
    return cond()


def test_fcc_download_done_then_import():
    # _on_dl_done once called a removed _path_hint(): the AttributeError left the
    # status at "downloading … (100%)". Now it opens the license search.
    _fresh_state()
    win = _build()
    try:
        win._dl_dataset = "LMcomm"
        win._on_dl_done(A._uls_dir())
        assert "search" in win.fetch_status.text(), win.fetch_status.text()
        assert win.download_btn.text() == "Download FCC data"
        assert win._uls_win is not None and win._uls_win.isVisible()
    finally:
        win.close()
    print("ok  test_fcc_download_done_then_import")


def test_fcc_search_without_import():
    # the downloaded FCC data is searchable (place / freq / text / service) before
    # anything is imported; results tune and copy into a bank on request
    from sdrexplorer.providers import fcc_uls
    uls = os.path.join(_TMP, "uls_search")
    shutil.rmtree(uls, ignore_errors=True)
    _write_uls(uls)
    _fresh_state()
    orig_dir = P.get("uls_dir")
    from sdrexplorer import ulsui
    orig_geo = ulsui.geocode.resolve
    ulsui.geocode.resolve = lambda s: (42.36, -71.06, "Boston, MA") if s else None
    P.override(uls_dir=uls)
    win = _build()
    try:
        banks_before = set(win.store.banks)
        win._show_uls_window()
        u = win._uls_win
        assert _wait(lambda: fcc_uls.index_fresh(uls) and u._index_worker is None), u.status.text()
        assert "4 licensed channels" in u.status.text(), u.status.text()   # expired one skipped
        assert set(win.store.banks) == banks_before         # nothing imported

        call_col = ulsui.COLS.index("Call")

        def run(where="", radius=25, fmin="", fmax="", text="", service="", emission=""):
            u.where.setText(where)
            u.radius.setValue(radius)
            u.fmin.setText(fmin)
            u.fmax.setText(fmax)
            u.text.setText(text)
            u.service.setCurrentIndex(max(0, u.service.findData(service)))
            u.emission.setCurrentIndex(max(0, u.emission.findData(emission)))
            u.search()
            assert _wait(lambda: u._search_worker is None), u.status.text()
            return [(u.table.item(r, 0).text(), u.table.item(r, call_col).text())
                    for r in range(u.table.rowCount())]

        near = run(where="Boston, MA")
        assert sorted(near) == [("453.25000", "WQAA001"), ("460.35000", "WQAA001"),
                                ("464.50000", "WQAA002")], near
        assert sorted(run(fmin="453.25")) == [("453.25000", "KCHI003"), ("453.25000", "WQAA001")]
        assert run(text="courier") == [("453.25000", "KCHI003")]
        assert run(where="Boston, MA", service="Business") == [("464.50000", "WQAA002")]
        u.fmin.setText("abc")                               # bad input: message, table kept
        u.search()
        assert "Freq" in u.status.text() and u._search_worker is None

        # emission designators -> readable names (column + per-designator tooltip)
        rows = run(where="Boston, MA", fmin="453.25")
        assert rows == [("453.25000", "WQAA001")]
        kind = u.table.item(0, ulsui.COLS.index("Emission type"))
        assert kind.text() == "NFM voice (12.5 kHz) / P25 Phase 1", kind.text()
        assert "8K10F1E — P25 Phase 1" in kind.toolTip(), kind.toolTip()
        assert u.table.item(0, ulsui.COLS.index("Designators")).text() == "11K2F3E,8K10F1E"
        # ... and the Type filter
        assert run(emission="P25 Phase 1") == [("453.25000", "WQAA001")]
        assert run(emission="DMR") == [("464.50000", "WQAA002")]
        assert run(emission="NXDN96") == []
        assert fcc_uls.emission_name("16K0F3E") == "FM voice (25 kHz)"
        assert fcc_uls.emission_name("8K10F1D") == "P25 Phase 1"
        assert fcc_uls.emission_name("12K5F1D") == "FM digital data, 12.5 kHz"   # ITU decode
        assert fcc_uls.emission_name("junk") == ""

        # tune a result
        assert run(emission="P25 Phase 1") == [("453.25000", "WQAA001")]
        u._tune(0)
        assert win.radio.freq_hz == 453_250_000, win.radio.freq_hz

        # copy all results into a bank
        orig = QtWidgets.QInputDialog.getItem
        QtWidgets.QInputDialog.getItem = staticmethod(lambda *a, **k: ("FCC test", True))
        try:
            run(where="Boston, MA")
            u._add_to_bank()
        finally:
            QtWidgets.QInputDialog.getItem = orig
        b = win.store.bank("FCC test")
        assert sorted(c.freq for c in b.channels) == [453_250_000, 460_350_000, 464_500_000]
        assert all(c.source == "fcc" for c in b.channels)

        # a newer download makes the index stale -> rebuilt on the next search
        old = time.time() - 60                              # index older than FR.dat
        os.utime(fcc_uls.index_path(uls), (old, old))
        assert not fcc_uls.index_fresh(uls)
        run(text="taxi")
        assert _wait(lambda: u._index_worker is None and u._search_worker is None)
        assert fcc_uls.index_fresh(uls)
        assert [r[1] for r in run(text="taxi")] == ["WQAA002"]
    finally:
        ulsui.geocode.resolve = orig_geo
        P.override(uls_dir=orig_dir)
        win.close()
    print("ok  test_fcc_search_without_import")


def test_persistence_migration():
    # a legacy state.json with the dropped src_bank key must load cleanly, and
    # a re-save must not re-emit it.
    _fresh_state({"src_bank": "Gone", "src_search": "fire", "bank": "Default"})
    win = _build()
    try:
        assert win.store.active in win.store.banks
        assert not hasattr(win, "_src_bank")
        win._save_state()
        with open(A._state_path()) as fh:
            saved = json.load(fh)
        assert "src_bank" not in saved, "src_bank must be dropped on save"
        # the waterfall toggles now persist off the menu actions
        assert "waterfall_scan" in saved and "wf_labels" in saved
    finally:
        win.close()
    print("ok  test_persistence_migration")


def test_label_style_persist_and_apply():
    """Label size/color load into the spectrum widget, apply live via the
    setters, and round-trip through save/reload."""
    _fresh_state({"wf_label_px": 16, "wf_label_color": "#ffcc00",
                  "wf_tick_color": "#00ffee", "map_label_px": 14,
                  "map_label_color": "#123456"})
    win = _build()
    try:
        # loaded values reach the spectrum widget at build time
        assert win.spectrum.marker_font_px == 16
        assert win.spectrum.marker_text_color == "#ffcc00"
        assert win.spectrum.marker_tick_color == "#00ffee"
        # live setters update both the app state and the widget
        win._set_wf_label_px(20)
        win._set_wf_label_color("#ffffff")
        win._set_wf_tick_color("#39ff88")
        assert win.spectrum.marker_font_px == 20
        assert win.spectrum.marker_text_color == "#ffffff"
        # map setters must not raise when the map view isn't built yet
        win._set_map_label_px(18)
        win._set_map_label_color("#abcdef")
        # persist + reload
        win._save_state()
        with open(A._state_path()) as fh:
            saved = json.load(fh)
        assert saved["wf_label_px"] == 20 and saved["wf_label_color"] == "#ffffff"
        assert saved["map_label_px"] == 18 and saved["map_label_color"] == "#abcdef"
    finally:
        win.close()
    print("ok  test_label_style_persist_and_apply")


def test_waterfall_toggle_only_on_radio():
    _fresh_state()
    win = _build()
    try:
        win._show_radio()
        win.act_wf_scan.setChecked(False)
        # sweeping simulation: scanning + not holding -> paused on Radio
        win._scanning = True
        win._scan_holding = False
        win._apply_waterfall_state()
        assert win.spectrum.timer.isActive() is False
        win.act_wf_scan.setChecked(True)         # -> _on_wf_scan_toggle
        assert win.spectrum.timer.isActive() is True, "Show-during-scan should resume"
        # switch to Memories: paused regardless of the toggle
        win._show_memories()
        assert win.spectrum.timer.isActive() is False
        win._scanning = False
    finally:
        win.close()
    print("ok  test_waterfall_toggle_only_on_radio")


def test_tuning_above_2147_mhz():
    """Frequencies above 2^31 Hz (2147.48 MHz) survive every Qt signal they travel through
    (pyqtSignal(int) is 32-bit and used to wrap them negative), and the readout has a GHz digit
    to click: 0154.0000's first digit -> 1154.0000."""
    _fresh_state()
    win = _build()
    try:
        disp = win.freq_disp
        disp.setFrequency(2_400_000_000)
        disp.tuned.emit(disp.frequency())                     # digit-click / entry path
        assert win.radio.freq_hz == 2_400_000_000, win.radio.freq_hz
        disp._entry = "5800.1250"; disp._commit_entry()       # typed entry, 5.8 GHz
        assert win.radio.freq_hz == 5_800_125_000, win.radio.freq_hz
        assert disp._text() == "5800.125000"
        win.spectrum.tuned.emit(5_800_300_000)                # waterfall click in a 5.8 GHz band
        assert win.radio.demod_hz == 5_800_300_000, win.radio.demod_hz
        got = []
        win.spectrum.selection_made.connect(lambda c, b: got.append((c, b)))
        win.spectrum.selection_made.disconnect(win._on_spectrum_selection)
        win.spectrum.selection_made.emit(3_456_789_000, 25_000)
        assert got == [(3_456_789_000, 25_000)], got
        # the GHz digit: always shown, so it can be clicked from a VHF frequency
        disp.setFrequency(154_000_000)
        assert disp._text() == "0154.000000"
        disp._entry = None                                    # display mode (spans are laid out)
        disp.grab()                                           # paint offscreen: lays out the digits
        first = disp._spans[0]
        assert first[2] == 1_000_000_000, disp._spans[:2]     # the leading digit = 1 GHz place
        ev = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPoint(first[0] + 2, 40),
                               QtCore.Qt.LeftButton, QtCore.Qt.LeftButton, QtCore.Qt.NoModifier)
        disp.mousePressEvent(ev)
        assert disp.frequency() == 1_154_000_000 and win.radio.freq_hz == 1_154_000_000
        disp.setFrequency(5_900_000_000); disp.tuned.emit(disp.frequency() + 1_000_000_000)
        disp.setFrequency(7_000_000_000)
        assert disp.frequency() == disp.MAX_HZ == 6_000_000_000
    finally:
        win.close()
    print("ok  test_tuning_above_2147_mhz")


def test_tuning_display_hz_digits():
    """The readout has 100 Hz, 10 Hz and 1 Hz digits: each click / right-click / wheel notch on one
    steps the radio by that much, and the digits never round (no float MHz)."""
    _fresh_state()
    win = _build()
    try:
        disp = win.freq_disp
        disp.setFrequency(7_030_000); disp.tuned.emit(disp.frequency())
        assert disp._text() == "0007.030000"
        disp.setFrequency(1_234_567_891)
        assert disp._text() == "1234.567891"                  # every digit exact, at > 2^31 Hz too
        disp.setFrequency(7_030_000); disp.tuned.emit(disp.frequency())
        disp.grab()
        places = [p for (_x0, _x1, p) in disp._spans]
        assert places == [10 ** k for k in range(9, -1, -1)], places      # 1 GHz ... 1 Hz
        spans = {p: (x0, x1) for (x0, x1, p) in disp._spans}
        assert spans[100][0] > spans[1000][1]                 # the gap between kHz and Hz digits

        def click(place, button=QtCore.Qt.LeftButton):
            x = (spans[place][0] + spans[place][1]) // 2
            disp.mousePressEvent(QtGui.QMouseEvent(QtCore.QEvent.MouseButtonPress, QtCore.QPoint(x, 40),
                                                   button, button, QtCore.Qt.NoModifier))

        def wheel(place, notches):
            x = (spans[place][0] + spans[place][1]) // 2
            ev = QtGui.QWheelEvent(QtCore.QPointF(x, 40), QtCore.QPointF(x, 40), QtCore.QPoint(0, 0),
                                   QtCore.QPoint(0, int(120 * notches)), QtCore.Qt.NoButton,
                                   QtCore.Qt.NoModifier, QtCore.Qt.NoScrollPhase, False)
            disp.wheelEvent(ev)

        click(1)
        assert disp.frequency() == win.radio.freq_hz == 7_030_001
        click(10); click(100)
        assert win.radio.freq_hz == 7_030_111, win.radio.freq_hz
        click(100, QtCore.Qt.RightButton); click(1, QtCore.Qt.RightButton)
        assert win.radio.freq_hz == 7_030_010
        wheel(10, 3)
        assert win.radio.freq_hz == 7_030_040
        wheel(1, -2)
        assert win.radio.freq_hz == 7_030_038
        wheel(1, 0.5); assert win.radio.freq_hz == 7_030_038  # half a notch: nothing yet
        wheel(1, 0.5); assert win.radio.freq_hz == 7_030_039  # ... the second half steps
        assert disp._text() == "0007.030039"
        disp._entry = "7.030012"; disp._commit_entry()        # typed entry to the Hz
        assert win.radio.freq_hz == 7_030_012
        assert A._fmt_mhz(154_000_000) == "154.0000" and A._fmt_mhz(7_030_050) == "7.03005"
        assert A._fmt_mhz(7_030_012) == "7.030012"
        # sub-kHz steps move only the demod: no hardware retune, same band
        center = win.radio.band_center_hz
        click(1); click(100); wheel(10, 2)
        assert win.radio.band_center_hz == center and win.radio.freq_hz == 7_030_133, \
            (win.radio.band_center_hz, center, win.radio.freq_hz)
        click(1000)                                           # a kHz step recenters, as before
        assert win.radio.freq_hz == 7_031_133 and win.radio.band_center_hz == 7_031_133 - __import__("sdrexplorer.radio", fromlist=["OFFSET"]).OFFSET
    finally:
        win.close()
    print("ok  test_tuning_display_hz_digits")


def test_reconfigurations_spaced_on_a_device():
    """Two flowgraph reconfigurations within RECONFIG_GAP_S while a device streams: the second waits (the
    RTL-SDR source aborted the app when torn down ~0.1 s after a restart, entering a module's mode). Nested locks don't
    wait, a file replay doesn't wait, and the depth is back at 0 after each unlock."""
    from sdrexplorer import radio as R
    r = R.ScannerRadio(audio_out=False)
    calls = []
    r._impl = type("Impl", (), {"lock": lambda self: calls.append("lock")})()
    orig_unlock = R.gr.top_block.unlock
    R.gr.top_block.unlock = lambda self: calls.append("unlock")
    try:
        r.running, r._is_replay = True, False
        t0 = time.monotonic()
        r.lock(); r.lock(); r.unlock(); r.unlock()          # nested: one reconfiguration, no wait
        assert time.monotonic() - t0 < 0.05 and r._lock_depth == 0
        r.lock(); r.unlock()                                 # right after: held back
        dt = time.monotonic() - t0
        assert R.RECONFIG_GAP_S - 0.02 <= dt < R.RECONFIG_GAP_S + 0.2, dt
        r._is_replay = True                                  # a file: no USB reader thread, no wait
        t1 = time.monotonic()
        r.lock(); r.unlock()
        assert time.monotonic() - t1 < 0.05
        assert calls == ["lock", "lock", "unlock", "unlock"] * 1 + ["lock", "unlock"] * 2, calls
    finally:
        R.gr.top_block.unlock = orig_unlock
        r.running = False
    print("ok  test_reconfigurations_spaced_on_a_device")


def test_tuner_lease_in_app():
    """Scan and the ADS-B park take the tuner lease: a scan refuses a module (said why), the user
    tuning by hand ends a scan and leaves ADS-B (to the frequency they chose), and a module's tune() is
    refused while someone else holds the band."""
    import queue as _q
    _fresh_state()
    win = _build()
    try:
        stopped = []
        win._stop_scan = lambda: stopped.append(True)
        assert win.tuner.acquire("scan", A.TUNE_SCAN, "scanning channels",
                                 on_preempt=lambda by: win._stop_scan())
        assert "scan" in win.tuner.may_tune("some.module")[1]
        ok, why = win.tune(460_000_000, owner="some.module")
        assert not ok and "scan" in why
        win._on_display_tuned(462_562_500)                   # by hand: the scan is pre-empted
        assert stopped == [True] and win.tuner.holder() is None
        assert win.radio.freq_hz == 462_562_500
        ok, _ = win.tune(463_000_000, owner="some.module")   # nobody holds it now
        assert ok and win.radio.freq_hz == 463_000_000
        # ADS-B holds the band while parked; tuning by hand leaves it, at the new frequency
        A.ADSB_AVAILABLE = True
        win.radio.start_adsb = lambda: setattr(win.radio, "adsb_q", _q.Queue())
        win.radio.stop_adsb = lambda: None
        win._ensure_map_window = lambda: None
        win._enter_mode("ADS-B")
        assert win._in_adsb and win.tuner.holder()[0] == "ADS-B"
        win._on_display_tuned(154_000_000)
        assert not win._in_adsb and win.tuner.holder() is None
        assert win.radio.freq_hz == 154_000_000 and win.mode_combo.currentText() != "ADS-B"
    finally:
        win.close()
    print("ok  test_tuner_lease_in_app")


def test_cw_mode():
    """CW: tuned on the carrier it beats to a 700 Hz tone, the side follows the offset (CW-U), the
    500 Hz filter rejects a signal 500 Hz away, and the UI enters CW with a 100 Hz snap grid, a
    symmetric marker and 50 Hz bandwidth steps."""
    from gnuradio import gr, blocks
    from sdrexplorer.radio import DemodChain, MODES, QUAD_RATE, AUDIO_RATE

    def tone(parts, bw=0, seconds=0.5):
        n = int(QUAD_RATE * seconds)
        t = np.arange(n) / QUAD_RATE
        x = sum(a * np.exp(2j * np.pi * f * t) for f, a in parts).astype(np.complex64)
        tb = gr.top_block()
        snk = blocks.vector_sink_f()
        tb.connect(blocks.vector_source_c(x.tolist(), False), DemodChain(MODES["CW"], bw), snk)
        tb.run()
        y = np.array(snk.data())[int(0.1 * AUDIO_RATE):]
        Y = np.abs(np.fft.rfft(y * np.hanning(len(y))))
        return np.fft.rfftfreq(len(y), 1.0 / AUDIO_RATE), Y

    for off, want in ((0, 700), (150, 850), (-150, 550)):
        f, Y = tone([(off, 1.0)])
        assert abs(f[Y.argmax()] - want) < 15, (off, f[Y.argmax()])
    f, Y = tone([(0, 1.0), (500, 1.0)])
    rej = 20 * np.log10(Y[np.argmin(abs(f - 1200))] / Y[np.argmin(abs(f - 700))])
    assert rej < -40, rej

    _fresh_state()
    win = _build()
    try:
        assert "CW" in [win.mode_combo.itemText(i) for i in range(win.mode_combo.count())]
        win.radio.set_frequency(7_030_000)
        win.mode_combo.setCurrentText("CW")
        assert win.radio.mode.name == "CW" and win._snap_khz == 0.1
        assert win.radio.demod_edges() == (-250, 250)          # on the carrier, not one-sided
        assert win.spectrum._snap_bw(430) == 450 and win.spectrum._snap_bw(10) == 50
        win._snap_enabled = True
        win._on_spectrum_click(7_030_036)
        assert win.radio.demod_hz == 7_030_000, win.radio.demod_hz
        sig, floor = win.radio.channel_power(win.radio.demod_hz, win.radio.current_bw())
        assert sig > -200                                       # a 500 Hz window still reads a bin
        win.mode_combo.setCurrentText("USB")
        assert win.spectrum._snap_bw(1400) == 1000              # SSB keeps its 1 kHz steps
        win.mode_combo.setCurrentText("CW")
        win._save_state()
        assert json.load(open(A._state_path()))["mode"] == "CW"
    finally:
        win.close()
    _fresh_state({"mode": "CW", "vfo_a": {"freq": 7_030_000, "mode": "CW"}})
    win = _build()
    try:
        assert win.radio.mode.name == "CW" and win.mode_combo.currentText() == "CW"
    finally:
        win.close()
    print("ok  test_cw_mode")


def test_snap_realigns_tuned_freq():
    """Changing the grid spacing (or turning Snap on) must re-snap the tuned
    frequency to the nearest grid point, so the marker/clicks stay aligned."""
    _fresh_state()
    win = _build()
    try:
        win.radio.set_frequency(154_000_000)     # band centered here
        win.radio.set_demod(154_007_000)         # 7 kHz off any 12.5k grid point
        assert win.radio.demod_hz == 154_007_000

        # spacing change with Snap ON -> re-snap to nearest 12.5 kHz grid point
        win._snap_enabled = True
        win._snap_khz = 12.5
        win._on_snap_khz_change("12.5")
        assert win.radio.demod_hz == 154_012_500, win.radio.demod_hz

        # Snap OFF: a spacing change must NOT move the tune
        win._snap_enabled = False
        win.radio.set_demod(154_003_000)
        win._on_snap_khz_change("25")
        assert win.radio.demod_hz == 154_003_000, "snap off must not retune"

        # toggling Snap ON realigns the current tune to the (25 kHz) grid
        win._on_snap_toggle(True)
        assert win.radio.demod_hz == 154_000_000, win.radio.demod_hz
    finally:
        win.close()
    print("ok  test_snap_realigns_tuned_freq")


def test_pcap_export_roundtrip():
    """write_pcap emits a valid raw-IP pcap: magic + linktype 101 + one record
    per frame with the exact bytes; captured payloads of several link types go to one
    file each, and ones without a link type aren't exported."""
    import struct
    from sdrexplorer.pcap import write_pcap, write_by_linktype
    frames = [(1_700_000_000_123, b"\x45\x00\x00\x14" + b"\x00" * 16),
              (1_700_000_001_456, b"\x45\x01\x00\x0a" + b"\xab" * 6)]
    path = os.path.join(_TMP, "out.pcap")
    assert write_pcap(path, frames) == 2
    with open(path, "rb") as fh:
        blob = fh.read()
    magic, vmaj, vmin, _tz, _sig, snap, net = struct.unpack("<IHHiIII", blob[:24])
    assert magic == 0xa1b2c3d4 and net == 101, (hex(magic), net)
    off, seen = 24, []
    while off < len(blob):
        sec, usec, incl, orig = struct.unpack("<IIII", blob[off:off + 16])
        off += 16
        seen.append((sec, usec, blob[off:off + incl]))
        off += incl
    assert [s[2] for s in seen] == [f[1] for f in frames], seen
    ents = [{"ts": 1, "raw": b"\x45" * 20, "linktype": 101}, {"ts": 2, "raw": b"\x45" * 20, "linktype": 101},
            {"ts": 3, "raw": b"\x01" * 8, "linktype": 147}, {"ts": 4, "raw": b"x", "linktype": None}]
    files = write_by_linktype(os.path.join(_TMP, "mixed.pcap"), ents)
    assert [(os.path.basename(p), lt, n) for p, lt, n in files] == [("mixed.pcap", 101, 2),
                                                                   ("mixed_lt147.pcap", 147, 1)], files
    with open(files[1][0], "rb") as fh:
        assert struct.unpack("<IHHiIII", fh.read(24))[6] == 147
    assert seen[0][0] == 1_700_000_000 and seen[0][1] == 123_000, seen[0]
    print("ok  test_pcap_export_roundtrip")


def test_save_to_memory_commit():
    """_commit_memory saves the tuned VFO into an existing OR new bank, keeps the
    metadata (name/service/tone/notes), and persists it."""
    _fresh_state()
    win = _build()
    try:
        win.radio.set_frequency(155_000_000)
        # save into a brand-new bank
        ch = Channel(freq=int(win.radio.freq_hz), mode=win.radio.mode.name,
                     label="Fire Disp", service="Fire", tone="131.8 PL",
                     notes="loud around noon", source="manual")
        name = win._commit_memory("Interesting", ch)
        assert name == "Interesting" and "Interesting" in win.store.banks
        saved = win.store.bank("Interesting").channels[-1]
        assert saved.freq == 155_000_000 and saved.label == "Fire Disp"
        assert saved.notes == "loud around noon" and saved.tone == "131.8 PL"

        # save another into the SAME (now existing) bank -> appended, not replaced
        win._commit_memory("Interesting",
                           Channel(freq=155_100_000, mode="NFM", label="two"))
        assert len(win.store.bank("Interesting").channels) == 2

        # empty bank name falls back to the active bank
        active = win.store.active
        n0 = len(win.store.bank(active).channels)
        win._commit_memory("", Channel(freq=156_000_000, mode="NFM"))
        assert len(win.store.bank(active).channels) == n0 + 1

        # notes round-trip through save/reload (banks.json)
        win.store.save()
        from sdrexplorer.banks import BankStore
        reloaded = BankStore(A._memory_path())
        assert reloaded.bank("Interesting").channels[0].notes == "loud around noon"
    finally:
        win.close()
    print("ok  test_save_to_memory_commit")


def test_edit_channel():
    """_commit_channel_edit updates an existing channel's freq/mode/name/
    service/tone/notes in place, persists it, and the change survives a
    reload — the fix for 'no way to edit a saved memory'."""
    _fresh_state()
    win = _build()
    try:
        ch = Channel(freq=155_000_000, mode="NFM", label="Fire Disp",
                     service="Fire", tone="131.8 PL", notes="", source="manual")
        win.store.create_bank("Interesting")
        win.store.add_channel(ch, "Interesting")
        bank = win.store.bank("Interesting")
        assert bank.channels[-1] is ch

        # a mode no loaded module handles (imported data) is kept as it is, and offered by the
        # edit dialog once a bank holds it
        win._commit_channel_edit(ch, 155_500_000, "MODE-X", "Fire Dispatch",
                                  "Fire/EMS", "131.8 PL", "loud around noon")
        assert ch.freq == 155_500_000 and ch.mode == "MODE-X"   # now editable
        assert "MODE-X" in win._known_modes()
        assert ch.label == "Fire Dispatch" and ch.service == "Fire/EMS"
        assert ch.notes == "loud around noon"

        # edited fields round-trip through save/reload (banks.json)
        win.store.save()
        from sdrexplorer.banks import BankStore
        reloaded = BankStore(A._memory_path())
        edited = reloaded.bank("Interesting").channels[-1]
        assert edited.freq == 155_500_000 and edited.mode == "MODE-X"
        assert edited.label == "Fire Dispatch" and edited.notes == "loud around noon"

        # module fields (Channel.ext) survive an edit and are listed read-only, also for a module
        # that isn't loaded
        ch.ext["absent"] = {"tg": 9}
        win._commit_channel_edit(ch, ch.freq, ch.mode, ch.label, ch.service, ch.tone, ch.notes)
        assert BankStore(A._memory_path()).bank("Interesting").channels[-1].fields_for("absent") == {"tg": 9}
        assert win._module_channel_summary(ch) == [("absent (not loaded)", "tg 9")]
    finally:
        win.close()
    print("ok  test_edit_channel")


def test_iq_record_scope():
    """IQ recording supports full-band and demod-window scope, with correct
    metadata, and the top-row combo selects it."""
    _fresh_state()
    win = _build()
    try:
        r = win.radio
        r.set_frequency(462_000_000)

        bandp = os.path.join(_TMP, "band.cs16")
        m = r.start_record(bandp, scope="band")
        assert m["scope"] == "band" and m["samp_rate"] == 2_400_000, m
        assert m["center_hz"] == r.band_center_hz, m
        r.stop_record()
        assert os.path.exists(bandp)

        demodp = os.path.join(_TMP, "demod.cs16")
        m2 = r.start_record(demodp, scope="demod")
        assert m2["scope"] == "demod" and m2["samp_rate"] == 240_000, m2
        assert m2["center_hz"] == r.demod_hz, m2
        r.stop_record()
        assert os.path.exists(demodp)

        # app wiring: the combo drives the scope + filename tag
        win.rec_scope_combo.setCurrentIndex(win.rec_scope_combo.findData("demod"))
        win._start_record()
        assert r._rec_scope == "demod", r._rec_scope
        assert win._rec_path and "chan" in os.path.basename(win._rec_path)
        win._stop_record()
    finally:
        win.close()
    print("ok  test_iq_record_scope")


def _make_cs16(path, n=24000, fs=240000, tone_hz=5000):
    """Write a synthetic interleaved-int16 IQ file (a complex tone)."""
    t = np.arange(n)
    z = np.exp(2j * np.pi * tone_hz / fs * t) * 0.5
    inter = np.empty(2 * n, dtype=np.int16)
    inter[0::2] = (z.real * 32767).astype(np.int16)
    inter[1::2] = (z.imag * 32767).astype(np.int16)
    inter.tofile(path)


def test_replay_keeps_vfo_in_narrow_file():
    """A VFO the user tuned inside a narrow sub-band recording (here 11.6 kHz around one NXDN
    channel, narrower than a 12.5 kHz NFM channel plus margin) survives Stop/Start and
    re-opening the file; a VFO outside the recording still falls back to its channel."""
    from sdrexplorer.radio import ScannerRadio
    p = os.path.join(_TMP, "narrow.cs16")
    _make_cs16(p, n=24000, fs=16326, tone_hz=500)
    meta = {"samp_rate": 16326.53, "center_hz": 463_611_760, "chan_hz": 463_611_923,
            "rf_bw_hz": 11593, "scope": "subband", "mode": "NFM"}
    stub = A.ScannerRadio.start
    A.ScannerRadio.start = _REAL_START
    try:
        r = ScannerRadio(audio_out=False)
        r.demod_hz = 154_000_000                         # tuned elsewhere: falls back
        r.set_replay(p, meta); r.start()
        assert not r.replay_kept_vfo and r.demod_hz == 463_611_923
        r.set_demod(463_608_000)                         # the user moves the VFO inside the file
        r.stop(); r.set_replay(p, meta); r.start()       # restart / re-open
        assert r.replay_kept_vfo and r.demod_hz == 463_608_000, r.demod_hz
        r.stop(); r.start()
        assert r.demod_hz == 463_608_000, r.demod_hz
        r.stop(); r.demod_hz = 463_640_000; r.start()   # outside the 11.6 kHz file: fall back
        assert not r.replay_kept_vfo and r.demod_hz == 463_611_923
        r.stop()
    finally:
        A.ScannerRadio.start = stub
    print("ok  test_replay_keeps_vfo_in_narrow_file")


def test_iq_replay_end_to_end():
    """Replay a real .cs16 through the actual GNU Radio flowgraph: sidecar drives
    band/demod, the rate is resampled to 2.4 Msps, samples flow, teardown is
    clean, and a full-band file skips the resampler."""
    import time
    from sdrexplorer.radio import ScannerRadio, SAMP_RATE
    p = os.path.join(_TMP, "replay240.cs16")
    _make_cs16(p)
    meta = {"samp_rate": 240000, "center_hz": 463_562_500,
            "chan_hz": 463_562_500, "mode": "NFM"}
    stub = A.ScannerRadio.start           # the harness stubs start(); replay needs
    A.ScannerRadio.start = _REAL_START    # the real flowgraph to actually build
    try:
        r = ScannerRadio(audio_out=False)             # real flowgraph, no ALSA
        r.set_replay(p, meta, loop=True)
        r.start()
        try:
            assert r.is_replay() and r.running
            assert r.band_center_hz == 463_562_500 and r.demod_hz == 463_562_500
            assert len(r._replay_blocks) == 6, r._replay_blocks   # file->s2c->scale->resamp->gate->throttle
            time.sleep(0.5)
            sig, floor = r.channel_power(r.demod_hz, 12500)
            assert sig > floor, (sig, floor)          # samples really flowing
        finally:
            r.stop()
        assert not r.running and r._replay_blocks == [] and not r.is_replay()

        # a full-band (2.4 Msps) capture needs no resampler
        r.set_replay(p, dict(meta, samp_rate=SAMP_RATE))
        r.start()
        assert len(r._replay_blocks) == 5, r._replay_blocks   # no resampler
        r.stop()
    finally:
        A.ScannerRadio.start = stub
    print("ok  test_iq_replay_end_to_end")


def test_subband_recording_survives_a_receiver_restart():
    """A sub-band recording hangs off the source block. Stopping the receiver (as PTT does to free the
    HackRF) ends it and removes its chain, so the restarted flowgraph has no stale source in it and runs;
    a failed reconfiguration never leaves the flowgraph locked; stop() always returns. (Before: the stale
    edges kept the old source in the graph, stop_record's disconnect from the new source raised between
    lock() and unlock(), and the next stop() / quit waited forever.)"""
    import threading
    import time
    from sdrexplorer.radio import ScannerRadio, SAMP_RATE
    p = os.path.join(_TMP, "restart.cs16")
    _make_cs16(p, n=SAMP_RATE * 2, fs=SAMP_RATE, tone_hz=100_000)
    meta = {"samp_rate": SAMP_RATE, "center_hz": 463_262_500, "chan_hz": 463_362_500, "mode": "NFM"}
    stub = A.ScannerRadio.start
    A.ScannerRadio.start = _REAL_START

    def returns(fn, what, timeout=15):
        done = threading.Event()
        err = []

        def run():
            try:
                fn()
            except Exception as e:                # noqa: BLE001
                err.append(e)
            done.set()
        threading.Thread(target=run, daemon=True).start()
        assert done.wait(timeout), f"{what} hung"
        if err:
            raise err[0]
    try:
        r = ScannerRadio(audio_out=False)
        r.set_replay(p, meta, loop=True)
        r.start()
        rec = os.path.join(_TMP, "restart_sub.cs16")
        r.start_record(rec, scope="subband", center_hz=463_362_500, bw_hz=25_000)
        time.sleep(0.3)
        returns(r.stop, "stop() with a sub-band recording")      # the receiver stops for TX
        assert not r.is_recording() and r._subband_blocks is None
        assert os.path.getsize(rec) > 0
        r.start()                                                 # PTT released: receive again
        time.sleep(0.4)
        sig, floor = r.channel_power(r.demod_hz, 12500)
        assert sig > floor, (sig, floor)                          # the restarted graph runs
        returns(lambda: r.set_mode("AM"), "a reconfiguration after the restart")
        # a failing reconfiguration raises, but leaves the graph unlocked
        try:
            with r._reconfig():
                r.disconnect(__import__('gnuradio').blocks.null_source(8), __import__('gnuradio').blocks.null_sink(8))
        except Exception:
            pass
        assert r._lock_depth == 0
        returns(r.stop, "stop() after a failed reconfiguration")
    finally:
        A.ScannerRadio.start = stub
    print("ok  test_subband_recording_survives_a_receiver_restart")


def test_replay_position_and_seek():
    """The playback bar: replay_position_s advances in real time on a real flowgraph,
    survives a lock()/unlock() (the file source's counter restarts), seek_replay jumps
    while playing and is remembered while stopped, and the bar's slider / labels follow."""
    from sdrexplorer.radio import SAMP_RATE
    p = os.path.join(_TMP, "seek.cs16")
    _make_cs16(p, n=SAMP_RATE * 6, fs=SAMP_RATE, tone_hz=100_000)        # 6 s
    meta = {"samp_rate": SAMP_RATE, "center_hz": 463_262_500,
            "chan_hz": 463_562_500, "mode": "NFM"}
    stub = A.ScannerRadio.start
    A.ScannerRadio.start = _REAL_START
    _fresh_state()
    win = _build()
    r = win.radio
    try:
        r.set_replay(p, meta, loop=False)
        win._sdr_device = A.IQ_FILE_DEVICE
        assert abs(r.replay_duration_s() - 6.0) < 1e-6
        assert r.seek_replay(2.0) and abs(r.replay_position_s() - 2.0) < 1e-6   # parked
        r.start()
        time.sleep(0.6)
        pos = r.replay_position_s()
        assert 2.0 < pos < 3.5, pos                         # started at 2 s, ~real time
        r.lock(); r.unlock()                                # counter restarts at 0
        time.sleep(0.3)
        assert r.replay_position_s() >= pos, (r.replay_position_s(), pos)
        assert r.seek_replay(5.0)
        time.sleep(0.3)
        assert 5.0 <= r.replay_position_s() < 5.9, r.replay_position_s()
        win._update_playback_bar()
        assert win.playback_bar.isVisibleTo(win)
        assert win.pb_total.text() == "0:06"
        assert win.pb_elapsed.text() in ("0:05", "0:06"), win.pb_elapsed.text()
        assert win.pb_slider.value() > 0.8 * win.PLAYBACK_STEPS
        win.pb_slider.setValue(win.PLAYBACK_STEPS // 6)     # drag to ~1 s and let go
        win._on_playback_release()
        time.sleep(0.2)
        assert 1.0 <= r.replay_position_s() < 1.7, r.replay_position_s()
        win._playback_skip(-10)                             # clamps at the start
        assert r.replay_position_s() < 0.5
        assert not r.seek_replay(-1) or r.replay_position_s() < 0.5

        # pause: the position freezes, the flowgraph keeps running, EOF isn't declared
        r.seek_replay(3.0)
        time.sleep(0.2)
        win._toggle_playback_pause()
        assert r.replay_paused() and win.pb_pause.text() == "▶"
        time.sleep(0.3)
        p0 = r.replay_position_s()
        time.sleep(1.5)
        assert abs(r.replay_position_s() - p0) < 0.1, (p0, r.replay_position_s())
        assert r.running and not r.replay_finished()
        sig_items = r._replay_blocks[-1].nitems_written(0)
        time.sleep(0.3)
        assert r._replay_blocks[-1].nitems_written(0) > sig_items   # downstream still fed
        win._toggle_playback_pause()                       # resume
        assert not r.replay_paused() and win.pb_pause.text() == "⏸"
        time.sleep(0.5)
        assert r.replay_position_s() > p0 + 0.3
    finally:
        r.stop()
        win.close()
        A.ScannerRadio.start = stub
    print("ok  test_replay_position_and_seek")


def _run_snapshot(win, subband=None, timeout_s=15):
    """Take a snapshot through the app and wait for the writer thread."""
    got = []
    win._take_snapshot(subband)
    assert win._snap_writer is not None
    win._snap_writer.done.connect(lambda p, s: got.append((p, s)))
    t0 = time.time()
    while win._snap_writer is not None and time.time() - t0 < timeout_s:
        _QAPP.processEvents()
        time.sleep(0.02)
    assert win._snap_writer is None, "snapshot did not finish"
    return win.rec_status.text()


def test_iq_snapshot_end_to_end():
    """SNAP on a running (replayed) band writes a band-scope cs16 + sidecar of
    the buffered past plus the post-click tail; a sub-band snapshot is the
    decimated slice; both files replay. Waterfall rows go alongside."""
    import glob as _glob
    from sdrexplorer.radio import SAMP_RATE, SNAPSHOT_POST_S
    p = os.path.join(_TMP, "snapsrc.cs16")
    _make_cs16(p, n=SAMP_RATE, fs=SAMP_RATE, tone_hz=200_000)       # 1 s, looped
    meta = {"samp_rate": SAMP_RATE, "center_hz": 463_262_500,
            "chan_hz": 463_562_500, "mode": "NFM"}
    stub = A.ScannerRadio.start
    A.ScannerRadio.start = _REAL_START
    _fresh_state({"snapshot_s": 1})
    win = _build()
    try:
        assert win.radio.snapshot_seconds == 1
        win.radio.set_replay(p, meta, loop=True)
        win.radio.start()
        time.sleep(1.5)
        win.spectrum._poll()                       # at least one waterfall row
        text = _run_snapshot(win)
        assert text.startswith("snapshot iq_snap_"), text
        snaps = _glob.glob(os.path.join(A._recordings_dir(), "iq_snap_*.cs16"))
        assert len(snaps) == 1, snaps
        side = json.load(open(snaps[0] + ".json"))
        assert side["scope"] == "band" and side["center_hz"] == 463_262_500
        assert side["samp_rate"] == SAMP_RATE and side["format"] == "cs16"
        dur = os.path.getsize(snaps[0]) / 4 / SAMP_RATE
        # 1 s before the click (if buffered) + the post-click tail, throttled replay
        assert 1.0 <= dur <= 1 + SNAPSHOT_POST_S + 0.2, dur
        assert abs(side["snapshot"]["duration_s"] - dur) < 0.01
        assert os.path.exists(snaps[0] + ".waterfall.npy")
        x = np.fromfile(snaps[0], np.int16)[:2 * 65536].astype(np.float32)
        z = x[0::2] + 1j * x[1::2]
        f = np.fft.fftfreq(len(z), 1 / SAMP_RATE)[np.argmax(np.abs(np.fft.fft(z)))]
        assert abs(f - 200_000) < 200, f            # the replayed tone, unshifted

        text = _run_snapshot(win, (463_462_500, 25_000))   # the tone at +200 kHz
        subs = _glob.glob(os.path.join(A._recordings_dir(), "iq_snapsub_*.cs16"))
        assert len(subs) == 1, (subs, text)
        side = json.load(open(subs[0] + ".json"))
        assert side["scope"] == "subband" and side["rf_bw_hz"] == 25_000
        assert side["center_hz"] == 463_462_500 and side["samp_rate"] < 100_000
        x = np.fromfile(subs[0], np.int16).astype(np.float32)
        z = (x[0::2] + 1j * x[1::2])[500:]
        f = np.fft.fftfreq(len(z), 1 / side["samp_rate"])[np.argmax(np.abs(np.fft.fft(z)))]
        assert abs(f) < 100, f                      # tone landed at baseband
    finally:
        win.radio.stop()
        win.close()
        A.ScannerRadio.start = stub
    print("ok  test_iq_snapshot_end_to_end")


def test_selection_menu_record_or_snapshot():
    """Right-click-drag no longer records straight away: a menu offers Record
    or Snapshot of the selected sub-band, and each choice does just that."""
    _fresh_state()
    win = _build()
    calls = []
    try:
        win._begin_recording = lambda *a: calls.append(("rec",) + a)
        win._take_snapshot = lambda *a: calls.append(("snap",) + a)
        win.radio.running = True                    # start() is stubbed
        seen = []

        def pick(label):
            def exec_(menu, *_a):
                acts = [a for a in menu.actions() if not a.isSeparator()]
                seen.append([(a.text(), a.isEnabled()) for a in acts])
                return next(a for a in acts if a.text().startswith(label))
            return exec_

        orig = QtWidgets.QMenu.exec_
        try:
            QtWidgets.QMenu.exec_ = pick("● Record")
            win._on_spectrum_selection(463_500_000, 50_000)
            QtWidgets.QMenu.exec_ = pick("◷ Snapshot")
            win._on_spectrum_selection(463_500_000, 50_000)
            QtWidgets.QMenu.exec_ = pick("Cancel")
            win._on_spectrum_selection(463_500_000, 50_000)
        finally:
            QtWidgets.QMenu.exec_ = orig
        assert calls == [("rec", "subband", 463_500_000, 50_000),
                         ("snap", (463_500_000, 50_000))], calls
        labels = [t for t, _ in seen[0]]
        assert any(t.startswith("◷ Snapshot this sub-band") for t in labels), labels
        assert any(t.startswith("◎ Record when a signal appears") for t in labels), labels
        assert all(en for t, en in seen[0][1:]), seen[0]
        assert win.spectrum._pending_sel is None    # highlight cleared after the menu
        win.radio.running = False
    finally:
        win.close()
    print("ok  test_selection_menu_record_or_snapshot")


def test_trigger_monitor():
    """TriggerMonitor: attack before starting, hang before stopping (bridges short gaps),
    hysteresis, max length, re-arm per transmission, one-shot, None = no measurement."""
    from sdrexplorer.rectrigger import TriggerMonitor
    m = TriggerMonitor(threshold_db=10, hang_s=2.0, max_s=20, attack_s=0.2, hysteresis_db=3)
    seq = []

    def run(level, t0, t1, dt=0.1):
        t = t0
        while t < t1 - 1e-9:
            ev = m.feed(level, round(t, 3))
            if ev:
                seq.append((ev, round(t, 1)))
            t += dt
    run(4, 0, 2)                    # noise: nothing
    run(12, 2, 2.15)                # a click shorter than the attack: nothing
    run(4, 2.15, 3)
    run(15, 3, 6)                   # a transmission
    run(8, 6, 7)                    # 2 dB under threshold, inside the hysteresis: keeps recording
    run(15, 7, 8)
    run(4, 8, 9)                    # a 1 s gap (< hang): keeps recording
    run(15, 9, 10)
    run(4, 10, 10.5)                # gone ...
    run(9, 10.5, 10.6)              # ... a noise spike below threshold doesn't reset the hang ...
    run(4, 10.6, 13)                # ... so it stops 2 s after 10.0, re-arms
    run(15, 13, 40)                 # a long one: capped at max_s, then starts again
    ev = [e for e, _ in seq]; at = [t for _, t in seq]
    assert ev == ["start", "stop", "start", "stop", "start"], seq
    assert abs(at[0] - 3.2) < 0.15 and abs(at[1] - 12.0) < 0.15, seq     # attack, hang
    assert abs(at[2] - 13.2) < 0.15, seq
    assert abs((at[3] - at[2]) - 20) < 0.15, seq                          # max_s cap
    assert at[4] - at[3] < 0.5, seq                                       # re-armed at once
    assert m.count == 3
    m2 = TriggerMonitor(one_shot=True)
    for t in range(40):
        m2.feed(20 if t < 10 else 0, t * 0.1)
    for t in range(40, 80):
        m2.feed(20, t * 0.1)
    assert m2.state == "done" and m2.count == 1
    assert TriggerMonitor().feed(None, 0) is None
    print("ok  test_trigger_monitor")


def test_triggered_recording_end_to_end():
    """Right-drag ▸ Record when a signal appears, on a real replay: radio.window_level sees
    the keyed tone over the floor; the app records a sub-band file per transmission
    (iq_trig_*, re-armed in between), with a pre-roll iq_pretrig_* from the snapshot ring;
    ■ DISARM / Esc stop watching and keep what was recorded."""
    import glob as _glob
    from sdrexplorer.radio import SAMP_RATE
    fs = SAMP_RATE
    off = 200_000                                    # tone 200 kHz above the band center
    t = np.arange(int(fs * 6))
    keyed = ((t / fs > 1.0) & (t / fs < 2.5)) | ((t / fs > 4.0) & (t / fs < 5.0))
    rng = np.random.default_rng(2)
    z = 0.002 * (rng.standard_normal(len(t)) + 1j * rng.standard_normal(len(t)))
    z = z + 0.3 * keyed * np.exp(2j * np.pi * off * t / fs)
    p = os.path.join(_TMP, "trig.cs16")
    inter = np.empty(2 * len(z), np.int16)
    inter[0::2] = np.round(z.real * 32767); inter[1::2] = np.round(z.imag * 32767)
    inter.tofile(p)
    meta = {"samp_rate": fs, "center_hz": 463_262_500, "chan_hz": 463_562_500, "mode": "NFM"}
    stub = A.ScannerRadio.start
    A.ScannerRadio.start = _REAL_START
    _fresh_state({"snapshot_s": 2, "trigger_hang_s": 0.8})
    win = _build()
    try:
        assert win._trigger_hang == 0.8
        win.radio.set_replay(p, meta, loop=False)
        win.radio.start()
        time.sleep(0.4)
        tone = 463_262_500 + off
        lvl = win.radio.window_level(tone, 25_000)
        assert lvl is not None and lvl < 8, lvl        # unkeyed: near the floor
        win._arm_trigger(tone, 25_000)
        assert win._trigger is not None and win.rec_btn.text() == "■ DISARM"
        assert win.spectrum._trigger_win == (tone, 25_000, True)
        # wait on the file position, not wall time (the replay can run slow under load): both
        # key-ups (1.0-2.5 s, 4.0-5.0 s) plus the 0.8 s hang must have played
        # hold our own reference: at end of file the app stops the replay and disarms the trigger
        # (win._trigger -> None), which used to race with the reads below (~1 run in 6)
        mon = win._trigger["mon"]
        t0 = time.time(); states = []
        while time.time() - t0 < 20:
            _QAPP.processEvents()
            states.append(mon.state)
            pos = win.radio.replay_position_s() or 0.0
            if pos >= 5.95 or win.radio.replay_finished():
                break
            time.sleep(0.02)
        t1 = time.time()                                # the 2nd key-up's 0.8 s hang may end at EOF
        while time.time() - t1 < 3.0 and (mon.state == "recording" or mon.count < 2):
            _QAPP.processEvents(); time.sleep(0.02)
        assert mon.count == 2, (mon.count, states[-20:])
        recs = sorted(_glob.glob(os.path.join(A._recordings_dir(), "iq_trig_*.cs16")))
        assert len(recs) == 2, recs
        for r_ in recs:
            side = json.load(open(r_ + ".json"))
            assert side["scope"] == "subband" and side["center_hz"] == tone and "trigger" in side
            dur = os.path.getsize(r_) / 4 / side["samp_rate"]
            assert 0.5 < dur < 3.0, (r_, dur)
        _QAPP.processEvents()
        if win._trigger is not None:                    # not already disarmed at end of file
            win._toggle_record()                        # ■ DISARM
        assert win._trigger is None and win.rec_btn.text() == "● REC"
        assert win.spectrum._trigger_win is None
        t1 = time.time()
        while win._snap_writer is not None and time.time() - t1 < 10:
            _QAPP.processEvents(); time.sleep(0.05)
        pre = _glob.glob(os.path.join(A._recordings_dir(), "iq_pretrig_*.cs16"))
        assert pre, "no pre-roll snapshot"
        for p_ in pre:                                  # only the last trigger_pre_s (default 2 s)
            side = json.load(open(p_ + ".json"))
            assert side["snapshot"]["duration_s"] <= win._trigger_pre_s + 0.05, side["snapshot"]
        assert len(_glob.glob(os.path.join(A._recordings_dir(), "iq_trig_*.cs16"))) == 2   # kept
        win._arm_trigger(tone, 25_000)
        ev = QtGui.QKeyEvent(QtCore.QEvent.KeyPress, QtCore.Qt.Key_Escape, QtCore.Qt.NoModifier)
        win.keyPressEvent(ev)                           # Esc disarms too
        assert win._trigger is None
    finally:
        win.radio.stop()
        win.close()
        A.ScannerRadio.start = stub
    print("ok  test_triggered_recording_end_to_end")


def test_recorded_frequency_index():
    """recindex.build merges the real-data store (exact crop frequencies, held-out flag) with
    recording sidecars (sub-band center, mode = folder name; heldout/ subfolder; full-band and
    noise skipped); the waterfall finds the badge under the cursor; the Settings toggle hides it."""
    from sdrexplorer import recindex as RI
    rd = os.path.join(_TMP, "recidx")
    for sub in ("P25", os.path.join("NFM", "heldout"), "noise", "old"):
        os.makedirs(os.path.join(rd, sub), exist_ok=True)

    def side(rel, **m):
        with open(os.path.join(rd, rel + ".cs16.json"), "w") as fh:
            json.dump(m, fh)
    side("P25/iq_sub_a_20260102_142000", scope="subband", center_hz=470_500_000, samp_rate=24000)
    side("NFM/heldout/iq_sub_b", scope="subband", center_hz=147_100_000, samp_rate=24000)
    side("noise/iq_sub_c", scope="subband", center_hz=241_000_000, samp_rate=80000)
    side("old/iq_band_d", scope="band", center_hz=455_000_000, samp_rate=9_600_000)

    class FakeStore:
        @staticmethod
        def held_out(_p):
            return ["iq_band_460.0000MHz_20260101_120000"]

        @staticmethod
        def examples(_p):
            return [("x", {"label": "DMR", "freq_hz": 461_012_500,
                           "recording": "iq_band_460.0000MHz_20260101_120000"}),
                    ("y", {"label": "DMR", "freq_hz": 461_013_000,
                           "recording": "iq_band_461.0000MHz_20260101_120500"}),
                    ("z", {"label": "NBFM", "freq_hz": 462_550_000, "recording": "iq_sub_f"}),
                    ("n", {"label": "noise", "freq_hz": 241_000_000, "recording": "iq_sub_c"})]
    idx = {round(e["freq_hz"] / 1e3): e for e in RI.build(rd, FakeStore)}
    assert set(idx) == {470500, 147100, 461012, 462550}, sorted(idx)
    assert idx[470500]["labels"] == ["P25"] and not idx[470500]["held_out"]
    assert idx[147100]["labels"] == ["NFM"] and idx[147100]["held_out"]        # heldout/ folder
    assert idx[461012]["recordings"] == 2 and idx[461012]["held_out"]          # 6.25 kHz merge
    assert idx[462550]["labels"] == ["NFM"]                                    # store name mapped
    assert RI.describe(idx[461012]) == "DMR ×2"
    assert RI.last_text(idx[461012]) == "last 2026-01-01 12:05"     # newest of the two stamps
    assert RI.last_text(idx[470500]) == "last 2026-01-02 14:20"     # from the file name
    assert idx[147100]["last"] is not None                          # no stamp: sidecar mtime

    _fresh_state()
    win = _build()
    try:
        win.spectrum.set_recorded(list(idx.values()))
        win.radio.band_center_hz = 470_000_000
        win.spectrum.reset_view()
        hit = win.spectrum.recorded_near(470_500_000)
        assert hit is not None and hit["labels"] == ["P25"]
        assert win.act_show_recorded.isChecked()
        win.act_show_recorded.setChecked(False)
        assert win.spectrum.recorded_near(470_500_000) is None
        win.act_show_recorded.setChecked(True)
        win.spectrum.set_recorded(list(idx.values()))
        assert win.spectrum.recorded_near(470_500_000) is not None
        win.spectrum.repaint()                              # paints without error
    finally:
        win.close()
    print("ok  test_recorded_frequency_index")


def test_open_iq_file_wiring():
    """The 'IQ file' picker wires replay + combo, and never clobbers the
    persisted hardware device (so launch never auto-opens a file dialog)."""
    _fresh_state()
    win = _build()
    try:
        p = os.path.join(_TMP, "wire.cs16")
        with open(p, "wb") as fh:
            fh.write(b"\x00" * 64)
        with open(p + ".json", "w") as fh:
            json.dump({"samp_rate": 240000, "center_hz": 463_000_000,
                       "chan_hz": 463_000_000, "mode": "NFM"}, fh)
        orig = QtWidgets.QFileDialog.getOpenFileName
        QtWidgets.QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (p, "f"))
        try:
            ok = win._open_iq_file()
        finally:
            QtWidgets.QFileDialog.getOpenFileName = orig
        assert ok and win.radio.device == A.IQ_FILE_DEVICE
        assert win.radio.replay["path"] == p
        assert win.radio.replay["meta"]["samp_rate"] == 240000
        assert win.sdr_combo.currentText() == A.IQ_FILE_DEVICE
        assert win._hw_device in ("hackrf:fake1", "rtlsdr:fake2")   # real device preserved
        # and the persisted device is the real one, not "IQ file"
        win._save_state()
        with open(A._state_path()) as fh:
            assert json.load(fh)["sdr_device"] != A.IQ_FILE_DEVICE
    finally:
        win.close()
    print("ok  test_open_iq_file_wiring")


def test_multi_bank_scan_selection():
    """The main-UI scan-bank picker scans one or more banks: the flat scan list
    spans every ticked bank's enabled channels, indices map back to (bank, ch),
    and no selection falls back to the active bank."""
    _fresh_state()
    win = _build()
    try:
        win.store.create_bank("Fire")
        win.store.bank("Fire").channels[:] = [
            Channel(freq=154_000_000, mode="NFM", label="F1", enabled=True),
            Channel(freq=154_100_000, mode="NFM", label="F2", enabled=False)]
        win.store.create_bank("Police")
        win.store.bank("Police").channels[:] = [
            Channel(freq=155_000_000, mode="NFM", label="P1", enabled=True)]
        win.store.set_active("Fire")
        win._refresh_bank_combo()
        win._refresh_table()             # sync _row_bankidx to the active (Fire) bank
        # the picker lists banks (minus ★ Priority)
        items = [win.scan_bank_combo.model().item(i).text()
                 for i in range(win.scan_bank_combo.model().rowCount())]
        assert "Fire" in items and "Police" in items and "★ Priority" not in items

        # nothing ticked -> scan the active bank only (F1 enabled, F2 not)
        win._scan_banks = []
        got = win._scan_channels()
        assert [c.label for _p, c in got] == ["F1"], got
        assert win._scan_bank_names() == ["Fire"]

        # tick both banks -> flat list spans both (enabled only)
        win._scan_banks = ["Fire", "Police"]
        win._refresh_bank_combo()        # reflect the selection in the picker
        assert set(win.scan_bank_combo.checked_items()) == {"Fire", "Police"}
        got = win._scan_channels()
        assert [c.label for _p, c in got] == ["F1", "P1"], got
        # worker echoes the position; _scan_entry maps it back to (bank, ch)
        assert win._scan_entry(0)[0] == "Fire" and win._scan_entry(0)[1].label == "F1"
        assert win._scan_entry(1)[0] == "Police" and win._scan_entry(1)[1].label == "P1"

        # persistence round-trips the selection
        win._save_state()
        with open(A._state_path()) as fh:
            assert json.load(fh)["scan_banks"] == ["Fire", "Police"]
    finally:
        win.close()
    print("ok  test_multi_bank_scan_selection")


def test_adsb_msg_sink():
    """AdsbMsgSink converts a gr-adsb 'decoded' PMT PDU into a Python dict on the
    queue (the pmt.car dict → adsb_q hand-off). Uses only core GNU Radio/pmt."""
    import queue as _q
    import pmt
    from sdrexplorer.radio import AdsbMsgSink
    q = _q.Queue()
    sink = AdsbMsgSink(q)
    meta = pmt.to_pmt({"icao": "a1b2c3", "callsign": "TEST123", "altitude": 10000,
                       "speed": 420.0, "heading": 90.0, "latitude": 42.0,
                       "longitude": -71.0, "num_msgs": 5})
    sink._on_msg(pmt.cons(meta, pmt.make_u8vector(1, 0)))
    d = q.get_nowait()
    assert d["icao"] == "a1b2c3" and d["callsign"] == "TEST123"
    assert abs(d["latitude"] - 42.0) < 1e-6 and d["num_msgs"] == 5
    assert "ac_class" not in d, "no identification bits -> no class"
    # gr-adsb sends np.nan for unresolved fields -> the table must show blank
    assert A.ScannerApp._fmt(float("nan"), "{:.0f}") == ""
    assert A.ScannerApp._fmt(None) == "" and A.ScannerApp._fmt(9000, "{:.0f}") == "9000"

    # Emitter category rides in the raw bits (pmt.cdr): the sink decodes it from
    # an identification message and caches it per ICAO for later position msgs.
    bits = [0] * 112
    for i, x in enumerate("00100"):
        bits[32 + i] = int(x)                 # type code 4
    for i, x in enumerate("111"):
        bits[37 + i] = int(x)                 # category 7 -> Rotorcraft
    idmeta = pmt.to_pmt({"icao": "beef01", "callsign": "HELO1"})
    sink._on_msg(pmt.cons(idmeta, pmt.init_u8vector(len(bits), bits)))
    d2 = q.get_nowait()
    assert d2["ac_class"] == "helo" and d2["emitter_category"] == "Rotorcraft", d2
    # a later position message (no category bits) still carries the cached class
    posmeta = pmt.to_pmt({"icao": "beef01", "latitude": 1.0, "longitude": 2.0})
    sink._on_msg(pmt.cons(posmeta, pmt.make_u8vector(1, 0)))
    d3 = q.get_nowait()
    assert d3["ac_class"] == "helo", d3
    print("ok  test_adsb_msg_sink")


def test_adsb_emitter_category():
    """Emitter-category decode: type code + 3-bit subfield -> (label, class)."""
    from sdrexplorer.radio import adsb_emitter_category as cat

    def frame(tc, ca):
        b = [0] * 112
        for i, x in enumerate(format(tc, "05b")):
            b[32 + i] = int(x)
        for i, x in enumerate(format(ca, "03b")):
            b[37 + i] = int(x)
        return b

    assert cat(frame(4, 7)) == ("Rotorcraft", "helo")
    assert cat(frame(4, 5)) == ("Heavy", "heavy")
    assert cat(frame(4, 1)) == ("Light", "light")
    assert cat(frame(3, 6)) == ("UAV", "uav")
    assert cat(frame(3, 2)) == ("Lighter-than-air", "balloon")
    assert cat(frame(4, 0)) is None           # "no category information"
    assert cat(frame(11, 3)) is None          # position msg, not identification
    assert cat([0] * 20) is None              # frame too short
    print("ok  test_adsb_emitter_category")


def test_adsb_monitor():
    """AdsbMonitor drains its queue and emits the aircraft dict on the main thread."""
    import queue as _q
    import time
    from sdrexplorer.monitors import AdsbMonitor
    q = _q.Queue()
    mon = AdsbMonitor(q)
    got = []
    mon.aircraft.connect(lambda d: got.append(d))
    mon.start()
    q.put({"icao": "abc123"})
    end = time.time() + 2
    while not got and time.time() < end:
        QtWidgets.QApplication.processEvents()
        time.sleep(0.02)
    mon.stop()
    mon.wait(1000)
    assert got and got[0]["icao"] == "abc123", got
    print("ok  test_adsb_monitor")


def test_adsb_chain_runs():
    """Full gr-adsb chain + AdsbMsgSink builds and runs clean in this GR build.
    Skipped when gr-adsb isn't on the path (normal for CI)."""
    import sdrexplorer.radio as R
    if not R.ADSB_AVAILABLE:
        print("ok  test_adsb_chain_runs (skipped: gr-adsb not on PYTHONPATH)")
        return
    import queue as _q
    import time
    import logging
    import pmt
    import adsb
    from gnuradio import gr, blocks
    from gnuradio.filter import rational_resampler_ccc
    logging.disable(logging.CRITICAL)          # silence gr-adsb DEBUG
    q = _q.Queue()
    tb = gr.top_block()
    src = blocks.null_source(gr.sizeof_gr_complex)
    thr = blocks.throttle(gr.sizeof_gr_complex, 2_400_000)
    from gnuradio import filter as gr_filter
    rs = rational_resampler_ccc(5, 6)
    dcb = gr_filter.dc_blocker_cc(64, True)
    mag = blocks.complex_to_mag_squared(1)     # power domain (matches production)
    fr = adsb.framer(2_000_000, 0.010)         # gr-adsb default threshold
    dm = adsb.demod(2_000_000)
    dec = adsb.decoder("All", "None", "None")
    ns = blocks.null_sink(gr.sizeof_float)
    sink = R.AdsbMsgSink(q)
    tb.connect(src, thr, rs, dcb, mag, fr, dm, ns)
    tb.msg_connect(dm, pmt.intern("demodulated"), dec, pmt.intern("demodulated"))
    tb.msg_connect(dec, pmt.intern("decoded"), sink, pmt.intern("decoded"))
    tb.start()
    time.sleep(0.6)
    tb.stop()
    tb.wait()
    print("ok  test_adsb_chain_runs (clean)")


def test_adsb_suspend_restore():
    """start_adsb must fully detach the analog audio chain and every module branch on the channel
    tap (taps.suspend) so the flowgraph re-validates on unlock; a partial disconnect orphans a
    port and crashes. stop_adsb must restore every suspended edge. With a module's audio input the speaker
    path ends in a mixer whose analog port must stay fed while ADS-B rests it (an unfed port made unlock()
    throw and left the graph locked: the app hung on the next stop). Uses IQ-file replay as the source;
    needs gr-adsb."""
    import sdrexplorer.radio as R
    if not R.ADSB_AVAILABLE:
        print("ok  test_adsb_suspend_restore (skipped: needs gr-adsb)")
        return
    import numpy as _np
    import time
    p = os.path.join(_TMP, "adsb_src.cs16")
    _np.zeros(2 * 100000, dtype=_np.int16).tofile(p)
    stub = A.ScannerRadio.start          # harness stubs start(); need the real one
    A.ScannerRadio.start = _REAL_START
    try:
        from gnuradio import blocks as _blocks, gr as _gr
        rad = R.ScannerRadio(audio_out=False)
        branch = _blocks.copy(_gr.sizeof_gr_complex)            # a module's branch on the channel tap
        sink = _blocks.null_sink(_gr.sizeof_gr_complex)
        rad.taps.attach(R.TAP_CHANNEL, branch, edges=[(branch, sink)])
        from sdrexplorer.api import AudioArbiter
        arb = AudioArbiter()
        arb.bind(rad)
        arb.open_input("test", rate=8000)                       # a module's audio: the speaker gets a mixer
        assert rad._mix is not None
        rad.set_replay(p, {"samp_rate": 2_400_000, "center_hz": R.ADSB_FREQ,
                           "chan_hz": R.ADSB_FREQ}, loop=True)
        rad.start()
        try:
            rad.start_adsb()                         # the call that used to crash
            assert rad._adsb_blocks and rad._adsb_suspended
            assert (rad.chan, rad.demod) in rad._adsb_suspended   # audio suspended
            assert rad.taps.suspended(R.TAP_CHANNEL)              # the module branch rests
            assert rad._adsb_filler                   # the mixer's analog port is fed silence meanwhile
            assert rad._lock_depth == 0
            time.sleep(0.4)
            rad.stop_adsb()
            assert not rad._adsb_suspended           # everything restored
            assert not rad._adsb_filler
            assert not rad.taps.suspended(R.TAP_CHANNEL)
            time.sleep(0.2)
            assert rad.running
        finally:
            rad.stop()                               # used to hang here
        assert not rad.running
    finally:
        A.ScannerRadio.start = stub
    print("ok  test_adsb_suspend_restore")


def test_adsb_mode_entry():
    """Entering ADS-B parks 1090 MHz, mutes analog, starts the branch + window;
    leaving restores the prior tune. (gr-adsb DSP stubbed — app wiring only.)"""
    import queue as _q
    orig_avail = A.ADSB_AVAILABLE
    _fresh_state()
    win = _build()
    try:
        A.ADSB_AVAILABLE = True
        rec = {"freqs": [], "muted": False, "started": False}
        win.radio.start_adsb = lambda: (setattr(win.radio, "adsb_q", _q.Queue()),
                                        rec.update(started=True))
        win.radio.stop_adsb = lambda: None
        win.radio.mute = lambda: rec.update(muted=True)
        # _start_adsb now pre-warms the map; don't spin up a real QtWebEngine
        # render process in the headless suite (its out-of-order teardown can
        # abort the run — same reason the map test avoids it). Wiring only here.
        win._ensure_map_window = lambda: None
        _setf = win.radio.set_frequency
        win.radio.set_frequency = lambda hz: (rec["freqs"].append(int(hz)), _setf(hz))
        win._enter_mode("ADS-B")
        assert win._in_adsb and rec["started"] and rec["muted"], rec
        assert A.ADSB_FREQ in rec["freqs"], rec["freqs"]
        assert win._adsb_win is not None and win.adsb_monitor is not None
        win._enter_mode("NFM")               # leave -> restore prior tune, stop branch
        assert not win._in_adsb and win.adsb_monitor is None
        # the display follows the restored tune, not just the radio
        assert win.freq_disp.frequency() == win.radio.freq_hz != A.ADSB_FREQ, \
            (win.freq_disp.frequency(), win.radio.freq_hz)
        # the same leaving ADS-B through the Mode picker, into CW
        win.mode_combo.setCurrentText("ADS-B")
        assert win.freq_disp.frequency() == A.ADSB_FREQ
        win.mode_combo.setCurrentText("CW")
        assert win.radio.mode.name == "CW" and not win._in_adsb
        assert win.freq_disp.frequency() == win.radio.freq_hz != A.ADSB_FREQ
    finally:
        A.ADSB_AVAILABLE = orig_avail
        win.close()
    print("ok  test_adsb_mode_entry")


def test_adsb_graceful_degrade():
    """With gr-adsb absent, selecting ADS-B shows a note and reverts — no crash."""
    orig_avail = A.ADSB_AVAILABLE
    _fresh_state()
    win = _build()
    try:
        A.ADSB_AVAILABLE = False
        win._enter_mode("ADS-B")
        assert not win._in_adsb
        assert win.mode_combo.currentText() != "ADS-B"
    finally:
        A.ADSB_AVAILABLE = orig_avail
        win.close()
    print("ok  test_adsb_graceful_degrade")


def test_rds_state_accumulation():
    """rds.RdsState merges gr-rds parser (type, msg) updates into the current
    station's fields, returning the merged dict only when something actually
    changes (dedup) -- pure Python, no GNU Radio needed."""
    from sdrexplorer.rds import RdsState
    st = RdsState()
    assert st.update(1, "KROQ") == {"ps": "KROQ"}
    assert st.update(1, "KROQ") is None, "unchanged repeat -> no update"
    d = st.update(4, "Now playing: test")
    assert d == {"ps": "KROQ", "rt": "Now playing: test"}, d
    assert st.update(99, "unknown type") is None, "unknown field type -> ignored"
    st.reset()
    assert st.update(1, "KROQ") == {"ps": "KROQ"}, "reset clears prior state"
    print("ok  test_rds_state_accumulation")


def test_rds_msg_sink():
    """RdsMsgSink converts gr-rds's rds.parser (type, msg) pmt tuples into
    merged station dicts on the queue, deduping repeats. Uses only core
    GNU Radio/pmt (gr-rds itself isn't required)."""
    import queue as _q
    import pmt
    from sdrexplorer.radio import RdsMsgSink
    q = _q.Queue()
    sink = RdsMsgSink(q)
    sink._on_msg(pmt.make_tuple(pmt.from_long(1), pmt.intern("KROQ")))
    d = q.get_nowait()
    assert d == {"ps": "KROQ"}, d
    sink._on_msg(pmt.make_tuple(pmt.from_long(4), pmt.intern("hello")))
    d2 = q.get_nowait()
    assert d2 == {"ps": "KROQ", "rt": "hello"}, d2
    sink._on_msg(pmt.make_tuple(pmt.from_long(1), pmt.intern("KROQ")))
    assert q.empty(), "unchanged PS repeat must not re-queue"
    print("ok  test_rds_msg_sink")


def test_rds_survives_samp_rate_change():
    """Regression: set_samp_rate() rebuilds self.chan (a new object) but used
    to leave the RDS parallel branch's first edge wired to the
    now-dead OLD chan object -- an orphaned block with an unconnected
    mandatory input, which GNU Radio rejects at the next start(), taking the
    whole flowgraph (and the SDR claim) down with it. Skipped when gr-rds
    isn't on the path (normal for CI). Replays a synthetic noise capture (via
    the real file-replay path, not real hardware) so the REAL GNU Radio
    scheduler validates the flowgraph topology -- only that is under test
    here, not RDS decode correctness (covered elsewhere)."""
    import sdrexplorer.radio as R
    if not R.RDS_AVAILABLE:
        print("ok  test_rds_survives_samp_rate_change (skipped: gr-rds not on PYTHONPATH)")
        return
    samp_rate = 240_000
    iq = (np.random.default_rng(0).standard_normal((samp_rate, 2)) * 3000).astype("<i2")
    path = os.path.join(_TMP, "rds_samprate_test.cs16")
    iq.tofile(path)
    meta = {"center_hz": 100_000_000, "chan_hz": 100_000_000,
            "samp_rate": float(samp_rate), "format": "cs16",
            "iq_size": 2, "iq_signed": True}

    sr = R.ScannerRadio(freq_hz=100_000_000, mode_name="WFM", audio_out=False)
    sr.set_replay(path, meta, loop=True)
    sr.start()
    sr.start_rds()
    old_chan = sr.chan
    assert sr._rds_blocks[0][0] is old_chan

    new_rate = 4_800_000 if sr.samp_rate != 4_800_000 else 9_600_000
    sr.set_samp_rate(new_rate)          # must not raise, must rewire the branch
    assert sr.chan is not old_chan, "sanity: the channelizer really was rebuilt"
    assert sr._rds_blocks[0][0] is sr.chan, \
        "RDS branch left wired to the dead old channelizer"
    sr.start()                          # would raise (unconnected input) pre-fix
    sr.stop_rds()
    sr.stop()
    print("ok  test_rds_survives_samp_rate_change")


def test_rds_graceful_degrade():
    """With gr-rds absent, entering WFM shows a note and doesn't crash."""
    from sdrexplorer import radio as R
    orig_avail = R.RDS_AVAILABLE
    _fresh_state()
    win = _build()
    try:
        R.RDS_AVAILABLE = False
        win._enter_mode("WFM")
        assert win.rds_monitor is None
        assert "gr-rds" in win.dig_status.text()
    finally:
        R.RDS_AVAILABLE = orig_avail
        win.close()
    print("ok  test_rds_graceful_degrade")


def test_rds_starts_on_restored_wfm_mode():
    """Regression: mode_combo.setCurrentText() (in _build_ui) runs BEFORE its
    currentTextChanged signal is connected, so _enter_mode() never fires for
    whatever mode state.json restores -- a session that saved off in WFM must
    still start the RDS branch on the next launch, not only on a manual mode
    switch."""
    _fresh_state({"mode": "WFM"})
    win = _build()
    try:
        assert win.radio.mode.name == "WFM"
        assert win.rds_monitor is not None, \
            "RDS branch must auto-start when WFM is the restored mode"
        assert win.radio.rds_q is not None
    finally:
        win.close()
    print("ok  test_rds_starts_on_restored_wfm_mode")


def test_target_store():
    """TargetStore: NaN positions don't map, moves grow a bounded de-duped
    track, attrs merge across updates, prune drops stale targets."""
    from sdrexplorer.targets import TargetStore
    clk = [1000.0]
    s = TargetStore(track_len=3, clock=lambda: clk[0])
    s.upsert("abc", "aircraft", lat=float("nan"), lon=float("nan"),
             altitude=35000, heading=90)
    assert len(s) == 1 and s.snapshot() == [], "no fix yet -> not on map"
    clk[0] = 1001.0
    s.upsert("abc", "aircraft", lat=40.0, lon=-74.0, label="UAL1", heading=95)
    t = s.snapshot()[0]
    assert t["label"] == "UAL1" and t["attrs"]["altitude"] == 35000 \
        and t["attrs"]["heading"] == 95, t
    s.upsert("abc", "aircraft", lat=40.1, lon=-74.1)
    s.upsert("abc", "aircraft", lat=40.1, lon=-74.1)          # dup ignored
    assert s.snapshot()[0]["track"] == [[40.0, -74.0], [40.1, -74.1]]
    for i in range(3):
        s.upsert("abc", "aircraft", lat=41.0 + i, lon=-74.0)
    assert len(s.snapshot()[0]["track"]) == 3, "track bounded to maxlen"
    s.upsert("R1", "radio", lat=42.0, lon=-71.0, speed_mps=5.0)
    assert sorted(x["kind"] for x in s.snapshot()) == ["aircraft", "radio"]
    clk[0] = 2000.0
    s.upsert("fresh", "radio", lat=1.0, lon=2.0)
    s.prune(300)
    assert [x["id"] for x in s.snapshot()] == ["fresh"], "stale pruned"
    print("ok  test_target_store")


def test_aircraft_feeds_map():
    """_on_aircraft upserts the aircraft into the shared TargetStore; a NaN
    position stays off the map, a resolved one appears with its callsign."""
    _fresh_state()
    win = _build()
    try:
        win._on_aircraft({"icao": "c0635f", "callsign": "SWA42",
                          "altitude": 12000, "heading": 270,
                          "latitude": float("nan"), "longitude": float("nan"),
                          "num_msgs": 3})
        assert len(win.targets) == 1 and win.targets.snapshot() == [], \
            "pre-CPR aircraft must not appear on the map"
        win._on_aircraft({"icao": "c0635f", "callsign": "SWA42",
                          "latitude": 33.9, "longitude": -118.4, "heading": 275})
        snap = win.targets.snapshot()
        assert len(snap) == 1 and snap[0]["kind"] == "aircraft" \
            and snap[0]["label"] == "SWA42", snap
        assert snap[0]["attrs"]["altitude"] == 12000, "attrs persist across msgs"

        # Emitter category flows to the map (ac_class -> icon) and the table.
        win._on_aircraft({"icao": "c0635f", "callsign": "SWA42",
                          "ac_class": "helo", "emitter_category": "Rotorcraft",
                          "latitude": 33.9, "longitude": -118.4})
        assert win.targets.snapshot()[0]["attrs"].get("ac_class") == "helo"
        hdr = [win._adsb_table.horizontalHeaderItem(c).text()
               for c in range(win._adsb_table.columnCount())]
        ccol = hdr.index("Class")
        row = win._adsb_rows["c0635f"]
        assert win._adsb_table.item(row, ccol).text() == "Rotorcraft"
    finally:
        win.close()
    print("ok  test_aircraft_feeds_map")


def test_annotation_tag_trails_a_stopped_signal():
    """A labeled signal that stops leaves its tag on the waterfall row it was last seen on; the
    tag scrolls down one row per waterfall row and is dropped when that row leaves the view. A
    signal that comes back gets a new tag at the top (the old one keeps scrolling). The module
    stopping or the band moving drops trail tags (the rows under them are another band)."""
    from sdrexplorer import spectrum as SP
    _fresh_state()
    win = _build()
    try:
        sp = win.spectrum
        sp._history.clear(); sp._trail_tags = []
        import types as _t
        lv = np.full(SP.NFFT, -100.0)
        sp._tx_probe = (_t.SimpleNamespace(level=lambda: lv), float(sp.radio.samp_rate), 0)

        def rows(n):
            for _ in range(n):
                sp._poll()
        rows(5)
        a = {"freq_hz": 470_100_000, "bw_hz": 8000, "label": "NXDN96", "raw_label": "NXDN96",
             "conf": 1.0, "confirmed": True}
        b = {"freq_hz": 470_300_000, "bw_hz": 8000, "label": "DMR", "raw_label": "DMR",
             "conf": 1.0, "confirmed": True}
        q = {"freq_hz": 470_500_000, "bw_hz": 8000, "label": "?", "raw_label": "?", "conf": 0.3,
             "confirmed": False}
        sp.set_detections([a, b, q])
        rows(3)
        sp.set_detections([b])                        # a and ? stopped
        assert [t["freq_hz"] for t in sp._trail_tags] == [470_100_000]   # '?' leaves no tag
        t0 = sp._trail_tags[0]["row"]
        rows(10)
        assert sp._rows_total - t0 == 10              # 10 rows down the waterfall
        sp.set_detections([a, b])                     # a is back: fresh tag at the top too
        assert sp._detections[0]["freq_hz"] == 470_100_000 and len(sp._trail_tags) == 1
        sp.set_detections([b])                        # stops again: the newer stop replaces it
        assert len(sp._trail_tags) == 1 and sp._trail_tags[0]["row"] == sp._rows_total
        win.resize(1200, 800); sp.resize(1100, 600)
        img = QtGui.QImage(sp.size(), QtGui.QImage.Format_RGB32)
        sp.label_source = "annotations"
        sp.render(img)                                # paints the trail tag without errors
        rows(SP.WF_ROWS + 1)                          # scrolled out of view
        assert sp._trail_tags == []
        sp.set_detections([a]); sp.set_detections([])          # b stopped, then a
        assert sorted(t["freq_hz"] for t in sp._trail_tags) == [470_100_000, 470_300_000]
        sp.radio.band_center_hz = sp.radio.band_center_hz + 1_000_000
        rows(1)
        assert sp._trail_tags == []                   # band moved
        sp.set_detections([a]); sp.set_detections([], trail=False)
        assert sp._trail_tags == []                   # the module stopped
    finally:
        win.close()
    print("ok  test_annotation_tag_trails_a_stopped_signal")


def main():
    _install_stubs()
    tests = [test_view_switch,
             test_menu_dialog_widgets_wired,
             test_tx_controls_gated_on_hackrf,
             test_repeater_offset_up_to_50_mhz, test_tx_ham_band_guard,
             test_vfo_shows_tx_frequency_during_tx,
             test_legacy_gains_migrate_to_device,
             test_foreign_gain_stage_pruned,
             test_repeater_offset_auto_by_band,
             test_filtered_scan_keeps_hidden_enabled,
             test_import_export_on_active_bank,
             test_fcc_download_done_then_import,
             test_fcc_search_without_import,
             test_persistence_migration,
             test_label_style_persist_and_apply,
             test_waterfall_toggle_only_on_radio,
             test_tuning_above_2147_mhz,
             test_snap_realigns_tuned_freq,
             test_cw_mode,
             test_reconfigurations_spaced_on_a_device,
             test_tuner_lease_in_app,
             test_tuning_display_hz_digits,
             test_pcap_export_roundtrip,
             test_save_to_memory_commit,
             test_edit_channel,
             test_iq_record_scope,
             test_iq_replay_end_to_end,
             test_subband_recording_survives_a_receiver_restart,
             test_replay_keeps_vfo_in_narrow_file,
             test_replay_position_and_seek,
             test_recorded_frequency_index,
             test_open_iq_file_wiring,
             test_iq_snapshot_end_to_end,
             test_selection_menu_record_or_snapshot,
             test_trigger_monitor,
             test_triggered_recording_end_to_end,
             test_multi_bank_scan_selection,
             test_adsb_msg_sink,
             test_adsb_emitter_category,
             test_adsb_monitor,
             test_adsb_chain_runs,
             test_adsb_suspend_restore,
             test_adsb_mode_entry,
             test_adsb_graceful_degrade,
             test_rds_state_accumulation,
             test_rds_msg_sink,
             test_rds_graceful_degrade,
             test_rds_survives_samp_rate_change,
             test_rds_starts_on_restored_wfm_mode,
             test_target_store,
             test_aircraft_feeds_map,
             test_annotation_tag_trails_a_stopped_signal]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    A.ScannerRadio.start = _REAL_START
    print(f"\n{len(tests) - failed}/{len(tests)} passed", flush=True)
    # Skip the normal interpreter shutdown: GNU Radio's C++ objects can segfault
    # when garbage-collected out of order at exit (harmless, but it corrupts the
    # process exit code). All work is done, so exit hard with the real status.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1 if failed else 0)


if __name__ == "__main__":
    main()
