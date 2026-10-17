#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Tests for the module framework (sdrexplorer.modules / sdrexplorer.api). No hardware; a throwaway
module repo is written to a temp directory and found the way a sibling sdrexplorer-* checkout is.

    QT_QPA_PLATFORM=offscreen python3 -m sdrexplorer.tests.test_modules
"""
import json
import os
import sys
import tempfile
import textwrap
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np                                     # noqa: E402
from PyQt5 import QtWidgets                            # noqa: E402

from sdrexplorer import paths as P                     # noqa: E402

_QAPP = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
_TMP = tempfile.mkdtemp(prefix="sdrexplorer-modtest-")
P.override(_TMP)

from sdrexplorer import app as A                       # noqa: E402
from sdrexplorer import modules as M                   # noqa: E402
from sdrexplorer import api                            # noqa: E402
from sdrexplorer.api import Decode, proofs, store      # noqa: E402
from sdrexplorer.api.testing import FakeRadio, FakeUI  # noqa: E402

_REAL_START = A.ScannerRadio.start

FAKE_MODULE = '''
from sdrexplorer.api import Module, ModeSpec, DemodSpec, MenuEntry, Overlay, Panel, Decode, TAP_CHANNEL, TxModeSpec
from gnuradio import blocks, gr

LOG = []


class Fake(Module):
    name = "fake"
    title = "Fake module"
    version = "1.2"
    api = ">=1.0,<2.0"

    def probe(self):
        return True, ""

    def attach_radio(self, radio, taps):
        super().attach_radio(radio, taps)
        self.sink = blocks.null_sink(gr.sizeof_gr_complex)
        taps.attach(TAP_CHANNEL, self.sink)
        LOG.append("attach_radio")

    def attach_ui(self, app, ui):
        super().attach_ui(app, ui)
        self.ui = ui
        ui.bus.subscribe("tuned", lambda **kw: LOG.append(("tuned", kw["freq_hz"], kw["primary"])))
        LOG.append("attach_ui")

    def modes(self):
        return [ModeSpec("FAKEMODE", picker="mode", demod="NFM",
                         enter=lambda app: LOG.append("enter"), leave=lambda app: LOG.append("leave")),
                ModeSpec("FAKEFM", demod=DemodSpec("fm", bw=16_000, max_dev=3_000, deemph=0.0),
                         enter=lambda app: LOG.append("enter fm")),
                ModeSpec("NEVER", available=lambda: (False, "needs a thing"))]

    def menu_entries(self):
        return [MenuEntry("View", "Fake thing", checkable=True, checked=True,
                          toggled=lambda on: LOG.append(("toggled", on)))]

    def overlays(self):
        return [Overlay("fake", lambda qp, r, wf, lo, hi: LOG.append("paint"))]

    def services(self):
        return {"fake": self}

    def sources(self):
        from sdrexplorer.api.testing import FakeSourceDriver
        from sdrexplorer.api.sources import Caps, GainStage
        return [FakeSourceDriver("fakemod", [("lime:mod1", "Module SDR",
                                             Caps(rate_ranges=[(1e6, 30e6)], gains=[GainStage("LNA", 0, 30, 1),
                                                  GainStage("TIA", 0, 12, 3), GainStage("PGA", 0, 31, 1)],
                                                  tx=True, full_duplex=True, dc_spike=False))])]

    def decodes(self):
        return {"FAKELABEL": lambda f, lab, info: LOG.append(("decode", f, lab)) or True}

    def tx_modes(self):
        from PyQt5 import QtWidgets

        def build(tb, audio, settings):
            from gnuradio import analog
            LOG.append(("tx build", settings.get("code")))
            mod = analog.frequency_modulator_fc(0.1)
            tb.connect(audio, mod)
            return mod
        self.tx_code = 5
        self._fake_quick = QtWidgets.QLabel("fake quick field")
        return [TxModeSpec("FAKETX", build, settings={"code": 5},
                           cleanup=lambda tb: LOG.append("tx cleanup"),
                           settings_widget=lambda parent: QtWidgets.QLabel("fake tx fields", parent),
                           quick_widget=lambda parent: self._fake_quick,
                           channel_tone=lambda: f"CODE {self.tx_code}",
                           apply_tone=lambda t: setattr(self, "tx_code", int(t.split()[-1]))
                                                if t.startswith("CODE") else None),
                TxModeSpec("NOTX", build, available=lambda: (False, "no encoder"))]

    def panels(self):
        from PyQt5 import QtWidgets
        return [Panel("Fake", lambda parent: QtWidgets.QLabel("fake panel", parent))]

    def context_entries(self, freq_hz, annotation):
        LOG.append(("context", freq_hz, (annotation or {}).get("label")))
        return [MenuEntry("", "Fake action", triggered=lambda: LOG.append("ctx action")),
                MenuEntry("", "Fake sub", submenu=[MenuEntry("", "Inner", triggered=lambda: LOG.append("inner"))])]

    def channel_fields(self, channel):
        return {"code": self.tx_code} if channel.mode == "FAKEFM" else None

    def channel_recalled(self, channel, fields):
        LOG.append(("recalled", channel.freq, dict(fields)))
        if "code" in fields:
            self.tx_code = fields["code"]

    def channel_summary(self, fields):
        return f"code {fields.get('code')}"

    def importers(self):
        from sdrexplorer.api import importers, FoundChannel
        return [importers.ImportProvider(
            "Fake list", lambda req: [FoundChannel(freq=146_520_000, label="calling", mode="FAKEFM", source="fake",
                                                   meta={"ext": {"fake": {"code": 9}}})],
            needs_location=False)]

    def load_state(self, d):
        self.state = d
        LOG.append(("load_state", dict(d)))

    def save_state(self):
        return {"n": 7}

    def shutdown(self):
        LOG.append("shutdown")


class Old(Module):
    name = "old"
    api = ">=2.0"


class Broken(Module):
    name = "broken"
    api = ">=1.0,<2.0"

    def probe(self):
        return False, "libfoo not found"


def PROOFS():
    return {"FAKE": lambda iq, fs: {"ok": True, "decoder": "fake", "frames": 1, "clean": 1, "crc_err": 0,
                                    "ids": [], "detail": ["fake"]}}
'''

PYPROJECT = '''
[project]
name = "sdrexplorer-fake"
version = "1.2"

[project.entry-points."sdrexplorer.modules"]
fake = "sdrexplorer_fake:Fake"
old = "sdrexplorer_fake:Old"
broken = "sdrexplorer_fake:Broken"

[project.entry-points."sdrexplorer.proofs"]
fake = "sdrexplorer_fake:PROOFS"
'''


def _make_repo():
    d = os.path.join(_TMP, "sdrexplorer-fake")
    os.makedirs(os.path.join(d, "src", "sdrexplorer_fake"), exist_ok=True)
    with open(os.path.join(d, "pyproject.toml"), "w") as fh:
        fh.write(textwrap.dedent(PYPROJECT))
    with open(os.path.join(d, "src", "sdrexplorer_fake", "__init__.py"), "w") as fh:
        fh.write(textwrap.dedent(FAKE_MODULE))
    return d


def _stubs():
    from sdrexplorer.api.testing import use_fake_sources
    use_fake_sources()
    A.ScannerRadio.start = lambda self: setattr(self, "running", True)
    A.explorer_available = lambda: (False, "disabled in tests")


def _build():
    w = A.ScannerApp()
    w.resize(900, 700)
    return w


REPO = _make_repo()
P.override(module_dirs=[REPO])


def _fake_log():
    import sdrexplorer_fake
    return sdrexplorer_fake.LOG


# ------------------------------------------------------------------------------------------- unit
def test_api_version_ranges():
    assert M.api_compatible(">=0.1,<0.2", (0, 1))
    assert not M.api_compatible(">=0.2", (0, 1))
    assert M.api_compatible("==0.1", (0, 1)) and not M.api_compatible("!=0.1", (0, 1))
    assert M.api_compatible("", (0, 1))                  # no constraint
    assert not M.api_compatible("~0.1", (0, 1))          # unknown operator: refused, not guessed
    print("ok  test_api_version_ranges")


def test_secrets():
    """api.secrets: round trip, owner isolation, delete, a missing store, and a 0600 file on disk."""
    from sdrexplorer.api import secrets as S
    path = S._path()
    for owner in ("alice", "bob"):
        for name in S.keys(owner):
            S.delete(owner, name)
    assert S.get("alice", "k") is None and S.keys("alice") == []   # empty store

    S.set("alice", "k", "secret-value")
    assert S.get("alice", "k") == "secret-value"
    assert S.keys("alice") == ["k"]
    assert S.get("bob", "k") is None                               # owners don't collide

    S.set("bob", "k", "other")
    S.set("alice", "k2", "v2")
    assert S.get("bob", "k") == "other"
    assert sorted(S.keys("alice")) == ["k", "k2"]

    # the file is 0600 and never world/group readable
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o600, oct(mode)

    S.delete("alice", "k")
    assert S.get("alice", "k") is None and S.keys("alice") == ["k2"]
    S.delete("alice", "nope")                                      # no error for a missing name

    # removing an owner's last key drops the owner but leaves others intact
    S.delete("alice", "k2")
    assert S.keys("alice") == [] and S.get("bob", "k") == "other"
    S.delete("bob", "k")
    print("ok  test_secrets")


def test_gains_per_device_and_live_apply():
    """Gains are strictly per device, and are (re)applied to the LIVE handle after the flowgraph starts
    (so gr-osmosdr honors the HackRF RF amp without a slider toggle). A fake HackRF + fake RTL keep
    their own gains; neither leaks onto the other (the old global-triple bug)."""
    from sdrexplorer.radio import ScannerRadio
    from sdrexplorer import sources as _src
    A.ScannerRadio.start = _REAL_START
    try:
        r = ScannerRadio(audio_out=False, device="hackrf:fake1")
        hackrf = _src.find("hackrf:fake1")
        rtl = _src.find("rtlsdr:fake2")
        # select the HackRF, set per-stage gains, start -> they reach the live handle while streaming
        r.set_device("hackrf:fake1", _src.settings(hackrf))
        assert r.devinfo is not None and r.devinfo.key == "hackrf:fake1"   # tracked before start
        r.set_gain("RF", 14)
        r.set_gain("IF", 24)
        r.set_gain("BB", 30)
        r.dev_settings["gains"]["LNA"] = 49    # a stray foreign stage (old cross-device leak)
        r.start()
        try:
            # the foreign LNA is filtered: only the device's real stages reach the live handle
            assert r.handle.gains == {"RF": 14, "IF": 24, "BB": 30}, r.handle.gains
        finally:
            r.stop()
        # switch to the RTL: its own (default) gains, and the HackRF's are untouched in its settings
        r.set_device("rtlsdr:fake2", _src.settings(rtl))
        assert r.devinfo.key == "rtlsdr:fake2"
        r.set_gain("TUNER", 42)
        r.start()
        try:
            assert r.handle.gains == {"TUNER": 42}, r.handle.gains      # no RF/IF/BB leak
        finally:
            r.stop()
        assert r.dev_settings["gains"] == {"TUNER": 42}
    finally:
        _stubs()
    print("ok  test_gains_per_device_and_live_apply")


def test_discovery_from_checkout():
    found = {f.name: f for f in M.discover()}
    assert set(found) == {"fake", "old", "broken"}, found
    assert found["fake"].ok and not found["fake"].error and found["fake"].version == "1.2"
    assert found["fake"].origin.startswith("checkout ")
    assert "needs module API" in found["old"].error
    assert not found["broken"].ok and found["broken"].why == "libfoo not found"
    assert found["broken"].status() == "unavailable: libfoo not found"
    # the checkout's proofs entry point registered its proof without loading the module
    assert proofs.get("FAKE") is not None and proofs.provider("FAKE") == "fake"
    assert proofs.verify(np.zeros(10, np.complex64), 24000, "FAKE")["ok"]
    assert not proofs.verify(np.zeros(10, np.complex64), 24000, "NOPE")["ok"]
    print("ok  test_discovery_from_checkout")


def test_switch_off_and_run_override():
    M.set_enabled("fake", False)
    try:
        assert "fake" in M.disabled()
        assert json.load(open(P.user_json()))["modules_off"] == ["fake"]
        f = next(f for f in M.discover() if f.name == "fake")
        assert not f.enabled and not M.should_run(f) and f.status() == "off"
        M.set_run_override({"fake"})                    # --modules fake beats the saved choice
        assert M.should_run(f)
        M.set_run_override(set())                       # --no-modules
        assert not M.should_run(f)
    finally:
        M.set_run_override(None)
        M.set_enabled("fake", True)
    assert "fake" not in M.disabled()
    print("ok  test_switch_off_and_run_override")


def test_audio_arbiter():
    a = api.AudioArbiter()
    seen = []
    a.changed = lambda arb: seen.append(arb.owner())
    assert a.analog_allowed() and a.owner() is None
    a.claim("deca")
    a.claim("deca")                                     # same claim again: no change event
    assert not a.analog_allowed() and a.owner() == "deca"
    a.claim("decb", priority=20)
    assert a.owner() == "decb"
    a.release("decb")
    assert a.owner() == "deca"
    a.release_all()
    assert a.analog_allowed() and seen == ["deca", "decb", "deca", None]
    a.analog_off = True
    assert not a.analog_allowed()
    print("ok  test_audio_arbiter")


def test_event_bus():
    b = api.EventBus()
    got = []
    b.subscribe("tuned", lambda **kw: got.append(kw))
    b.subscribe("tuned", lambda **kw: 1 / 0)            # a broken handler doesn't stop the others
    b.subscribe("tuned", lambda **kw: got.append("second"))
    b.emit("tuned", freq_hz=1, primary=True)
    assert got == [{"freq_hz": 1, "primary": True}, "second"]
    try:
        b.emit("no_such_event")
        raise AssertionError("unknown core topic accepted")
    except ValueError:
        pass
    mod = []
    b.subscribe("sat.pass", lambda **kw: mod.append(kw))       # a module topic: owner-prefixed, no declaration
    b.publish("sat.pass", aos=1, los=2)
    b.publish("nobody.listens", x=1)                          # publishing to no one is fine
    assert mod == [{"aos": 1, "los": 2}] and b.subscribers("sat.pass") == 1
    assert b.request("adsb.type", default="?", icao="A1") == "?"          # nobody answers: the default
    assert b.provide_request("adsb.type", lambda icao: "C130", owner="db")
    assert not b.provide_request("adsb.type", lambda icao: "X", owner="other")   # first provider wins
    assert b.request("adsb.type", icao="A1") == "C130" and b.can_answer("adsb.type")
    try:
        b.provide_request("unprefixed", lambda: 1)
        raise AssertionError("unprefixed request accepted")
    except ValueError:
        pass
    print("ok  test_event_bus")


def test_taps_on_fake_radio():
    r = FakeRadio()
    t = api.Taps(r)
    sink = object()
    t.attach(api.TAP_CHANNEL, sink)
    t.attach(api.TAP_CHANNEL, sink)                     # twice: still one edge
    assert r.edges == [(r.chan, sink)]
    new = object()
    t.channel_rebuilt(r.chan, new)
    assert r.edges == [(new, sink)]
    r.chan = new
    src_blk = object()
    t.attach(api.TAP_SOURCE, src_blk)                   # radio stopped: only remembered
    assert t.source_targets() == [src_blk] and (None, src_blk) not in r.edges
    t.detach(api.TAP_CHANNEL, sink)
    assert r.edges == []
    # suspend: whole branches off the point; attach meanwhile is remembered; resume puts all back
    a, a2, b = object(), object(), object()
    t.attach(api.TAP_CHANNEL, a, edges=[(a, a2)])            # a branch: its own edge goes with it
    assert r.edges == [(a, a2), (r.chan, a)]
    t.suspend(api.TAP_CHANNEL)
    assert r.edges == [] and t.suspended(api.TAP_CHANNEL)
    t.attach(api.TAP_CHANNEL, b)
    assert r.edges == []
    t.resume(api.TAP_CHANNEL)
    assert r.edges == [(a, a2), (r.chan, a), (r.chan, b)] and not t.suspended(api.TAP_CHANNEL)
    t.detach(api.TAP_CHANNEL, a)
    assert r.edges == [(r.chan, b)]
    print("ok  test_taps_on_fake_radio")


def test_ext_registries():
    api.ext.reset()
    api.ext.add("db.aircraft", {"icao": "A1"}, owner="early")        # before it's declared: kept
    api.ext.declare("db.aircraft", doc="ICAO -> type", owner="db")
    api.ext.add("db.aircraft", {"icao": "B2"}, owner="late")
    assert [e["icao"] for e in api.ext.entries("db.aircraft")] == ["A1", "B2"]
    assert api.ext.entries("nothing.here") == []
    assert api.ext.registries()["db.aircraft"][:3] == ("db", True, 2)
    print("ok  test_ext_registries")


def test_audio_input_and_paths_register():
    """An AudioInput buffers what a module writes (int16 or float), drops the oldest beyond MAX_S, and is
    silent when nothing is written; a module's own path key resolves like the core's."""
    a = api.AudioArbiter()
    i = a.open_input("m", rate=8000)
    i.write(np.full(800, 16384, np.int16))
    out = i.read(1000)
    assert abs(out[:800].mean() - 0.5) < 1e-3 and not out[800:].any()
    i.write(np.ones(int(8000 * (i.MAX_S + 1)), np.float32))
    assert abs(i.buffered_s() - i.MAX_S) < 1e-3 and i.dropped == 8000
    a.release_all()
    assert i.buffered_s() == 0                                         # a retune drops queued audio
    i.close()
    assert a.inputs() == []
    a.mute_analog("m", True)
    assert not a.analog_allowed()
    a.mute_analog("m", False)
    a.claim("m", label="VOICE")
    assert a.label() == "VOICE"
    api.paths.register("fakebuild", "a fake external build")
    os.environ["SDREXPLORER_FAKEBUILD"] = _TMP
    try:
        assert api.paths.get("fakebuild") == os.path.abspath(_TMP) and api.paths.ext("fakebuild")
    finally:
        del os.environ["SDREXPLORER_FAKEBUILD"]
    # a default derived from another key, an old variable name, extra Python dirs, a program build
    base = os.path.join(_TMP, "fakeprefix")
    os.makedirs(os.path.join(base, "apps", "tx"), exist_ok=True)
    api.paths.register("fakeprefix", "a prefix", legacy_env=("FAKE_PREFIX_OLD",))
    api.paths.register("fakeapps", "its apps", py_dirs=(".", "tx"),
                       default=lambda: os.path.join(api.paths.get("fakeprefix") or "", "apps"))
    api.paths.register("fakeprog", "a program", program=True)
    os.environ["FAKE_PREFIX_OLD"] = base
    os.environ["SDREXPLORER_FAKEPROG"] = base
    try:
        assert api.paths.lookup("fakeprefix") == (base, "env FAKE_PREFIX_OLD")
        assert api.paths.get("fakeapps") == os.path.join(base, "apps")
        py = api.paths.tool_env("fakeapps")["PYTHONPATH"].split(os.pathsep)
        assert py[:2] == [os.path.join(base, "apps"), os.path.join(base, "apps", "tx")], py
        assert not api.paths.tool_env("fakeprog", env={}).get("PYTHONPATH")
    finally:
        del os.environ["FAKE_PREFIX_OLD"], os.environ["SDREXPLORER_FAKEPROG"]
    print("ok  test_audio_input_and_paths_register")


def test_tuner_lease():
    """One holder; higher priority pre-empts (the old holder is told), equal / lower is refused; the user
    beats everyone; release by the holder only."""
    from sdrexplorer.api.tuner import Tuner
    t = Tuner()
    told = []
    assert t.may_tune("x") == (True, "")
    assert t.acquire("hunt", 60, "signal hunt", on_preempt=lambda by: told.append(("hunt", by)))
    assert not t.may_tune("scan")[0] and "hunt" in t.may_tune("scan")[1]
    assert not t.acquire("other", 60) and not t.acquire("low", 10)      # equal / lower: refused
    assert t.acquire("scan", 100, on_preempt=lambda by: told.append(("scan", by)))
    assert told == [("hunt", "scan")] and t.holder()[0] == "scan"
    assert not t.release("hunt") and t.holder()[0] == "scan"            # only the holder releases
    assert t.user_tunes() == "scan" and told[-1] == ("scan", "user") and t.holder() is None
    assert t.user_tunes() is None
    print("ok  test_tuner_lease")


def test_store_roundtrip():
    root = os.path.join(_TMP, "store")
    d = store.add("vu", "NBFM", np.ones(48000, np.complex64), 24000, 462.5e6, "rec1", root=root, bw_hz=12500)
    m = json.load(open(os.path.join(d, "meta.json")))
    assert m["format"] == store.FORMAT and m["bw_hz"] == 12500
    assert [x[1]["label"] for x in store.examples("vu", root=root)] == ["NBFM"]
    assert store.summary(root=root)["vu"]["NBFM"]["examples"] == 1
    store.set_held_out("vu", ["rec1"], root=root)
    X, y, rec = store.to_windows("vu", 4096, ["noise", "NBFM"], held=True, root=root)
    assert len(X) == 0 or X.shape[1:] == (2, 1, 4096)
    assert store.root() == P.get("store_dir")
    print("ok  test_store_roundtrip")


# ------------------------------------------------------------------------------------------- in the app
def test_module_loads_into_app():
    log = _fake_log()
    log.clear()
    os.makedirs(os.path.dirname(A._state_path()), exist_ok=True)
    with open(A._state_path(), "w") as fh:
        json.dump({"modules": {"fake": {"saved": 1}, "gone": {"keep": True}}}, fh)
    w = _build()
    try:
        fake = w.modules.get("fake")
        assert fake is not None and w.modules.get("old") is None and w.modules.get("broken") is None
        assert log[:2] == ["attach_radio", "attach_ui"] and ("load_state", {"saved": 1}) in log
        # its channel tap is wired into the real (unstarted) flowgraph
        assert any(b is fake.sink for b in w.radio.taps.attached(api.TAP_CHANNEL))
        # modes: in the pickers; the unavailable one is listed with its reason
        items = [w.mode_combo.itemText(i) for i in range(w.mode_combo.count())]
        assert "FAKEMODE" in items and "NEVER" in items
        assert w.service("fake") is fake and w.service("nothing") is None
        # decodes(): a label routed to the module that declared it; an unknown one does nothing
        assert w.request_decode("FAKELABEL", 151_000_000, {}) and log[-1] == ("decode", 151_000_000, "FAKELABEL")
        assert not w.request_decode("UNKNOWN", 151_000_000, {})
        # a mode with its own DemodSpec is built by the core
        w.mode_combo.setCurrentText("FAKEFM")
        assert w.radio.mode.name == "FAKEFM" and w.radio.mode.max_dev == 3_000 and log[-1] == "enter fm"
        w.mode_combo.setCurrentText("NFM")
        # its source driver: its device is in the SDR picker, with the gain stages it reports
        keys = [w.sdr_combo.itemData(i) for i in range(w.sdr_combo.count())]
        assert "lime:mod1" in keys, keys
        w._on_sdr_change("lime:mod1")
        assert w.radio.device == "lime:mod1" and w._gain_box.count() == 3
        assert w.tx_mode_combo.isEnabled()
        ok, why = A.ModuleUI(w, fake).tune(150_000_000)           # a module tunes when nobody holds the band
        assert ok and w.radio.freq_hz == 150_000_000
        w.tuner.acquire("other", 50)
        ok, why = A.ModuleUI(w, fake).tune(151_000_000)
        assert not ok and "other" in why
        w.tuner.release("other")
        # annotations: drawn, found near a frequency, published; a module's right-click entries
        from sdrexplorer.api import Annotation
        got = []
        w.bus.subscribe("annotations", lambda source, items: got.append((source, len(items))))
        ui2 = A.ModuleUI(w, fake)
        ui2.annotate([Annotation(freq_hz=w.radio.band_center_hz + 200_000, bw_hz=12_500, label="FAKE-SIG",
                                 confidence=0.9, raw="FAKESIG")])
        near = w.annotation_near(w.radio.band_center_hz + 201_000)
        assert near and near["label"] == "FAKE-SIG" and near["raw_label"] == "FAKESIG" and near["source"] == "fake"
        assert got == [("fake", 1)]
        from PyQt5 import QtWidgets as _QW
        menu = _QW.QMenu()
        w._add_module_context_entries(menu, w.radio.band_center_hz + 200_000, near)
        texts = [a.text() for a in menu.actions() if not a.isSeparator()]
        assert texts == ["Fake action", "Fake sub"], texts
        assert log[-1] == ("context", w.radio.band_center_hz + 200_000, "FAKE-SIG")
        next(a for a in menu.actions() if a.text() == "Fake action").trigger()
        assert log[-1] == "ctx action"
        sub = next(a for a in menu.actions() if a.text() == "Fake sub").menu()
        sub.actions()[0].trigger()
        assert log[-1] == "inner"
        ui2.set_status_box([("fake status", "#ffffff")])
        assert w.spectrum._status_box == [("fake status", "#ffffff")]
        ui2.set_status_box(None)
        ui2.annotate([], trail=False)
        assert w.annotation_near(w.radio.band_center_hz + 201_000) is None
        # its panel is a tab in the decode strip
        assert w.module_tabs.isVisibleTo(w) and w.module_tabs.tabText(0) == "Fake"
        # publish(Decode): logged once per dedup key, counted for recordings, on the "decode" topic
        seen = []
        w.bus.subscribe("decode", lambda record: seen.append(record.protocol))
        w._rec_labels = A.RecordingLabels(151_000_000)
        ui = A.ModuleUI(w, fake)
        for _ in range(6):
            ui.publish(Decode(source="", protocol="FAKEP", text="FAKE frame 1", ids={"unit": 7}, dedup_key=1))
        assert w.dig_log.toPlainText().count("FAKE frame 1") == 1 and seen == ["FAKEP"] * 6
        sm = w._rec_labels.summary()
        assert sm["label"] == "FAKEP" and sm["protocols"]["FAKEP"]["unit"] == [7], sm
        w._rec_labels = None
        # entering the module's mode sets its analog demod first, then calls enter
        w.mode_combo.setCurrentText("FAKEMODE")
        assert w._module_mode == "FAKEMODE" and log[-1] == "enter" and w.radio.mode.name == "NFM"
        w.mode_combo.setCurrentText("AM")
        assert w._module_mode is None and "leave" in log and w.radio.mode.name == "AM"
        w.mode_combo.setCurrentText("NEVER")
        assert w.mode_combo.currentText() == "AM" and "needs a thing" in w.dig_status.text()
        # menu entry, overlay, tune event
        act = next(a for a in w._module_actions if a.text() == "Fake thing")
        assert act.isChecked() and act in w._menus["View"].actions()
        act.setChecked(False)
        assert log[-1] == ("toggled", False)
        from sdrexplorer.spectrum import blank_dc
        from sdrexplorer.radio import NFFT
        w.spectrum._data = blank_dc(np.full(NFFT, -90.0), samp_rate=w.radio.samp_rate)
        w.spectrum._history.append(w.spectrum._data.astype(np.float32))
        w.spectrum.grab()                                  # paints the overlay
        assert "paint" in log
        w._on_spectrum_click(w.radio.band_center_hz + 400_000)
        assert log[-1][0] == "tuned" and log[-1][2] is False
        # a module TX mode: in the TX: picker, its fields in Settings > Transmit, built by TxChain
        txs = [w.tx_mode_combo.itemText(i) for i in range(w.tx_mode_combo.count())]
        assert "FAKETX" in txs and "NOTX" in txs, txs
        assert any(isinstance(c, _QW.QLabel) and c.text() == "fake tx fields"
                   for c in w._tx_ext_box.findChildren(_QW.QLabel))
        # the TX mode's quick widget (api.TxModeSpec.quick_widget) sits in the SDR strip, shown only
        # while that mode is the selected TX mode
        qw = w._tx_quick_widgets.get("FAKETX")
        assert qw is not None
        # isVisibleTo(window): the window itself is never shown under offscreen Qt
        w.tx_mode_combo.setCurrentText("Analog")
        assert not qw.isVisibleTo(w)
        w.tx_mode_combo.setCurrentText("FAKETX")
        assert qw.isVisibleTo(w)
        assert w._tx_mode() == "FAKETX" and w._tx_mode_available("FAKETX")
        assert not w._tx_mode_available("NOTX")
        from sdrexplorer.radio import TxChain
        import wave
        wav = os.path.join(_TMP, "tx.wav")
        with wave.open(wav, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(48_000)
            wf.writeframes(b"\0\0" * 4800)
        import osmosdr as _osm
        real_sink = _osm.sink
        _osm.sink = lambda args="": (_ for _ in ()).throw(RuntimeError("no sink in the test"))
        try:
            try:
                TxChain(151_000_000, source="file", file_path=wav,
                        device="HackRF", tx_spec=w._tx_spec("FAKETX"))
            except Exception:
                pass
        finally:
            _osm.sink = real_sink
        assert ("tx build", 5) in log
        # channel tone: saved with the current TX mode's text, given back on recall
        w._add_current()
        ch = w.store.bank().channels[-1]
        assert ch.tone == "CODE 5", ch.tone
        w._apply_tx_tone("CODE 9")
        assert fake.tx_code == 9
        w._store_current_vfo()
        assert w._vfos[w._active_vfo]["tx_tones"] == {"FAKETX": "CODE 9"}
        w.tx_mode_combo.setCurrentText("Analog")
        # the scan topic; modules' audio inputs are off while sweeping
        states = []
        w.bus.subscribe("scan", lambda state: states.append(state))
        w._scan_state("sweep")
        assert w.radio._inputs_muted and states == ["sweep"]
        w._scan_state("idle")
        assert not w.radio._inputs_muted and states == ["sweep", "idle"]
        # the retune hook runs on every channel move
        moved = []
        api.radio.on_retune(w.radio, moved.append)
        w.radio.set_demod(w.radio.band_center_hz + 100_000)
        assert moved == [100_000], moved
        api.radio.off_retune(w.radio, moved.append)
        w.radio.set_demod(w.radio.band_center_hz + 50_000)
        assert moved == [100_000]
        # a label alias for the recorded marks
        A.ModuleUI(w, fake).label_alias("FAKE-V1", "FAKE")
        from sdrexplorer import recindex
        assert recindex.DISPLAY["FAKE-V1"] == "FAKE"
        # the audio arbiter gates analog through the squelch poll
        w.audio.claim("fake")
        w._update_signal()
        assert w.radio._gate_open is False and "DECODER" in w.sig_label.text()
        w.audio.release("fake")
        # state: own section saved, sections of modules that aren't loaded kept
        w._save_state()
        st = json.load(open(A._state_path()))
        assert st["modules"]["fake"] == {"n": 7} and st["modules"]["gone"] == {"keep": True}
    finally:
        w.close()
    assert log[-1] == "shutdown" or "shutdown" in log
    print("ok  test_module_loads_into_app")


def test_source_tap_on_running_flowgraph():
    """A module's source tap is connected on start() and samples reach it (real flowgraph, IQ file)."""
    import time
    from gnuradio import blocks, gr
    from sdrexplorer.radio import ScannerRadio
    p = os.path.join(_TMP, "tap.cs16")
    n = 240_000 * 2
    t = np.arange(n) / 240_000.0
    z = 0.3 * np.exp(2j * np.pi * 10_000 * t)
    iq = np.empty(2 * n, np.int16)
    iq[0::2], iq[1::2] = (z.real * 32767).astype(np.int16), (z.imag * 32767).astype(np.int16)
    iq.tofile(p)
    A.ScannerRadio.start = _REAL_START
    try:
        r = ScannerRadio(audio_out=False)
        probe = blocks.probe_rate(gr.sizeof_gr_complex, 100, 0.5)
        r.taps.attach(api.TAP_SOURCE, probe)
        r.set_replay(p, {"samp_rate": 240000, "center_hz": 463_562_500, "chan_hz": 463_562_500,
                         "mode": "NFM"}, loop=True)
        r.start()
        try:
            time.sleep(1.0)
            assert probe.rate() > 0, probe.rate()
        finally:
            r.stop()
        r.start()                                           # reconnected on the next start too
        try:
            time.sleep(0.6)
            assert probe.rate() > 0
        finally:
            r.stop()
    finally:
        _stubs()
    print("ok  test_source_tap_on_running_flowgraph")


