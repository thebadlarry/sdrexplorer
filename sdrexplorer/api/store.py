# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Real-data store: labeled I/Q crops of real signals, for evaluation and training.

Layout (one directory per example, so adding / deleting / relabeling is a file operation):

    <root>/<profile>/<label>/<id>/iq.npy     complex64 at the profile rate (e.g. 24 kHz), ~2 s
    <root>/<profile>/<label>/<id>/meta.json  {format, label, profile, fs, n, freq_hz, recording,
                                              source: "manual"|"sync"|"decoder", time, ...}
    <root>/held_out.json                     {profile: [recording, ...]} reserved for evaluation,
                                             plus "keep_out": {profile: [{recording|id, why}, ...]}

<root> is paths key "store_dir" (default <data>/realdata). The format is part of the module API
(docs/realstore-format.md): sdrexplorer's "Label as..." writes it, and sdrexplorer-train reads it.
FORMAT is written to every new meta.json; examples without one are format 1 too.

Writers: "Label as..." (manual), the sync-word / decoder auto-labelers. Nothing here trains anything.
Readers split held-out data by ``recording``, never by window, so near-duplicate windows of one
transmission can't land on both sides.

numpy only: it's imported under the GNU Radio python, inference venvs and the training venv alike.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid

import numpy as np

from .. import paths as _paths

FORMAT = 1
CROP_S = 2.0                      # seconds saved per example (several model windows)
HELD_OUT_FILE = "held_out.json"   # {profile: [recording, ...]} -- recordings reserved for evaluation
KEEP_OUT = "keep_out"             # key in HELD_OUT_FILE: recordings / crops used on neither side
# labels whose windows are kept whole: the signal-on test (in-band power over out-of-band) would drop
# every window of an empty channel, and a bare carrier is too narrow for its in-band estimate
NO_SIGNAL_GATE = {"noise", "carrier"}


def root():
    return _paths.get("store_dir")


def _safe(s):
    return re.sub(r"[^A-Za-z0-9_.+-]", "_", str(s))


