# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Extension registries: a module declares a list other modules can add entries to.

    ext.declare("adsb.aircraft_db", doc="ICAO -> type", owner="adsb")
    ext.add("adsb.aircraft_db", {"icao": "A1B2C3", "type": "C130"}, owner="aircraftdb")
    ext.entries("adsb.aircraft_db")

Names are owner-prefixed. Adding to a registry nobody declared is allowed (the entries wait there), so
load order doesn't matter, and ``entries`` of an unknown name is [].
"""
from __future__ import annotations

_regs = {}                 # name -> {"doc", "owner", "declared", "entries": [(owner, entry)]}


def _reg(name):
    if "." not in name:
        raise ValueError(f"registry names are owner-prefixed, '<module>.<name>': {name!r}")
    return _regs.setdefault(name, {"doc": "", "owner": None, "declared": False, "entries": []})


def declare(name, doc="", owner=""):
    r = _reg(name)
    r.update(doc=doc, owner=owner, declared=True)


def add(name, entry, owner=""):
    _reg(name)["entries"].append((owner, entry))


def entries(name):
    r = _regs.get(name)
    return [e for _o, e in r["entries"]] if r else []


def registries():
    """{name: (owner, declared, n_entries, doc)}, for Settings > Modules and the debug view."""
    return {n: (r["owner"], r["declared"], len(r["entries"]), r["doc"]) for n, r in _regs.items()}


def reset():
    """Tests."""
    _regs.clear()