def test_audio_input_reaches_the_speaker():
    """A module's audio (api.AudioInput) is mixed into the speaker chain after the squelch gate, at the
    volume, within ~0.2 s, and silenced at shutdown (real flowgraph, IQ file replay)."""
    import time
    from gnuradio import blocks
    from sdrexplorer.radio import ScannerRadio
    p = os.path.join(_TMP, "silence.cs16")
    np.zeros(240_000 * 2 * 2, np.int16).tofile(p)
    A.ScannerRadio.start = _REAL_START
    try:
        r = ScannerRadio(audio_out=False)
        arb = api.AudioArbiter()
        arb.bind(r)
        ain = arb.open_input("test", rate=8000)
        tap = blocks.vector_sink_f()
        r.connect(r._mix, tap)
        r.set_replay(p, {"samp_rate": 240000, "center_hz": 152_000_000, "chan_hz": 152_000_000,
                         "mode": "NFM"}, loop=True)
        r.set_volume(0.5)
        r.mute()                                      # the analog gate is shut: only the module is heard
        r.start()
        try:
            time.sleep(0.8)
            n0 = len(tap.data())
            t0 = time.time()
            ain.write((0.8 * np.sin(2 * np.pi * 500 * np.arange(8000) / 8000)).astype(np.float32))
            time.sleep(1.4)
            y = np.array(tap.data())[n0:]
            loud = np.flatnonzero(np.abs(y) > 0.1)
            assert len(loud) and abs(np.abs(y).max() - 0.4) < 0.03, np.abs(y).max()   # 0.8 x volume 0.5
            assert loud[0] / 48_000.0 < 0.35, loud[0] / 48_000.0                         # latency
            r.silence_audio()
            time.sleep(0.3)
            n1 = len(tap.data())
            ain.write(np.ones(8000, np.float32))
            time.sleep(0.6)
            assert np.abs(np.array(tap.data())[n1:]).max() < 1e-6
        finally:
            r.stop()
    finally:
        _stubs()
    print("ok  test_audio_input_reaches_the_speaker")


