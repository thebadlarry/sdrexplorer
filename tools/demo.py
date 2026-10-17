#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""sdrexplorer demo: a synthetic band and made-up decodes, to see the app work without an SDR, and to make
the screenshots in docs/images.

    python3 tools/demo.py                  run the demo in a window
    python3 tools/demo.py --shots DIR      run it, save screenshots to DIR, and quit

Everything shown is synthetic. Nothing is received or transmitted:
  - the band is generated: an NFM voice channel, a P25-like control channel, DMR-like TDMA bursts, an
    NXDN-like channel and pager bursts, at made-up frequencies around 461 MHz;
  - the decodes, payloads and positions are made up (units driving loops around Boston), and are fed
    through the modules' own handlers, so they look as they do on air;
  - the waterfall labels are set as the classifier would set them; Memories holds the Boston FCC sample.

Settings, memories and recordings go to a temporary folder: your own setup isn't read or changed. The
digital and paging modules are used when they are installed beside the core; the classifier's labels are
drawn either way. Needs a display (on a headless machine: xvfb-run).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile

import numpy as np
from scipy import signal as sps

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

FS = 2_400_000                    # the band's sample rate (the app's default capture rate)
CENTER = 461_000_000
SECONDS = 6.0                     # the generated band loops
BASE = 48_000                     # each signal is made at this rate, then moved into the band
HOME = (42.3601, -71.0589)        # the made-up routes go around Boston (public, Boston-level)

# offset (Hz) from CENTER, what it looks like, the label drawn on the waterfall, its width
SIGNALS = [(+300_000, "voice", "NFM", 11_000), (-400_000, "p25", "P25", 9_000),
           (+620_000, "dmr", "DMR", 9_000), (-750_000, "nxdn", "NXDN48", 5_000),
           (+900_000, "pager", "POCSAG", 12_000), (-150_000, "voice2", "NFM", 11_000)]


# ------------------------------------------------------------------------------------------------ the band
def _fm(freq_hz, rng):
    """Constant-envelope FM from an instantaneous-frequency series at BASE."""
    return np.exp(2j * np.pi * np.cumsum(freq_hz) / BASE).astype(np.complex64)


def _keyed(n, rng, on=(0.8, 2.5), off=(0.3, 1.5)):
    """A transmitter keying on and off (1 / 0 per sample at BASE)."""
    m, i, state = np.zeros(n, np.float32), 0, rng.random() < 0.5
    while i < n:
        k = int(rng.uniform(*(on if state else off)) * BASE)
        m[i:i + k] = 1.0 if state else 0.0
        i, state = i + k, not state
    return m


def _fsk(n, rng, baud, devs, smooth=1.0):
    """M-level FSK frequency trajectory: random symbols, raised-cosine-ish smoothing."""
    sps_ = BASE / baud
    sym = rng.choice(devs, int(n / sps_) + 2)
    f = np.repeat(sym, int(round(sps_)))[:n].astype(np.float64)
    k = max(3, int(sps_ * smooth)) | 1
    return np.convolve(f, np.hanning(k) / np.hanning(k).sum(), mode="same")


def _voice(n, rng):
    """NFM speech-like audio: syllables of a few harmonics, a CTCSS tone, keyed."""
    t = np.arange(n) / BASE
    f0 = 140 + 30 * np.sin(2 * np.pi * 0.7 * t)
    audio = sum(np.sin(2 * np.pi * h * np.cumsum(f0) / BASE) / h for h in (1, 2, 3, 5, 7))
    syll = (0.5 + 0.5 * np.sin(2 * np.pi * 4.0 * t + rng.uniform(0, 6))) ** 2
    dev = 2500 * audio * syll / 2.0 + 300 * np.sin(2 * np.pi * 100.0 * t)
    return _fm(dev, rng) * _keyed(n, rng)


