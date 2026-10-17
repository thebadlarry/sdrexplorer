# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Which frequencies already have recordings -- for the waterfall's "recorded" indicator.

Two sources, merged per 6.25 kHz channel:
  * the real-data store (api.store): every labeled crop carries the
    exact frequency it was cut at and the recording it came from, including the many channels of a
    full-band capture;
  * the IQ recordings' .json sidecars under recordings/ (any depth): a sub-band / demod / snapshot
    recording not yet in the store is placed at its center frequency and labeled by the mode
    folder it sits in (recordings/<MODE>/...), so a fresh capture shows up straight away.
Full-band sidecars are skipped: they span megahertz, not a channel (the store covers their content).

Pure python (no numpy/Qt), so the index can be rebuilt on any thread.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime

CHANNEL_HZ = 6250                      # merge granularity (narrowest LMR channel)
_STAMP_RE = re.compile(r"_(\d{8})_(\d{6})(?:$|\D)")   # sdrexplorer names: ..._YYYYMMDD_HHMMSS
DISPLAY = {"NBFM": "NFM"}   # store labels -> the folder / waterfall names (modules add theirs: ui.label_alias)
NOT_A_MODE = {"old", "heldout", ".claude", ""}
REVIEW_MARK = ".review"     # a folder holding this file is a review folder (e.g. a module's finds): not "recorded"


def known_channels(index):
    """[(freq_hz, label)] for every label of every channel in a build() index."""
    return [(e["freq_hz"], lab) for e in index for lab in (e["labels"] or ["rec"])]


def _key(hz):
    return int(round(float(hz) / CHANNEL_HZ))


def recording_time(rec, fallback=None):
    """When a recording was made: the YYYYMMDD_HHMMSS stamp in its file name (all sdrexplorer
    recordings carry one), else ``fallback`` (e.g. a file mtime), else None."""
    m = _STAMP_RE.search(rec or "")
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            pass
    return fallback


def build(recordings_dir, realstore=None):
    """[{freq_hz, labels: [..], recordings: n, held_out: bool, last: datetime|None}] by frequency."""
    chans = {}

    def add(hz, label, rec, held=False, when=None):
        e = chans.setdefault(_key(hz), {"freq_hz": float(hz), "labels": set(), "recs": set(),
                                        "held_out": False, "last": None})
        if label:
            e["labels"].add(DISPLAY.get(label, label))
        e["recs"].add(rec)
        e["held_out"] = e["held_out"] or held
        when = recording_time(rec, when)
        if when is not None and (e["last"] is None or when > e["last"]):
            e["last"] = when

    stored_recs = set()
    if realstore is not None:
        try:
            hold = set(realstore.held_out("vu"))
            for _d, m in realstore.examples("vu"):
                if m.get("label") in ("noise",):
                    continue                      # an empty channel isn't "a recording of it"
                rec = m.get("recording", "")
                stored_recs.add(rec)
                add(m["freq_hz"], m.get("label"), rec, rec in hold)
        except Exception:
            pass                                  # store unreadable: sidecars still work

    for root, dirs, files in os.walk(recordings_dir):
        # _rejected/: recordings set aside as unconfirmed -- not "recorded" for the markers;
        # a folder marked REVIEW_MARK: results waiting for review -- counted once moved to <MODE>/
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))
                   and not os.path.exists(os.path.join(root, d, REVIEW_MARK))]
        rel = os.path.relpath(root, recordings_dir)
        parts = [] if rel == "." else rel.split(os.sep)
        folder = next((p for p in parts if p not in NOT_A_MODE), "")
        if folder.lower() == "noise":
            continue
        for fn in files:
            if not fn.endswith(".cs16.json"):
                continue
            rec = fn[:-len(".cs16.json")]
            if rec in stored_recs:
                continue                          # the store already placed it exactly
            try:
                with open(os.path.join(root, fn)) as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                continue
            if meta.get("scope", "band") == "band" or "center_hz" not in meta:
                continue
            label = folder or (meta.get("decoded") or {}).get("label")
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(os.path.join(root, fn)))
            except OSError:
                mtime = None
            add(meta["center_hz"], label, rec, "heldout" in parts, mtime)

    out = []
    for e in chans.values():
        out.append({"freq_hz": e["freq_hz"], "labels": sorted(e["labels"]),
                    "recordings": len(e["recs"]), "held_out": e["held_out"], "last": e["last"]})
    return sorted(out, key=lambda e: e["freq_hz"])


def describe(entry):
    """Short text for an index entry, e.g. 'NFM ×3' or 'NFM/AM ×2'. Training and held-out
    aren't told apart: either way the channel is already recorded."""
    lab = "/".join(entry["labels"]) or "rec"
    n = entry["recordings"]
    return f"{lab}" + (f" ×{n}" if n > 1 else "")


def last_text(entry):
    """'last 2026-10-05 14:16' for the most recent recording on a channel ('' if unknown)."""
    t = entry.get("last")
    if t is None:
        return ""
    return f"last {t:%Y-%m-%d %H:%M}"