def test_input_port_reuse_and_silent_mode():
    """A closed api.AudioInput's mixer port is reused by the next open_input (same rate: its chain is
    handed over; another rate: a new chain on that port), so ports don't pile up; the reused input is
    heard. A DemodSpec("silent") mode puts zeros on the speaker path, with no squelch (real flowgraph)."""
    import time
    from gnuradio import blocks
    from sdrexplorer.radio import ScannerRadio, mode_from_spec
    p = os.path.join(_TMP, "tone.cs16")
    t = np.arange(240_000 * 2)
    iq = np.exp(2j * np.pi * (1000 * t / 240_000 + 0.5 * np.sin(2 * np.pi * 400 * t / 240_000)))
    np.stack([iq.real, iq.imag], 1).ravel().__mul__(8000).astype(np.int16).tofile(p)
    A.ScannerRadio.start = _REAL_START
    try:
        r = ScannerRadio(audio_out=False)
        arb = api.AudioArbiter()
        arb.bind(r)
        a = arb.open_input("one", rate=8000)
        b = arb.open_input("two", rate=8000)
        assert r._mix_ports == 3
        a.close()
        c = arb.open_input("three", rate=8000)                 # same rate: a's chain, a's port
        assert r._mix_ports == 3 and len(r._ain) == 2 and c.block is not None and c.block._ain is c
        b.close()
        d = arb.open_input("four", rate=16000)                 # another rate: a new chain on b's port
        assert r._mix_ports == 3 and len(r._ain) == 2
        assert sorted(e[4] for e in r._ain) == [1, 2]
        spec = api.DemodSpec(kind="silent", bw=12_500)
        m = mode_from_spec("Quiet", spec)
        assert m.has_squelch is False
        r.register_mode(m)
        tap = blocks.vector_sink_f()
        r.connect(r._mix, tap)
        r.set_replay(p, {"samp_rate": 240000, "center_hz": 152_000_000, "chan_hz": 152_000_000,
                         "mode": "NFM"}, loop=True)
        r.set_volume(0.5)
        r.start()
        try:
            time.sleep(0.6)
            r.set_mode("Quiet")
            time.sleep(0.5)
            n0 = len(tap.data())
            time.sleep(0.6)
            y = np.array(tap.data())[n0:]
            assert len(y) > 10_000 and np.abs(y).max() < 1e-6, (len(y), np.abs(y).max())   # nothing heard
            d.write((0.8 * np.sin(2 * np.pi * 500 * np.arange(16000) / 16000)).astype(np.float32))
            c.write(np.zeros(8000, np.float32))
            time.sleep(1.2)
            y = np.array(tap.data())[n0:]
            assert abs(np.abs(y).max() - 0.4) < 0.03, np.abs(y).max()        # the reused port is heard
        finally:
            r.stop()
    finally:
        _stubs()
    print("ok  test_input_port_reuse_and_silent_mode")