def make_band(path, seed=7):
    """Write the synthetic band as cs16 + its sidecar (what a band recording looks like)."""
    rng = np.random.default_rng(seed)
    n_base = int(SECONDS * BASE)
    n = int(SECONDS * FS)
    x = (rng.standard_normal(n) + 1j * rng.standard_normal(n)).astype(np.complex64) * 0.01
    t_up = np.arange(n) / FS
    for off, kind, _label, _bw in SIGNALS:
        if kind in ("voice", "voice2"):
            z, amp = _voice(n_base, rng), (0.30 if kind == "voice" else 0.12)
        elif kind == "p25":                                          # C4FM-like, continuous
            z, amp = _fm(_fsk(n_base, rng, 4800, (-1800, -600, 600, 1800)), rng), 0.25
        elif kind == "dmr":                                          # 4FSK, 30 ms TDMA bursts
            m = ((np.arange(n_base) // int(0.030 * BASE)) % 2).astype(np.float32)
            z, amp = _fm(_fsk(n_base, rng, 4800, (-1944, -648, 648, 1944)), rng) * m, 0.22
        elif kind == "nxdn":                                         # narrow 4FSK, keyed
            z = _fm(_fsk(n_base, rng, 2400, (-1050, -350, 350, 1050)), rng) * _keyed(n_base, rng, (1, 3), (1, 2))
            amp = 0.18
        else:                                                        # pager: 2FSK bursts
            m = _keyed(n_base, rng, (0.4, 0.9), (1.0, 2.0))
            z, amp = _fm(_fsk(n_base, rng, 1200, (-4500, 4500), smooth=0.4), rng) * m, 0.20
        up = sps.resample_poly(z, FS // BASE, 1)[:n].astype(np.complex64)
        x += amp * up * np.exp(2j * np.pi * off * t_up).astype(np.complex64)
    x += 0.05 * np.exp(2j * np.pi * -980_000 * t_up).astype(np.complex64)      # a carrier
    iq = np.empty(2 * n, np.int16)
    scale = 0.5 * 32767 / max(np.abs(x.real).max(), np.abs(x.imag).max())
    iq[0::2], iq[1::2] = (x.real * scale).astype(np.int16), (x.imag * scale).astype(np.int16)
    iq.tofile(path)
    json.dump({"center_hz": CENTER, "chan_hz": CENTER + SIGNALS[0][0], "samp_rate": FS, "format": "cs16",
               "iq_size": 2, "iq_signed": True, "mode": "NFM", "scope": "band",
               "note": "sdrexplorer demo: synthetic"}, open(path + ".json", "w"), indent=1)


# ------------------------------------------------------------------------------------------------ encoders
def moto_gps_lcw(lat, lon):
    """A P25 Motorola embedded-GPS link control word (MFID 0x90, opcode 6) for a position."""
    v = 0x06 << 64 | 0x90 << 56
    v |= (1 if lat < 0 else 0) << 47 | round((lat + 90 if lat < 0 else lat) * 0x7FFFFF / 90) << 24
    v |= (1 if lon < 0 else 0) << 23 | round((lon + 180 if lon < 0 else lon) * 0x7FFFFF / 180)
    return v.to_bytes(9, "big")


def voice_user_lcw(tg, src):
    return bytes([0x00, 0x00, 0x00, 0x00]) + tg.to_bytes(2, "big") + src.to_bytes(3, "big")


def udp(dport, payload, src=(12, 0, 0, 1), dst=(13, 0, 0, 2)):
    total = 28 + len(payload)
    ip = bytes([0x45, 0, total >> 8, total & 0xff, 0, 0, 0, 0, 0x40, 0x11, 0, 0, *src, *dst])
    return ip + bytes([0xc3, 0x50, dport >> 8, dport & 0xff, 0, 8 + len(payload), 0, 0]) + payload


def lrrp_point(lat, lon, speed=None, heading=None):
    """An LRRP report body: a point token, the 8-byte coordinate, optional speed / heading."""
    la = round(abs(lat) * 4294967295.0 / 180.0) | (0x80000000 if lat < 0 else 0)
    lo = round(lon * 4294967295.0 / 360.0) & 0xFFFFFFFF
    body = bytes([0x0d, 0x1e, 0x22, 0x04, 0x24, 0x68, 0xac, 0xe0, 0x51]) + la.to_bytes(4, "big") + lo.to_bytes(4, "big")
    if speed is not None:
        body += bytes([0x6c]) + int(speed * 10).to_bytes(2, "big")
    if heading is not None:
        body += bytes([0x56, int(heading / 2) & 0xff])
    return body


def route(t, phase, r=0.025, period=90.0):
    """A position on a loop around Boston (degrees), and the heading along it."""
    a = 2 * math.pi * (t / period + phase)
    lat = HOME[0] + r * math.sin(a)
    lon = HOME[1] + r * 1.35 * math.cos(a)
    heading = (math.degrees(math.atan2(math.cos(a), -1.35 * math.sin(a))) + 360) % 360
    return round(lat, 6), round(lon, 6), round(heading, 1)


# ------------------------------------------------------------------------------------------------ the feed
class Feed:
    """Made-up decodes through the modules' own handlers, a few per tick."""

    def __init__(self, w):
        self.w, self.t, self.n = w, 0.0, 0
        self.dig = w.modules.get("digital")
        self.pag = w.modules.get("paging")
        if self.dig is not None:
            for i in (3100101, 3100102):          # made-up DMR ids: no radioid.net lookup
                self.dig._dmr_id_cache[str(i)] = {"callsign": f"DEMO{i % 100}"}
            self.dig._set_capture(True)

    def tick(self, dt=1.5):
        self.t += dt
        self.n += 1
        if self.dig is not None:
            [self.p25, self.dmr, self.nxdn][self.n % 3]()
        if self.pag is not None and self.n % 4 == 0:
            self.pag._on_pocsag_page({"ric": 1234567 + self.n % 3, "function": 3, "baud": 1200,
                                      "alpha": ["DEMO: test page", "DEMO: unit 12 call dispatch",
                                                "DEMO: weekly test"][self.n % 3]})
        for i, (cs, phase) in enumerate((("DEMO123", 0.0), ("DEMO456", 0.4), ("DEMO789", 0.7))):
            lat, lon, hdg = route(self.t, phase, r=0.08 + 0.02 * i, period=60.0)
            self.w.targets.upsert(f"demo-ac{i}", "aircraft", lat=lat, lon=lon, label=cs, heading=hdg,
                                  altitude_ft=3000 + 1500 * i, category="jet")

    def p25(self):
        from sdrexplorer_digital.monitor import parse_p25_lcw
        m, nac = self.dig, 0x293
        src, tg = (1001, 100) if self.n % 2 else (1002, 200)
        lat, lon, _h = route(self.t, 0.0 if src == 1001 else 0.5)

        def lcw(word):
            lc = parse_p25_lcw(word)
            d = {"proto": "P25", "nac": nac, "duid": "LCW", "lcw": word, "lcw_kind": lc["kind"], "raw": b"\x02\x93" + word}
            d.update({"lcw_" + k: lc[k] for k in ("gps", "src", "tgid") if k in lc})
            return d
        m._on_decoded({"proto": "P25", "nac": nac, "duid": "HDU", "raw": b"\x02\x93"})
        m._on_decoded({"proto": "P25", "tgid": tg, "src": src, "encrypted": 0, "raw": b"{}"})
        m._on_decoded(lcw(voice_user_lcw(tg, src)))
        m._on_decoded(lcw(moto_gps_lcw(lat, lon)))
        m._on_decoded({"proto": "P25", "nac": nac, "duid": "TDU", "raw": b"\x02\x93"})

    def dmr(self):
        m = self.dig
        src = 3100101 if self.n % 2 else 3100102
        m._on_decoded({"proto": "DMR", "label": "CACH_SLC", "cc": 1, "raw": b"\x00\x00\x00\x00\x01"})
        m._on_decoded({"proto": "DMR", "label": "SLOT_VLC", "flco": 0, "call": "Group Voice", "tgid": 9,
                       "src": src, "fid": 0, "cc": 1, "raw": b"\x00" * 10})
        lat, lon, hdg = route(self.t, 0.25 if src == 3100101 else 0.75, r=0.035)
        ip = (12, src >> 16 & 0xff, src >> 8 & 0xff, src & 0xff)       # a MOTOTRBO radio's address holds its id
        m._on_data("DMR", "DATA", udp(4001, lrrp_point(lat, lon, speed=11.0, heading=hdg), src=ip))
        if self.n % 6 == 2:
            m._on_data("DMR", "DATA", udp(4005, bytes.fromhex("000df02008") + str(src).encode()[:8] + b"\x00\x00"))

    def nxdn(self):
        m = self.dig
        src = 2001
        lat, lon, hdg = route(self.t, 0.6, r=0.045)
        m._on_dsd_group(("NXDN", f"demo-{self.n}"))
        m._on_dsd_line("00:00:00 Sync: NXDN48  RDCH Data   F2U-Ve: 2.5;  RAN 05  UDCH DCALL_HEADER")
        m._on_dsd_line(f" Data Call Header (029400) Source: {src}; Target: 250; Group Call 4800bps Half Duplex")
        m._on_dsd_data("NXDN", "UDCH", bytes([0x0d, 0x09, 0x00, 0x22, 0x07, 0xd1, 0x00, 0xfa, 0x00, 0x02, 0x94]) + bytes(15))
        m._on_dsd_line(f" GPS: ({lat:.6f}°, {lon:.6f}°) Speed: 10.5 k/h; COG: {hdg:.1f}; Elevation: 15;")
        m._on_nxdn_position({"src": src, "lat": lat, "lon": lon, "speed_mps": 2.9, "heading_deg": round(hdg)})


# ------------------------------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shots", help="save screenshots to this folder, then quit")
    ap.add_argument("--warmup", type=float, default=25.0, help="seconds to run before the screenshots")
    ap.add_argument("--size", default="1400x900", help="main window size")
    a = ap.parse_args()

    from PyQt5 import QtCore, QtWidgets
    from sdrexplorer import paths as P
    tmp = tempfile.mkdtemp(prefix="sdrexplorer-demo-")
    P.override(tmp)                                  # every setting and data file in a temp folder
    band = os.path.join(tmp, "demo_band.cs16")
    print("generating the synthetic band…", flush=True)
    make_band(band)

    from sdrexplorer import sources
    sources.scan = lambda *x, **k: None              # no SDR is looked for or opened: the band is a file
    sources.devices = lambda *x, **k: []
    sources.find = lambda *x, **k: None
    from sdrexplorer import app as A
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.AA_ShareOpenGLContexts)
    qa = QtWidgets.QApplication(sys.argv[:1])
    qa.setStyle("Fusion")
    qa.setApplicationName("sdrexplorer")
    w = A.ScannerApp()
    ww, wh = (int(v) for v in a.size.split("x"))
    w.resize(ww, wh)
    w.show()

    # the band, replayed in a loop, tuned to the voice channel
    QtWidgets.QFileDialog.getOpenFileName = staticmethod(lambda *x, **k: (band, ""))
    w.loop_cb.setChecked(True)
    w._open_iq_file()
    w.radio.set_volume(0.0)
    w._set_label_source("annotations")              # the classifier's labels, not the memories' names
    clf = w.modules.get("classifier")
    if clf is not None:                             # its stats box would say "no models": not running here
        clf.stats_on = False
        clf._refresh_stats()

    # Memories: the Boston FCC sample
    from sdrexplorer.api import Annotation
    from sdrexplorer.providers import csv_import
    rows = csv_import.load_csv(os.path.join(REPO, "sdrexplorer", "samples", "boston_fcc_sample.csv"))
    rows = rows[0] if isinstance(rows, tuple) else rows
    name = "Boston (FCC sample)"
    w.store.create_bank(name)
    for r in rows:
        w.store.add_channel(r.to_channel(), name)
    w.store.set_active(name)
    w._refresh_bank_combo()
    w._refresh_table()

    # the waterfall labels, as the classifier draws them
    def labels():
        w.annotate("classifier", [Annotation(freq_hz=CENTER + off, bw_hz=bw, label=lab, raw=lab,
                                             confidence=0.95, confirmed=lab != "NFM", snr_db=24.0)
                                  for off, _k, lab, bw in SIGNALS])

    feed = Feed(w)
    for i in range(4):
        feed.tick()
    timer = QtCore.QTimer()
    timer.timeout.connect(lambda: (feed.tick(), labels()))
    timer.start(1500)
    labels()
    w.dig_hex_cb.setChecked(True)

    if not a.shots:
        print("demo running: close the window to quit", flush=True)
        rc = qa.exec_()
        os._exit(rc)

    os.makedirs(a.shots, exist_ok=True)

    def wait(sec):
        loop = QtCore.QEventLoop()
        QtCore.QTimer.singleShot(int(sec * 1000), loop.quit)
        loop.exec_()

    def shot(widget, name, screen=False):
        w.statusBar().clearMessage()
        QtWidgets.QApplication.processEvents()
        pix = (QtWidgets.QApplication.primaryScreen().grabWindow(int(widget.winId())) if screen
               else widget.grab())
        path = os.path.join(a.shots, name)
        pix.save(path)
        print(f"saved {path} ({pix.width()}x{pix.height()})", flush=True)

    print(f"warming up {a.warmup:g} s…", flush=True)
    wait(a.warmup)
    shot(w, "main-window.png")
    w.dig_search.setText("GPS")
    w.hex_follow_cb.setChecked(True)
    wait(0.5)
    shot(w, "decode-follow-filter.png")
    w.dig_search.setText("")
    w._show_memories()
    wait(1.0)
    shot(w, "memories.png")
    w._show_radio()
    from sdrexplorer.moduleui import ModulesDialog
    dlg = ModulesDialog(w, w.modules)
    dlg.resize(1100, 520)
    dlg.show()
    wait(1.0)
    shot(dlg, "modules.png")
    dlg.close()
    if not A.WEBENGINE_AVAILABLE:
        print("no live map: PyQtWebEngine is not installed", flush=True)
    else:
        print("opening the live map…", flush=True)
        w._ensure_map_window()
        w._map_win.resize(1200, 800)                 # before it shows: the page lays out at this size
        w._show_map_window()
        if w._map_win is not None:
            wait(15.0)                               # the map page, its tiles and a few position updates
            shot(w._map_win, "live-map.png", screen=True)
    w.close()
    os._exit(0)


if __name__ == "__main__":
    main()
