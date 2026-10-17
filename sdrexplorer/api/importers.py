# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Import providers (module API): where Memories ▸ Import gets channels from.

The core's own (CSV file, FCC ULS, RadioReference) register here like a module's would; a module returns
more from ``Module.importers()`` (RepeaterBook, an SDRTrunk playlist, TLE sources...). Each appears in the
Import picker; "Import → bank" runs it off the Qt thread and puts what it returns into a new bank.

    from sdrexplorer.api import importers, FoundChannel

    def run(req):                       # an ImportRequest; runs on a worker thread
        return [FoundChannel(freq=146_520_000, label="Calling", mode="NFM", source="mine",
                             meta={"tone": "100.0 PL", "ext": {"mymodule": {"talkgroup": 9}}})]

    importers.ImportProvider("My list", run, needs_location=False)

A FoundChannel's meta "place" / "tone" / "ext" go onto the bank channel (``ext`` = module fields, see
Module.channel_fields).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..providers.base import FoundChannel, geofence, haversine_km   # noqa: F401  (re-exported)


@dataclass
class ImportRequest:
    location: str = ""                  # the Where box as typed ("" = none)
    lat: Optional[float] = None         # resolved from it (None when blank / not needed)
    lon: Optional[float] = None
    radius_km: float = 40.0
    path: str = ""                      # the chosen file, for a provider with a file_filter
    keep_unlocated: bool = True


@dataclass
class ImportProvider:
    name: str                           # shown in the Import picker; saved in the session state
    run: Callable                       # (ImportRequest) -> [FoundChannel]; worker thread; raise to fail
    needs_location: bool = True         # needs a Where location (geocoded before run)
    file_filter: str = ""               # non-empty: needs a file, e.g. "CSV files (*.csv);;All files (*)"
    available: Optional[Callable] = None  # () -> (ok, why): checked when picked and before running
    widgets: Optional[Callable] = None  # (parent) -> [QWidget] shown next to the picker while it's selected
    tooltip: str = ""
    owner: str = ""                     # set by the core: "" for its own, else the module's name

    def check(self):
        if self.available is None:
            return True, ""
        try:
            r = self.available()
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        return (r, "") if isinstance(r, bool) else (bool(r[0]), str(r[1]))


_providers = []


def register(provider, owner=""):
    """Add (or, by name, replace) an import provider."""
    provider.owner = owner or provider.owner
    for i, p in enumerate(_providers):
        if p.name == provider.name:
            _providers[i] = provider
            return provider
    _providers.append(provider)
    return provider


def providers():
    return list(_providers)


def get(name):
    return next((p for p in _providers if p.name == name), None)


def reset():
    """Tests."""
    _providers.clear()