def test_add_remove_module_folder():
    """Settings > Modules: Add module... checks a cloned folder and remembers it in the user's paths.json
    (it's then found, listed with its folder and how it was found, and loads at the next start); bad
    folders are refused with the reason; Remove forgets it without touching the folder."""
    import shutil
    clone = os.path.join(_TMP, "clones", "my-foo-module")             # any name, anywhere
    shutil.rmtree(clone, ignore_errors=True)
    shutil.copytree(REPO, clone)
    with open(os.path.join(clone, "pyproject.toml")) as fh:
        pp = fh.read().replace("sdrexplorer-fake", "sdrexplorer-foo").replace("fake = ", "foo = ") \
                      .replace("old = ", "#old = ").replace("broken = ", "#broken = ") \
                      .replace("sdrexplorer_fake:", "sdrexplorer_foo:")
    with open(os.path.join(clone, "pyproject.toml"), "w") as fh:
        fh.write(pp)
    shutil.move(os.path.join(clone, "src", "sdrexplorer_fake"), os.path.join(clone, "src", "sdrexplorer_foo"))
    with open(os.path.join(clone, "src", "sdrexplorer_foo", "__init__.py")) as fh:
        code = fh.read().replace('name = "fake"', 'name = "foo"').replace('"FAKEMODE"', '"FOOMODE"') \
                        .replace('"FAKEFM"', '"FOOFM"') \
                        .replace('"FAKELABEL"', '"FOOLABEL"').replace('"FAKE"', '"FOOPROOF"')
    with open(os.path.join(clone, "src", "sdrexplorer_foo", "__init__.py"), "w") as fh:
        fh.write(code)
    # the real search (the user's paths.json is in _TMP), but not the real checkouts beside the core
    saved = P._override.pop("module_dirs")
    os.environ["SDREXPLORER_SKIP_BESIDE"] = "1"
    try:
        assert M.check_folder(_TMP)[0] is False and "pyproject.toml" in M.check_folder(_TMP)[1]
        notmod = os.path.join(_TMP, "notmod")
        os.makedirs(notmod, exist_ok=True)
        with open(os.path.join(notmod, "pyproject.toml"), "w") as fh:
            fh.write('[project]\nname = "x"\n')
        ok, msg, _ = M.check_folder(notmod)
        assert not ok and "sdrexplorer.modules" in msg, msg
        w = _build()
        try:
            dlg = A.ModulesDialog(w, w.modules)
            assert dlg.table.rowCount() == 0 and not dlg.empty.isHidden()
            ok, msg = dlg.add_folder(notmod)
            assert not ok and "sdrexplorer.modules" in msg
            ok, msg = dlg.add_folder(clone)
            assert ok, msg
            assert json.load(open(P.user_json()))["module_dirs"] == [os.path.abspath(clone)]
            assert not dlg.add_folder(clone)[0]                       # twice: refused
            rows = {dlg.table.item(r, 1).text(): r for r in range(dlg.table.rowCount())}
            r = rows["Fake module"]
            assert dlg.table.item(r, 3).text() == "found: loads at the next start", dlg.table.item(r, 3).text()
            assert dlg.table.item(r, 4).text() == os.path.abspath(clone)
            assert dlg.table.item(r, 5).text() == P.SRC_ADDED
            assert "loads at the next start" in dlg.restart_note.text()
            dlg.table.selectRow(r)
            assert dlg.remove_btn.isEnabled()
            assert dlg.remove_folder(clone) and os.path.isdir(clone)   # forgotten, not deleted
            assert json.load(open(P.user_json()))["module_dirs"] == []
            assert dlg.table.rowCount() == 0
            dlg.close()
        finally:
            w.close()
        P.add_module_dir(clone)                                       # added again: the next start loads it
        w = _build()
        try:
            assert w.modules.get("foo") is not None
            assert "FOOMODE" in [w.mode_combo.itemText(i) for i in range(w.mode_combo.count())]
        finally:
            w.close()
        P.remove_module_dir(clone)
    finally:
        os.environ.pop("SDREXPLORER_SKIP_BESIDE", None)
        P._override["module_dirs"] = saved
    print("ok  test_add_remove_module_folder")


