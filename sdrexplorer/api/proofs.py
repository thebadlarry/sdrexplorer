# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Decoder proofs: independent evidence that a crop of I/Q really is a given signal mode.

A proof is ``fn(iq, fs) -> dict`` with at least {"ok": bool, "decoder": str, "frames": int, "clean": int,
"crc_err": int, "ids": [..], "detail": [str]} (``ids``: system identities -- a color code, an access
code, a site id, ...). ``iq`` is complex at ``fs`` (24 or 48 kHz), the signal centered at 0 Hz.

Modules publish proofs headlessly (no Qt, no flowgraph), under the "sdrexplorer.proofs" entry point,
which names a dict {label: fn} or a callable returning one. Modules' decoder checks use them,
and so does sdrexplorer-train, without loading the module that provides them.

    proofs.get("MODE-A")      the proof for a label, or None
    proofs.labels()           every label with a proof
    proofs.verify(iq, fs, label)
"""
from __future__ import annotations

import sys

GROUP = "sdrexplorer.proofs"
_registry = {}          # label -> (fn, provider)
_loaded = False


def register(label, fn, provider="core"):
    _registry[label] = (fn, provider)


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    # module repo checkouts beside the core (no pip install needed), then installed packages
    try:
        from .. import modules as _modules
        for _d, eps in _modules._checkouts():
            for name, ref in eps.get(GROUP, {}).items():
                _modules._register_proofs(name, ref)
    except Exception as e:
        print(f"[sdrexplorer] proofs from module checkouts not loaded: {e}", file=sys.stderr)
    from importlib.metadata import entry_points
    for ep in entry_points(group=GROUP):
        try:
            obj = ep.load()
            table = obj() if callable(obj) else obj
            for label, fn in dict(table).items():
                _registry.setdefault(label, (fn, ep.name))
        except Exception as e:
            print(f"[sdrexplorer] proofs from {ep.name} not loaded: {e}", file=sys.stderr)


def get(label):
    _load()
    hit = _registry.get(label)
    return hit[0] if hit else None


def provider(label):
    _load()
    hit = _registry.get(label)
    return hit[1] if hit else None


def labels():
    _load()
    return sorted(_registry)


def verify(iq, fs, label):
    fn = get(label)
    if fn is None:
        return {"ok": False, "decoder": None, "frames": 0, "clean": 0, "crc_err": 0, "ids": [],
                "detail": [f"no proof for {label} (no installed module provides one)"]}
    return fn(iq, fs)


def reset():
    """Tests: forget everything registered and re-read the entry points on next use."""
    global _loaded
    _registry.clear()
    _loaded = False
