# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Share a recording safely: cut one signal out of an IQ recording, keep only short crops of it, and drop
everything that says where or when it was recorded. The result is a small zip in the real-data store's
layout (docs/realstore-format.md) that someone can send for others to train on, and that the receiver adds
to their store with ``ingest``.

    python -m sdrexplorer.contribute crop REC.cs16 --label NFM [--freq MHZ] [--bw KHZ] [--rate HZ]
                                          [--seconds S] [--crops N] [--band] [--out FILE.zip]
    python -m sdrexplorer.contribute ingest FILE.zip --profile NAME [--store DIR]

What a package holds, per crop: complex64 I/Q at --rate with the signal at 0 Hz, scaled to unit RMS, and a
meta.json with the label, the sample rate, the bandwidth, the crop's in-band SNR and, when an installed
module has a decoder proof for the label, whether the crop passed it (counts only). Left out: the frequency
(--band keeps a coarse band name), the recording's file name, start time and sidecar, positions, decoded ids
and text, and the signal's absolute level. Example ids are random; one random token per package keeps its
crops together on one side of a training / held-out split.

Not removable: what the signal itself carries. A crop of voice is still voice, and a data burst may still
hold unit ids or a position. Short crops limit that; share only signals you may share.

numpy + scipy only (no Qt, no GNU Radio), so it also runs where the app doesn't.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import uuid
import zipfile
from fractions import Fraction

import numpy as np
from scipy import signal as sps

from .api import proofs, store

FORMAT = 1
DEFAULT_RATE = 48_000
DEFAULT_BW = 25_000.0
DEFAULT_SECONDS = 2.0
DEFAULT_CROPS = 5
MIN_SNR_DB = 6.0                 # a crop's in-band power over out-of-band, for "signal on"
CHUNK = 1 << 20                  # input samples per processing block
# keys a crop's meta.json may carry: everything else is dropped (an allow-list, so nothing new leaks)
META_KEYS = ("format", "label", "profile", "fs", "n", "freq_hz", "recording", "source", "time", "bw_hz",
             "snr_db", "band", "verify")
VERIFY_KEYS = ("ok", "decoder", "frames", "clean", "crc_err")      # no "ids", no "detail"
BANDS = [(0, 30e6, "HF"), (30e6, 50e6, "VHF low"), (50e6, 88e6, "6 m / VHF"), (88e6, 136e6, "VHF air / FM"),
         (136e6, 174e6, "VHF high"), (174e6, 380e6, "VHF / UHF misc"), (380e6, 520e6, "UHF"),
         (520e6, 806e6, "UHF TV / 700"), (806e6, 960e6, "800 / 900"), (960e6, 6e9, "microwave")]