def test_no_modules_and_switched_off():
    M.set_run_override(set())                               # --no-modules
    try:
        w = _build()
        try:
            assert w.modules.loaded == [] and w.service("fake") is None
            items = [w.mode_combo.itemText(i) for i in range(w.mode_combo.count())]
            assert "FAKEMODE" not in items
            dlg = A.ModulesDialog(w, w.modules)               # lists what was found, nothing runs
            names = [dlg.table.item(r, 1).text() for r in range(dlg.table.rowCount())]
            assert "Fake module" in names
            dlg.close()
        finally:
            w.close()
    finally:
        M.set_run_override(None)
    print("ok  test_no_modules_and_switched_off")


# ------------------------------------------------------------------------------------------- 0.8
def test_channel_ext_roundtrip():
    from sdrexplorer.banks import BankStore, Channel
    path = os.path.join(_TMP, "banks_ext.json")
    with open(path, "w") as fh:
        json.dump({"active": "B", "banks": {"B": [
            {"freq": 155_000_000, "mode": "NFM", "ext": {"mod": {"tg": 5}}, "future_key": [1, 2]},
            {"freq": 156_000_000, "mode": "AM"}]}}, fh)
    st = BankStore(path)
    a, b = st.bank("B").channels[:2]
    assert a.fields_for("mod") == {"tg": 5} and a.fields_for("other") == {} and b.ext == {}
    a.ext["mod2"] = {"x": "y"}
    st.save()
    raw = json.load(open(path))["banks"]["B"]
    assert raw[0]["ext"] == {"mod": {"tg": 5}, "mod2": {"x": "y"}} and raw[0]["future_key"] == [1, 2]
    assert "ext" not in raw[1] and "extra" not in raw[0]       # nothing written for a plain channel
    assert BankStore(path).bank("B").channels[0].fields_for("mod2") == {"x": "y"}
    # an importer's meta "ext" lands on the channel
    from sdrexplorer.api import FoundChannel
    ch = FoundChannel(freq=1, meta={"ext": {"m": {"k": 1}}}).to_channel()
    assert isinstance(ch, Channel) and ch.fields_for("m") == {"k": 1}
    print("ok  test_channel_ext_roundtrip")