def add(profile, label, iq, fs, freq_hz, recording, source="manual", root=None, **meta):
    """Save one labeled crop. `iq` complex at `fs` (must equal the profile rate); `recording` is the
    split key (the capture file name, or e.g. 'live-YYYYMMDD' for live input). Returns the example
    directory."""
    root = root or globals()["root"]()
    iq = np.asarray(iq, np.complex64)
    eid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    d = os.path.join(root, _safe(profile), _safe(label), eid)
    os.makedirs(d, exist_ok=True)
    np.save(os.path.join(d, "iq.npy"), iq)
    m = {"format": FORMAT, "label": label, "profile": profile, "fs": float(fs), "n": int(len(iq)),
         "freq_hz": float(freq_hz), "recording": str(recording), "source": source,
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    m.update({k: v for k, v in meta.items() if v is not None})
    with open(os.path.join(d, "meta.json"), "w") as fh:
        json.dump(m, fh, indent=1)
    return d


def examples(profile=None, root=None):
    """[(dir, meta)] for every stored example (optionally one profile)."""
    root = root or globals()["root"]()
    out = []
    if not os.path.isdir(root):
        return out
    for prof in sorted(os.listdir(root)):
        pdir = os.path.join(root, prof)
        if not os.path.isdir(pdir) or (profile and prof != profile):
            continue
        for lab in sorted(os.listdir(pdir)):
            ldir = os.path.join(pdir, lab)
            if not os.path.isdir(ldir):
                continue
            for eid in sorted(os.listdir(ldir)):
                mp = os.path.join(ldir, eid, "meta.json")
                if os.path.exists(mp):
                    with open(mp) as fh:
                        out.append((os.path.join(ldir, eid), json.load(fh)))
    return out


def summary(root=None):
    """{profile: {label: {"examples": n, "recordings": k, "seconds": s, "sources": {...}}}}"""
    s = {}
    for d, m in examples(root=root):
        e = s.setdefault(m["profile"], {}).setdefault(m["label"], {"examples": 0, "recordings": set(),
                                                                  "seconds": 0.0, "sources": {}})
        e["examples"] += 1
        e["recordings"].add(m["recording"])
        e["seconds"] += m["n"] / m["fs"]
        e["sources"][m["source"]] = e["sources"].get(m["source"], 0) + 1
    for p in s.values():
        for e in p.values():
            e["recordings"] = len(e["recordings"])
            e["seconds"] = round(e["seconds"], 1)
    return s


def _read_split(root):
    try:
        with open(os.path.join(root, HELD_OUT_FILE)) as fh:
            h = json.load(fh)
        return h if isinstance(h, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_split(root, h):
    os.makedirs(root, exist_ok=True)
    p = os.path.join(root, HELD_OUT_FILE)
    with open(p + ".tmp", "w") as fh:
        json.dump(h, fh, indent=1)
    os.replace(p + ".tmp", p)


def held_out(profile, root=None):
    root = root or globals()["root"]()
    return list(_read_split(root).get(profile, []))


def set_held_out(profile, recordings, root=None):
    root = root or globals()["root"]()
    h = _read_split(root)
    h[profile] = sorted(set(recordings))
    _write_split(root, h)


def keep_out(profile, root=None):
    """[{"recording": name} | {"id": crop id}, with "why"] -- left out of training AND evaluation: a
    recording that would leak into the held-out split, or one bad crop of an otherwise good capture.
    A crop id is the example directory's name."""
    root = root or globals()["root"]()
    return [dict(e) for e in _read_split(root).get(KEEP_OUT, {}).get(profile, [])
            if isinstance(e, dict) and (e.get("recording") or e.get("id"))]


def set_keep_out(profile, entries, root=None):
    root = root or globals()["root"]()
    h = _read_split(root)
    seen, out = set(), []
    for e in entries:
        k = ("recording", e["recording"]) if e.get("recording") else ("id", e["id"])
        if k not in seen:
            seen.add(k)
            out.append({k[0]: k[1], "why": e.get("why", "")})
    h.setdefault(KEEP_OUT, {})[profile] = sorted(out, key=lambda e: (e.get("recording") or "", e.get("id") or ""))
    _write_split(root, h)


def kept_out(profile, root=None):
    """(recordings, crop ids) left out of both sides."""
    ko = keep_out(profile, root)
    return ({e["recording"] for e in ko if e.get("recording")}, {e["id"] for e in ko if e.get("id")})


def is_kept_out(d, m, ko):
    recs, ids = ko
    return m.get("recording") in recs or os.path.basename(os.path.normpath(d)) in ids


def trusted(m):
    """Whether an example's label may be used for training / evaluation:
      - "sync" (auto) examples must have passed an independent decode of the saved crop (a proof);
      - "manual" examples are trusted unless a decoder check of them explicitly FAILED (a human label for
        a mode that has a decoder can still be contradicted by it; modes without one -- NFM, AX.25,
        unknowns -- rely on the human)."""
    v = m.get("verify")
    if m.get("source") == "sync":
        return bool(v and v.get("ok"))
    return not (v and v.get("ok") is False)


def to_windows(profile, L, classes, held=True, signal_on_db=6.0, root=None):
    """Model windows from the store: (X [N,2,1,L] unit-RMS, y class index, rec). held=True -> only the
    held-out recordings, False -> only the others. Only trusted() examples. Windows where the signal is
    off (TDMA gaps, unkeyed) are dropped: in-band power must exceed out-of-band by signal_on_db.
    Kept-out recordings and crops (keep_out) are on neither side."""
    hold = set(held_out(profile, root))
    ko = kept_out(profile, root)
    idx = {c: i for i, c in enumerate(classes)}
    X, y, rec = [], [], []
    for d, m in examples(profile, root):
        if m["label"] not in idx or ((m["recording"] in hold) != held) or not trusted(m) \
                or is_kept_out(d, m, ko):
            continue
        z = np.load(os.path.join(d, "iq.npy"))
        fs = m["fs"]
        gate = m["label"] not in NO_SIGNAL_GATE
        for s in range(0, len(z) - L + 1, L):
            w = z[s:s + L]
            if gate:
                P = np.abs(np.fft.fftshift(np.fft.fft(w))) ** 2
                f = (np.arange(L) - L // 2) * fs / L
                bw = max(1000.0, min(0.4 * fs, 0.6 * m.get("bw_hz", 0.3 * fs)))
                inb, outb = P[np.abs(f) < bw], P[np.abs(f) > min(0.45 * fs, bw * 1.8)]
                if outb.size and 10 * np.log10(inb.mean() / (outb.mean() + 1e-20)) < signal_on_db:
                    continue
            w = w / (np.sqrt(np.mean(np.abs(w) ** 2)) + 1e-12)
            X.append(np.stack([w.real, w.imag]).astype(np.float32)[:, None, :])
            y.append(idx[m["label"]]); rec.append(m["recording"])
    if not X:
        return np.zeros((0, 2, 1, L), np.float32), np.zeros(0, int), np.zeros(0, str)
    return np.stack(X), np.array(y), np.array(rec)