# ------------------------------------------------------------------------------------------------ reading
def read_recording(path):
    """(memmap of interleaved int16, sidecar dict). The sidecar is <path>.json (sdrexplorer's)."""
    side = path + ".json"
    meta = json.load(open(side)) if os.path.exists(side) else {}
    raw = np.memmap(path, dtype=np.int16, mode="r")
    return raw[: len(raw) // 2 * 2], meta


def band_name(freq_hz):
    for lo, hi, name in BANDS:
        if lo <= freq_hz < hi:
            return name
    return ""


def channelize(raw, fs, offset_hz, bw_hz, rate, start=0, count=None):
    """Shift ``offset_hz`` (from the recording's center) to 0 Hz and resample from ``fs`` to ``rate``, low-passed
    only against aliasing: the crop spans the whole output rate, as the explorer's slices do, so neighbors
    and the noise beside the signal stay in it (the signal-on test needs them; ``bw_hz`` is not a filter). Works through the file in blocks (exact overlap-save), so a long full-band
    recording never has to fit in memory. Returns complex64 at ``rate``."""
    n_total = len(raw) // 2
    stop = n_total if count is None else min(n_total, start + count)
    d1 = max(1, int(fs // (4 * rate)))                  # integer first stage, to >= 4x the output rate
    fs1 = fs / d1
    cutoff = min(0.45 * fs1, 0.45 * rate)
    ntaps = int(min(8191, max(63, 8 * d1 * 4 + 1))) | 1
    taps = sps.firwin(ntaps, cutoff, fs=fs).astype(np.float32)
    tail = np.zeros(ntaps - 1, np.complex64)
    out, phase0 = [], 0                                  # phase0: filtered samples produced so far
    w = -2j * np.pi * offset_hz / fs
    for s in range(start, stop, CHUNK):
        e = min(stop, s + CHUNK)
        x = raw[2 * s: 2 * e].astype(np.float32).view(np.complex64) / 32767.0
        x = x * np.exp(w * (np.arange(len(x)) + (s - start))).astype(np.complex64)
        buf = np.concatenate([tail, x])
        y = sps.oaconvolve(buf, taps, mode="valid")      # y[k] = filtered input sample s - start + k
        tail = buf[len(buf) - (ntaps - 1):]
        first = (-phase0) % d1                           # keep every d1-th sample across block edges
        out.append(y[first::d1].astype(np.complex64))
        phase0 += len(y)
    z = np.concatenate(out) if out else np.zeros(0, np.complex64)
    q = Fraction(rate / fs1).limit_denominator(1000)
    if q != 1:
        z = sps.resample_poly(z, q.numerator, q.denominator).astype(np.complex64)
    return z


def find_signal(raw, fs, offset_hz, bw_hz, seconds=10.0):
    """The center of the signal near ``offset_hz`` (from the recording's center): the power-weighted middle of
    the bins within +-bw of it that stand >= 6 dB over the recording's median, from an averaged spectrum of
    up to ``seconds`` taken across the file. Returns ``offset_hz`` unchanged when nothing stands out."""
    n_total = len(raw) // 2
    nfft = 1 << int(np.clip(np.ceil(np.log2(fs / 100.0)), 8, 18))          # ~100 Hz bins
    blocks = max(1, min(64, int(seconds * fs) // nfft))
    starts = np.linspace(0, max(0, n_total - nfft), blocks).astype(int)
    win = np.hanning(nfft).astype(np.float32)
    P = np.zeros(nfft)
    for s in starts:
        x = raw[2 * s: 2 * (s + nfft)].astype(np.float32).view(np.complex64)
        if len(x) == nfft:
            P += np.abs(np.fft.fftshift(np.fft.fft(x * win))) ** 2
    f = (np.arange(nfft) - nfft // 2) * fs / nfft
    near = np.abs(f - offset_hz) <= bw_hz
    if not near.any() or not P.any():
        return offset_hz
    db = 10 * np.log10(P + 1e-20)
    on = near & (db > np.median(db) + 6)
    if not on.any():
        return offset_hz
    return float(np.sum(f[on] * P[on]) / np.sum(P[on]))


def snr_db(w, rate, bw_hz):
    """In-band power over out-of-band power of one crop, dB (as the store's signal-on gate)."""
    P = np.abs(np.fft.fftshift(np.fft.fft(w))) ** 2
    f = (np.arange(len(w)) - len(w) // 2) * rate / len(w)
    half = max(500.0, min(0.4 * rate, bw_hz / 2))
    inb, outb = P[np.abs(f) < half], P[np.abs(f) > min(0.45 * rate, half * 1.3)]
    if not outb.size or not inb.size:
        return 0.0
    return float(10 * np.log10(inb.mean() / (outb.mean() + 1e-20) + 1e-20))


def pick_crops(z, rate, bw_hz, seconds, crops, min_snr_db=MIN_SNR_DB):
    """Up to ``crops`` non-overlapping windows of ``seconds``, strongest first (by the in-band SNR of each
    window's weakest quarter, so a crop that is half silence doesn't count as on), each above
    ``min_snr_db`` (None: no threshold, e.g. for an empty-channel "noise" label). [(start, snr_db)]."""
    L = int(seconds * rate)
    if len(z) < L:
        return []
    step = max(1, L // 4)
    q = L // 4                       # a crop scores as its weakest quarter: the signal must be on throughout
    cands = [(s, min(snr_db(z[s + i * q:s + (i + 1) * q], rate, bw_hz) for i in range(4)))
             for s in range(0, len(z) - L + 1, step)]
    cands.sort(key=lambda c: -c[1])
    out = []
    for s, snr in cands:
        if min_snr_db is not None and snr < min_snr_db:
            break
        if all(abs(s - o) >= L for o, _ in out):
            out.append((s, snr))
        if len(out) >= crops:
            break
    return out


def _verify(iq, fs, label):
    """A decoder proof's verdict on a crop, counts only (no ids, no detail), or None without a proof."""
    if proofs.get(label) is None:
        return None
    try:
        r = proofs.verify(iq, fs, label)
    except Exception as e:                               # a proof failing must not stop the package
        return {"ok": None, "decoder": f"error: {type(e).__name__}"}
    return {k: r.get(k) for k in VERIFY_KEYS if k in r}


# ------------------------------------------------------------------------------------------------ crop
def crop(path, label, freq_hz=None, bw_hz=DEFAULT_BW, rate=DEFAULT_RATE, seconds=DEFAULT_SECONDS,
         crops=DEFAULT_CROPS, band=False, profile="", out=None, min_snr_db=MIN_SNR_DB, verify=True,
         start_s=0.0, length_s=None):
    """Make a contribution zip from one recording (or the part from ``start_s`` for ``length_s``).
    Returns (zip path, [crop meta])."""
    raw, side = read_recording(path)
    fs = float(side.get("samp_rate") or 0)
    if not fs:
        raise ValueError(f"{path}: no samp_rate in its sidecar ({path}.json)")
    center = float(side.get("center_hz") or 0)
    target = float(freq_hz) if freq_hz is not None else float(side.get("chan_hz") or center)
    offset = target - center if center else 0.0
    if freq_hz is None:              # no frequency given: center on the signal actually there, near the guess
        offset = find_signal(raw, fs, offset, bw_hz)
        target = center + offset
    if abs(offset) + bw_hz / 2 > fs / 2:
        raise ValueError(f"{target / 1e6:.4f} MHz with {bw_hz / 1e3:g} kHz isn't inside this recording "
                         f"({center / 1e6:.4f} MHz ± {fs / 2e6:g} MHz)")
    if rate < bw_hz:
        raise ValueError(f"--rate {rate} is narrower than --bw {bw_hz:g}: raise the rate or narrow the bandwidth")
    z = channelize(raw, fs, offset, bw_hz, rate, start=int(start_s * fs),
                   count=None if length_s is None else int(length_s * fs))
    chosen = pick_crops(z, rate, bw_hz, seconds, crops, min_snr_db)
    if not chosen:
        raise ValueError(f"no {seconds:g} s stretch where the signal is on (>= {min_snr_db} dB in band) at "
                         f"{target / 1e6:.4f} MHz: check --freq / --bw, or use --any for a quiet label")
    token = uuid.uuid4().hex[:12]                        # the split key: all crops of this package together
    order = np.random.default_rng().permutation(len(chosen))
    items = []
    L = int(seconds * rate)
    for k in order:
        s, snr = chosen[k]
        iq = z[s:s + L].astype(np.complex64)
        iq = iq / (np.sqrt(np.mean(np.abs(iq) ** 2)) + 1e-12)
        m = {"format": FORMAT, "label": label, "profile": profile, "fs": float(rate), "n": int(len(iq)),
             "freq_hz": 0.0, "recording": f"contrib-{token}", "source": "submitted", "time": "",
             "bw_hz": float(bw_hz), "snr_db": round(snr, 1)}
        if band:
            m["band"] = band_name(target)
        if verify:
            v = _verify(iq, rate, label)
            if v is not None:
                m["verify"] = v
        items.append((uuid.uuid4().hex[:16], iq.astype(np.complex64), clean_meta(m)))
    out = out or f"contribution-{token}.zip"
    write_zip(out, label, items)
    return out, [m for _i, _x, m in items]


def clean_meta(m):
    m = {k: v for k, v in m.items() if k in META_KEYS}
    if isinstance(m.get("verify"), dict):
        m["verify"] = {k: v for k, v in m["verify"].items() if k in VERIFY_KEYS}
    return m


README = """sdrexplorer contribution
========================

Short crops of one signal, for training signal classifiers. Each crop is <label>/<id>/iq.npy (complex64,
the signal at 0 Hz, unit RMS) with a meta.json beside it: label, sample rate, bandwidth, in-band SNR, a
decoder check's pass / fail and frame counts when one exists, and a coarse band only if the sender chose
to include it.

Left out on purpose: the frequency, the recording's name, date and time, the sidecar, any position, decoded
ids or text, and the signal's absolute level.

Still inside: whatever the signal itself carries (a crop of voice is voice; a data burst may hold unit ids
or a position).

Add it to a real-data store with:  python -m sdrexplorer.contribute ingest <this zip> --profile <name>
"""


def write_zip(path, label, items):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", README)
        manifest = {"format": FORMAT, "kind": "sdrexplorer-contribution",
                    "items": [{"id": i, "label": m["label"], "fs": m["fs"], "n": m["n"]} for i, _x, m in items]}
        zf.writestr("manifest.json", json.dumps(manifest, indent=1))
        for i, iq, m in items:
            buf = io.BytesIO()
            np.save(buf, iq, allow_pickle=False)
            base = f"{store._safe(label)}/{i}/"
            zf.writestr(base + "iq.npy", buf.getvalue())
            zf.writestr(base + "meta.json", json.dumps(m, indent=1))


# ------------------------------------------------------------------------------------------------ ingest
def ingest(path, profile, root=None):
    """Add a contribution zip's crops to the real-data store under ``profile``. Reads only the files the
    manifest lists (nothing is extracted to disk by name), checks each one, and re-cleans its meta.
    Returns the example directories written."""
    out = []
    with zipfile.ZipFile(path) as zf:
        man = json.loads(zf.read("manifest.json"))
        if man.get("kind") != "sdrexplorer-contribution":
            raise ValueError(f"{path}: not an sdrexplorer contribution")
        names = set(zf.namelist())
        for it in man.get("items", []):
            iid, label = str(it.get("id", "")), str(it.get("label", ""))
            if not iid.isalnum() or not label:
                raise ValueError(f"{path}: bad item {it!r}")
            base = f"{store._safe(label)}/{iid}/"
            if base + "iq.npy" not in names or base + "meta.json" not in names:
                raise ValueError(f"{path}: item {iid} is incomplete")
            m = clean_meta(json.loads(zf.read(base + "meta.json")))
            iq = np.load(io.BytesIO(zf.read(base + "iq.npy")), allow_pickle=False)
            if iq.dtype != np.complex64 or iq.ndim != 1 or len(iq) != int(m.get("n", -1)):
                raise ValueError(f"{path}: item {iid} doesn't match its meta.json")
            if m.get("label") != label:
                raise ValueError(f"{path}: item {iid} label mismatch")
            extra = {k: m[k] for k in ("bw_hz", "snr_db", "band", "verify") if k in m}
            out.append(store.add(profile, label, iq, m["fs"], 0.0, m.get("recording") or f"contrib-{iid}",
                                 source="submitted", root=root, **extra))
    return out


# ------------------------------------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m sdrexplorer.contribute",
                                 description="Cut a signal out of a recording for sharing (no frequency, "
                                             "time or position kept), or add a shared package to your store.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crop", help="make a contribution zip from one IQ recording (.cs16 + .json sidecar)")
    c.add_argument("recording")
    c.add_argument("--label", required=True, help="what the signal is, as the store names it (e.g. NBFM)")
    c.add_argument("--freq", type=float, help="the signal's frequency, MHz (default: the tuned one in the sidecar)")
    c.add_argument("--bw", type=float, default=DEFAULT_BW / 1e3, help="its bandwidth, kHz (default 25)")
    c.add_argument("--rate", type=int, default=DEFAULT_RATE, help="output sample rate, Hz (default 48000)")
    c.add_argument("--seconds", type=float, default=DEFAULT_SECONDS, help="length of each crop (default 2)")
    c.add_argument("--crops", type=int, default=DEFAULT_CROPS, help="at most this many crops (default 5)")
    c.add_argument("--band", action="store_true", help="keep a coarse band name (e.g. 'UHF'), not the frequency")
    c.add_argument("--any", action="store_true", help="no signal-on threshold (for a quiet label like noise)")
    c.add_argument("--start", type=float, default=0.0, help="use the recording from here, s (default 0)")
    c.add_argument("--length", type=float, help="use only this much of it, s (default: to the end)")
    c.add_argument("--profile", default="", help="the store profile it is meant for (optional)")
    c.add_argument("--out", help="the zip to write (default contribution-<random>.zip)")
    g = sub.add_parser("ingest", help="add a contribution zip to your real-data store")
    g.add_argument("zip")
    g.add_argument("--profile", required=True, help="the store profile to add it to (e.g. vu48)")
    g.add_argument("--store", help="store directory (default: paths key store_dir)")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "crop":
            path, metas = crop(a.recording, a.label, None if a.freq is None else a.freq * 1e6, a.bw * 1e3,
                               a.rate, a.seconds, a.crops, a.band, a.profile, a.out,
                               None if a.any else MIN_SNR_DB, start_s=a.start, length_s=a.length)
            print(f"{path}: {len(metas)} crop(s) of {a.seconds:g} s, {a.label}, {a.rate} Hz")
            for m in metas:
                v = m.get("verify")
                print(f"  SNR {m['snr_db']:5.1f} dB" + (f"  decoder check: {'pass' if v.get('ok') else 'fail'}"
                                                         if v else ""))
            print("Kept: the signal at 0 Hz, its label, rate and bandwidth. Not kept: frequency, time, file name, "
                  "position, ids. What the signal itself carries (voice, data) is still in it.")
        else:
            dirs = ingest(a.zip, a.profile, a.store)
            print(f"{len(dirs)} example(s) added to profile {a.profile}")
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