def test_channel_fields_hooks():
    w = _build()
    try:
        mod = next(m for m in w.modules.loaded if m.name == "fake")
        log = _fake_log()
        mod.tx_code = 42
        w.mode_combo.setCurrentText("FAKEFM")
        w._add_current()                                  # saving: Module.channel_fields -> ext
        ch = w.store.bank().channels[-1]
        assert ch.mode == "FAKEFM" and ch.fields_for("fake") == {"code": 42}, ch
        assert w._module_channel_summary(ch) == [("Fake module", "code 42")]
        mod.tx_code = 1
        del log[:]
        w._refresh_table()
        row = w._row_bankidx.index(len(w.store.bank().channels) - 1)
        w._tune_row(row)                                  # recall: Module.channel_recalled
        assert ("recalled", ch.freq, {"code": 42}) in log and mod.tx_code == 42
        mod.ui.set_channel_fields({"code": 7})
        assert w.store.bank().channels[-1].fields_for("fake") == {"code": 7}
        assert mod.ui.channel_fields() == {"code": 7}
        w.tune(w.radio.freq_hz + 10_000)                  # a manual tune forgets the recalled channel
        assert mod.ui.channel_fields() == {} and not mod.ui.set_channel_fields({"code": 1})
        # a label handed to request_decode by a recalled channel carries the channel's frequency
        from sdrexplorer.banks import Channel
        w.store.add_channel(Channel(freq=162_400_000, mode="FAKELABEL"))
        w._refresh_table()
        del log[:]
        w._tune_row(w._row_bankidx.index(len(w.store.bank().channels) - 1))
        assert ("decode", 162_400_000, "FAKELABEL") in log, log
    finally:
        w.close()
    print("ok  test_channel_fields_hooks")


