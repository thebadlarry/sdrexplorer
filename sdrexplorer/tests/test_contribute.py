#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The contribution tool: a signal cut out of a recording keeps only what is needed to train on.

    python3 -m sdrexplorer.tests.test_contribute

A synthetic sub-band recording (cs16 + sidecar) with an FM signal keyed for part of it and a second signal
elsewhere in the band: the crops hold only the keyed signal, at 0 Hz, and no frequency, time, file name or
id; ingest adds them to a store and refuses a tampered package. numpy + scipy only.
"""
import io
import json
import os
import sys
import tempfile
import zipfile

import numpy as np

from sdrexplorer import contribute as C
from sdrexplorer.api import store

FS = 480_000                     # a sub-band recording's rate
CENTER = 460_000_000
TARGET = 460_100_000             # +100 kHz
OTHER = 459_850_000              # -150 kHz: must not end up in the crops
DUR = 7.0
KEY = (1.5, 5.5)                 # the target is keyed only here


def _recording(d):
    rng = np.random.default_rng(1)
    n = int(DUR * FS)
    t = np.arange(n) / FS
    audio = np.sin(2 * np.pi * 1000 * t)
    target = np.exp(1j * (2 * np.pi * (TARGET - CENTER) * t + 2 * np.pi * 3000 * np.cumsum(audio) / FS))
    target *= ((t >= KEY[0]) & (t < KEY[1]))
    other = 0.5 * np.exp(2j * np.pi * (OTHER - CENTER) * t)
    x = 0.3 * target + other + 0.01 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    iq = np.empty(2 * n, np.int16)
    iq[0::2] = np.clip(x.real * 32767 * 0.5, -32767, 32767)
    iq[1::2] = np.clip(x.imag * 32767 * 0.5, -32767, 32767)
    p = os.path.join(d, "iq_sub_460.1000MHz_25kHz_20260101_120000.cs16")
    iq.tofile(p)
    json.dump({"center_hz": CENTER, "chan_hz": TARGET, "samp_rate": FS, "scope": "subband",
               "start_time": 1767268800.0, "decoded": {"label": "X", "protocols": {"X": {"unit": [42]}}}},
              open(p + ".json", "w"))
    return p


def test_crop_and_ingest():
    d = tempfile.mkdtemp(prefix="sdrexplorer-contrib-")
    rec = _recording(d)
    out = os.path.join(d, "pkg.zip")
    path, metas = C.crop(rec, "NBFM", bw_hz=16_000, rate=48_000, seconds=1.0, crops=5, out=out, verify=False)
    assert path == out and 1 <= len(metas) <= 4, metas        # only the 4 s the target is keyed
    allowed = set(C.META_KEYS)
    for m in metas:
        assert set(m) <= allowed, set(m) - allowed
        assert m["freq_hz"] == 0.0 and m["time"] == "" and "band" not in m
        assert m["fs"] == 48_000 and m["n"] == 48_000 and m["snr_db"] >= C.MIN_SNR_DB
        assert m["recording"].startswith("contrib-") and m["source"] == "submitted"
    assert len({m["recording"] for m in metas}) == 1           # one split key per package

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        blob = b"".join(zf.read(n) for n in names if n.endswith(".json"))      # the metadata (README is fixed text)
        for bad in (b"460100000", b"460.1", b"460000000", b"459850000", b"20260101", b"1767268800",
                    b"unit", b"iq_sub", b"center_hz", b"chan_hz"):      # (not "460": random hex ids hold it)
            assert bad not in blob, bad
        man = json.loads(zf.read("manifest.json"))
        assert len(man["items"]) == len(metas)
        it = man["items"][0]
        iq = np.load(io.BytesIO(zf.read(f"NBFM/{it['id']}/iq.npy")), allow_pickle=False)
    assert iq.dtype == np.complex64 and abs(np.mean(np.abs(iq) ** 2) - 1) < 0.01      # unit RMS
    P = np.abs(np.fft.fftshift(np.fft.fft(iq))) ** 2
    f = (np.arange(len(iq)) - len(iq) // 2) * 48_000 / len(iq)
    assert P[np.abs(f) < 8_000].sum() > 0.95 * P.sum()        # the target, at 0 Hz; the other signal gone

    # --band keeps a coarse band name and still no frequency
    _p, m2 = C.crop(rec, "NBFM", bw_hz=16_000, rate=48_000, seconds=1.0, crops=1, band=True,
                    out=os.path.join(d, "b.zip"), verify=False)
    assert m2[0]["band"] == "UHF" and m2[0]["freq_hz"] == 0.0

    # outside the recording, or nothing keyed: refused with a reason
    for kw in ({"freq_hz": 461_000_000}, {"freq_hz": 460_000_000 + 200_000, "bw_hz": 16_000}):
        try:
            C.crop(rec, "NBFM", out=os.path.join(d, "x.zip"), verify=False, **kw)
            raise AssertionError(f"accepted {kw}")
        except ValueError:
            pass

    # ingest into a store
    root = os.path.join(d, "store")
    dirs = C.ingest(out, "test48", root=root)
    ex = store.examples("test48", root=root)
    assert len(dirs) == len(metas) == len(ex)
    for _dir, m in ex:
        assert m["label"] == "NBFM" and m["source"] == "submitted" and m["freq_hz"] == 0.0
        assert store.trusted(m)
    w = store.to_windows("test48", 4096, ["noise", "NBFM"], held=False, root=root)
    assert len(w[0]) > 0

    # a tampered package: an item whose iq doesn't match its meta is refused
    bad = os.path.join(d, "bad.zip")
    with zipfile.ZipFile(out) as src, zipfile.ZipFile(bad, "w") as dst:
        for n in src.namelist():
            data = src.read(n)
            if n.endswith("meta.json"):
                m = json.loads(data)
                m["n"] = 7
                data = json.dumps(m).encode()
            dst.writestr(n, data)
    try:
        C.ingest(bad, "test48", root=os.path.join(d, "store2"))
        raise AssertionError("accepted a tampered package")
    except ValueError:
        pass
    print("ok  test_crop_and_ingest")


def test_cli():
    d = tempfile.mkdtemp(prefix="sdrexplorer-contrib-cli-")
    rec = _recording(d)
    out = os.path.join(d, "cli.zip")
    assert C.main(["crop", rec, "--label", "NBFM", "--bw", "16", "--seconds", "1", "--out", out]) == 0
    assert C.main(["ingest", out, "--profile", "p", "--store", os.path.join(d, "s")]) == 0
    assert C.main(["crop", rec, "--label", "NBFM", "--freq", "470"]) == 1      # error, not a traceback
    print("ok  test_cli")


if __name__ == "__main__":
    test_crop_and_ingest()
    test_cli()
    sys.stdout.flush()
