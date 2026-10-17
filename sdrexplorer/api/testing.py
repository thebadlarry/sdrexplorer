# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Fakes for testing a module without a radio, a display or the other modules.

    radio = FakeRadio()
    ui = FakeUI()
    m = MyModule(); m.attach_radio(radio, Taps(radio)); m.attach_ui(None, ui)
    ui.bus.emit("tuned", freq_hz=462_562_500, primary=True)
"""
from __future__ import annotations

from .audio import AudioArbiter
from .events import EventBus


class FakeRadio:
    """Enough of ScannerRadio for Taps and simple modules: records connections, tunes, never runs."""
    QUAD_RATE = 240_000

    def __init__(self, samp_rate=2_400_000, center_hz=462_000_000):
        self.samp_rate = samp_rate
        self.band_center_hz = center_hz
        self.demod_hz = center_hz + 300_000
        self.running = False
        self.chan = object()
        self.src = None
        self.edges = []

    def connect(self, a, b):
        self.edges.append((a, b))

    def disconnect(self, a, b):
        self.edges.remove((a, b))

    def lock(self):
        pass

    def unlock(self):
        pass


class FakeUI:
    """The registry a module gets in attach_ui, recording what it adds."""

    def __init__(self):
        self.bus = EventBus()
        self.audio = AudioArbiter()
        self.menu_entries, self.overlays, self.modes = [], [], []
        self._services = {}
        self.log = []

    def add_menu_entry(self, e):
        self.menu_entries.append(e)
        return e

    def add_overlay(self, o):
        self.overlays.append(o)

    def add_mode(self, m):
        self.modes.append(m)

    def service(self, name):
        return self._services.get(name)

    def provide(self, name, obj):
        self._services[name] = obj

    def status(self, text):
        self.log.append(text)


class FakeSourceDriver:
    """A source driver with made-up devices, for tests that mustn't depend on what's plugged in.
    ``devices``: [(key, label, Caps)]. open() gives a throttled noise source at the asked rate."""

    def __init__(self, name="fake", devices=()):
        self.name, self.title = name, "fake devices (tests)"
        self._devs = list(devices)
        self.opened = []

    def probe(self):
        return True, ""

    def enumerate(self):
        from .sources import DeviceInfo
        return [DeviceInfo(driver=self.name, key=k, label=lab, args=k, family=k.split(":")[0],
                           serial=k.split(":")[-1]) for k, lab, _c in self._devs]

    def caps(self, dev, handle=None):
        return next(c for k, _l, c in self._devs if k == dev.key)

    def open(self, dev, settings):
        from gnuradio import analog, blocks, gr
        from .sources import SourceHandle

        class _H(SourceHandle):
            def __init__(h):
                rate = float(settings.get("rate") or 2.4e6)
                tb = gr.hier_block2("fake_src", gr.io_signature(0, 0, 0),
                                    gr.io_signature(1, 1, gr.sizeof_gr_complex))
                n = analog.noise_source_c(analog.GR_GAUSSIAN, 0.01, 0)
                t = blocks.throttle(gr.sizeof_gr_complex, rate)
                tb.connect(n, t, tb)
                h.block, h.center, h.gains, h.ppm = tb, settings.get("center_hz"), {}, 0
                h._keep = (n, t)

            def set_center(h, hz):
                h.center = hz

            def set_rate(h, rate):
                pass

            def set_gain(h, stage, db):
                h.gains[stage] = db

            def set_freq_corr(h, ppm):
                h.ppm = ppm
        h = _H()
        # apply gains through the handle, filtered to this device's real stages -- mirrors the real
        # OsmosdrDriver.open (an unknown stage name must not reach the device)
        valid = {g.name for g in self.caps(dev).gains}
        for stage, db in (settings.get("gains") or {}).items():
            if stage in valid:
                h.set_gain(stage, db)
        self.opened.append((dev.key, settings, h))
        return h

    def sink_args(self, dev):
        c = self.caps(dev)
        return dev.args if c.tx else None


def use_fake_sources(devices=None):
    """Replace every source driver with one FakeSourceDriver (tests). Default devices: a transmit-capable
    3-stage "fake HackRF" and a receive-only 1-stage "fake RTL"."""
    from . import sources as api_sources
    from .sources import Caps, GainStage
    from .. import sources as core_sources
    if devices is None:
        devices = [("hackrf:fake1", "Fake HackRF",
                    Caps(freq_range=(1e6, 6e9), rate_ranges=[(1e6, 20e6)],
                         gains=[GainStage("RF", 0, 14, 14), GainStage("IF", 0, 40, 8), GainStage("BB", 0, 62, 2)],
                         tx=True, ppm=True)),
                   ("rtlsdr:fake2", "Fake RTL", Caps(freq_range=(24e6, 1.7e9), rate_ranges=[(1e6, 3.2e6)],
                                                     gains=[GainStage("TUNER", 0, 49, 1)], tx=False,
                                                     tx_why="receive-only device", ppm=True))]
    drv = FakeSourceDriver(devices=devices)
    api_sources._drivers.clear()
    api_sources.register(drv)
    core_sources.reset_cache()
    return drv