def test_importer_registry():
    from sdrexplorer.api import importers
    w = _build()
    try:
        names = [w.source_combo.itemText(i) for i in range(w.source_combo.count())]
        assert names[:3] == ["CSV file", "FCC ULS", "RadioReference"] and "Fake list" in names, names
        assert importers.get("Fake list").owner == "fake"
        w.source_combo.setCurrentText("FCC ULS")
        # (isHidden: the Memories page itself isn't the shown page)
        assert not w.dataset_combo.isHidden() and w.csv_btn.isHidden()
        w.source_combo.setCurrentText("CSV file")
        assert w.dataset_combo.isHidden() and not w.csv_btn.isHidden()
        w.source_combo.setCurrentText("Fake list")
        before = set(w.store.banks)
        w._import_source()
        w.fetch_worker.wait(10000)
        QtWidgets.QApplication.processEvents()
        new = set(w.store.banks) - before
        assert len(new) == 1, (new, w.fetch_status.text())
        ch = w.store.bank(new.pop()).channels[0]
        assert ch.freq == 146_520_000 and ch.fields_for("fake") == {"code": 9}
        # RadioReference: account in the keystore, an old config.json one moved there once
        from sdrexplorer.providers import radioreference as rr
        from sdrexplorer.api import secrets
        with open(rr._config_path(), "w") as fh:
            json.dump({"radioreference": {"username": "u", "password": "p"}, "other": 1}, fh)
        assert rr.available() and rr.creds()["username"] == "u"
        cfg = json.load(open(rr._config_path()))
        assert "password" not in json.dumps(cfg["radioreference"]) and cfg["other"] == 1
        rr.set_creds("", "")
        assert not rr.available() and secrets.get("core", "radioreference") is None
    finally:
        w.close()
    print("ok  test_importer_registry")


def test_worker_roundtrip():
    from sdrexplorer.api import worker
    pkg = os.path.join(_TMP, "wk_pkg")
    os.makedirs(pkg, exist_ok=True)
    with open(os.path.join(pkg, "echo_worker.py"), "w") as fh:
        fh.write(textwrap.dedent('''
            import importlib.util, sys
            import sdrexplorer_worker as W
            class Echo:
                def info(self):
                    return {"core_importable": importlib.util.find_spec("sdrexplorer") is not None}
                def handle(self, h, p):
                    if h.get("op") == "boom":
                        raise ValueError("boom")
                    return {"n": len(p)}, bytes(p)[::-1]
            def load(args):
                if args[:1] == ["fail"]:
                    raise RuntimeError("no device")
                return Echo()
            if __name__ == "__main__":
                sys.exit(W.main(load))
            '''))
    env = worker.clean_env(pkg)
    w = worker.Worker(worker.python_command(sys.executable, "echo_worker"), env=env, cwd=pkg, name="echo")
    try:
        assert w.info == {"core_importable": False}, w.info   # clean_env keeps the app's paths out
        assert w.call({"op": "x"}, b"abc") == ({"n": 3, "nbytes": 3}, b"cba")
        try:
            w.call({"op": "boom"})
            raise AssertionError("no error")
        except worker.WorkerError as e:
            assert "boom" in str(e)
        assert w.call({"op": "y"}, np.arange(4, dtype=np.float32).tobytes())[0]["n"] == 16
        assert w.call({"op": "y"}, np.zeros((3, 2), np.float32))[0]["n"] == 24       # an array as it is
    finally:
        w.close()
    assert not w.alive()
    try:
        worker.Worker(worker.python_command(sys.executable, "echo_worker", "fail"), env=env, cwd=pkg)
        raise AssertionError("started")
    except worker.WorkerError as e:
        assert "no device" in str(e)
    # parent death: a worker started by a process that then dies exits by itself
    import subprocess
    code = (f"import sys; sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))!r});"
            "from sdrexplorer.api import worker; "
            f"w = worker.Worker(worker.python_command(sys.executable, 'echo_worker'), env=worker.clean_env({pkg!r}), "
            f"cwd={pkg!r}); print(w.proc.pid, flush=True); import os; os._exit(0)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60).stdout.split()
    pid = int(out[-1])
    t0 = time.time()
    while time.time() - t0 < 6:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.2)
    else:
        os.kill(pid, 9)
        raise AssertionError("orphaned worker kept running")
    print("ok  test_worker_roundtrip")


def test_spectrum_frames_replay_events_station():
    from sdrexplorer.api import spectrum, station
    w = _build()
    try:
        got, seen = [], []
        spectrum.subscribe(got.append)
        w.radio.running = True
        w._deliver_spectrum()
        spectrum.unsubscribe(got.append)
        w._deliver_spectrum()
        assert len(got) == 1, got
        f = got[0]
        assert f.center_hz == int(w.radio.band_center_hz) and f.db.dtype == np.float32
        assert abs(f.bins_hz[f.bin_of(f.center_hz + 100_000)] - (f.center_hz + 100_000)) <= f.samp_rate / f.nfft
        # band / rate / replay topics
        for t in ("band_changed", "rate_changed", "replay_seek", "replay_wrap", "replay_loop", "station_changed",
                  "recording_started"):
            w.bus.subscribe(t, lambda _t=t, **kw: seen.append((_t, kw)))
        w._poll_core_topics()
        w.radio.band_center_hz += 1_000_000
        w._poll_core_topics()
        assert any(t == "band_changed" for t, _ in seen) and not any(t == "rate_changed" for t, _ in seen)
        # station: location round trip + topic; time base of a replayed file
        station.set_location(42.5, -71.0, 30, "Somewhere")
        loc = station.location()
        assert (loc.lat, loc.lon, loc.alt_m, loc.name) == (42.5, -71.0, 30.0, "Somewhere")
        assert seen[-1][0] == "station_changed" and seen[-1][1]["location"] == loc
        station.set_location(None, None)
        assert station.location() is None
        assert station.file_start_time({}, "iq_band_460.0000MHz_20260101_120000.cs16") is not None
        assert station.file_start_time({"start_time": 12.5}) == 12.5
        assert abs(station.now() - time.time()) < 2
        w.radio.started_at = 1000.0
        w.radio.samp_rate = 2_000_000
        assert station.sample_time(4_000_000) == 1002.0
    finally:
        w.radio.running = False
        w.close()
    print("ok  test_spectrum_frames_replay_events_station")


def test_store_keep_out():
    root = os.path.join(_TMP, "store_ko")
    for rec in ("a.cs16", "b.cs16", "c.cs16"):
        store.add("vu", "NBFM", np.ones(8192, np.complex64), 24000, 155e6, rec, root=root)
    bad = store.add("vu", "NBFM", np.ones(8192, np.complex64), 24000, 155e6, "a.cs16", root=root)
    store.set_held_out("vu", ["b.cs16"], root=root)
    store.set_keep_out("vu", [{"recording": "c.cs16", "why": "leak"}, {"id": os.path.basename(bad)}], root=root)
    assert store.held_out("vu", root) == ["b.cs16"]                      # keep_out doesn't disturb the list
    assert {e.get("recording") or e.get("id") for e in store.keep_out("vu", root)} == {"c.cs16", os.path.basename(bad)}
    _, _, tr = store.to_windows("vu", 4096, ["NBFM"], held=False, root=root)
    _, _, ho = store.to_windows("vu", 4096, ["NBFM"], held=True, root=root)
    assert set(tr) == {"a.cs16"} and len(tr) == 2 and set(ho) == {"b.cs16"}, (tr, ho)
    print("ok  test_store_keep_out")


def test_payloads_follow_log_filter():
    """Decode.ref ties a burst's log lines to its payload: with "Follow log filter" the payload pane shows
    the payloads of the lines the log filter matches (and payloads whose own text matches); without it the
    payload filter box applies on its own. A record with both a line and a payload is linked without a ref."""
    w = _build()
    try:
        w.dig_hex_cb.setChecked(True)
        burst = ("test", 1)
        w.publish(Decode(source="t", protocol="X", text="LOCATION 1.5, 2.5", ref=burst))       # line only
        w.publish(Decode(source="t", protocol="X", text="burst bytes", payload=b"\x01\x02POS",
                         log=False, ref=burst))                                                # its payload
        w.publish(Decode(source="t", protocol="X", text="other burst", payload=b"\xffZZZZ", log=False))
        w.publish(Decode(source="t", protocol="Y", text="UNIT 77 data", payload=b"\x77\x77"))   # both, no ref
        pane = lambda: w.dig_hex.toPlainText()
        w.dig_search.setText("location")
        assert "LOCATION" in w.dig_log.toPlainText() and "UNIT" not in w.dig_log.toPlainText()
        assert "POS" in pane() and "ZZZZ" in pane()              # not following: the payload box (empty)
        w.hex_follow_cb.setChecked(True)
        assert not w.hex_search.isEnabled()
        assert "POS" in pane() and "ZZZZ" not in pane() and "UNIT" not in pane()   # the matching burst only
        w.dig_search.setText("unit 77")
        assert "UNIT 77" in pane() and "POS" not in pane()        # a record's own line and payload
        w.dig_search.setText("other burst")
        assert "ZZZZ" in pane()                                   # a payload's own text matches
        w.dig_search.setText("location")
        late = ("test", 2)                                        # payload first, its line later
        w.publish(Decode(source="t", protocol="X", text="late bytes", payload=b"LATE", log=False, ref=late))
        assert "LATE" not in pane()
        w.publish(Decode(source="t", protocol="X", text="LOCATION 3.5, 4.5", ref=late))
        assert "LATE" in pane()
        w.dig_search.setText("")
        assert "ZZZZ" in pane() and "UNIT" in pane()              # no filter: every payload
        w.hex_follow_cb.setChecked(False)
        w.hex_search.setText("zzzz")
        assert "ZZZZ" in pane() and "POS" not in pane()           # the payload box on its own again
        w._save_state()
        assert json.load(open(A._state_path()))["payload_follow"] is False
    finally:
        w.close()
    print("ok  test_payloads_follow_log_filter")


def main():
    _stubs()
    tests = [test_api_version_ranges, test_secrets, test_gains_per_device_and_live_apply,
             test_discovery_from_checkout, test_switch_off_and_run_override,
             test_audio_arbiter, test_event_bus, test_ext_registries, test_audio_input_and_paths_register,
             test_taps_on_fake_radio, test_tuner_lease, test_store_roundtrip,
             test_module_loads_into_app, test_source_tap_on_running_flowgraph,
             test_audio_input_reaches_the_speaker, test_input_port_reuse_and_silent_mode,
             test_add_remove_module_folder,
             test_no_modules_and_switched_off,
             test_channel_ext_roundtrip, test_channel_fields_hooks, test_importer_registry, test_worker_roundtrip,
             test_spectrum_frames_replay_events_station, test_store_keep_out,
             test_payloads_follow_log_filter]
    for t in tests:
        t()
    print(f"\nall {len(tests)} module tests passed", flush=True)
    # Skip the interpreter's teardown, as the other suites do: PyQt5 destroys the QApplication during
    # Py_FinalizeEx after objects it still points at are gone, and segfaults about every other run
    # (gdb: Py_FinalizeEx -> QtCore.abi3.so -> sip -> a freed address). Everything is done by now; an
    # assertion error above still exits non-zero through the normal path.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
