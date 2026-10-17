# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Location→frequency fetch worker (runs off the UI thread).

Geocodes a location, runs the chosen provider, geofences by radius, filters by
service, and hands back FoundChannels for the UI to drop into a bank.
"""
from __future__ import annotations

import os
import sys

from PyQt5 import QtCore

from . import paths as _paths
from .providers import geocode, csv_import, fcc_uls
from .providers import radioreference as rr

# Public-safety terms are treated as one family so ticking "Police" still keeps
# an FCC record coded broadly as "Public Safety".
_PS_FAMILY = {"police", "fire", "ems", "public safety", "emergency", "sheriff"}



def _region_hint(location):
    """Extract a region (state/province) from the Where text so ambiguous town
    names geocode correctly — e.g. "Boston, MA" -> "MA"."""
    if location and "," in location:
        return location.split(",")[-1].strip()
    return ""


def _load_geocode_cache():
    import json
    try:
        with open(_paths.memory_file("geocode_cache.json")) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_geocode_cache(cache):
    import json
    try:
        path = _paths.memory_file("geocode_cache.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(cache, fh, indent=2)
    except OSError:
        pass


def service_matches(chan_service, selected):
    if not selected:
        return True
    s = (chan_service or "").lower()
    for tag in selected:
        t = tag.lower()
        if t == s or (t and t in s) or (s and s in t):
            return True
        if t in _PS_FAMILY and any(f in s for f in _PS_FAMILY):
            return True
    return False


class FetchWorker(QtCore.QThread):
    """Runs one import provider (api.importers) off the UI thread: geocodes the Where text if the provider
    needs a location, runs it, filters by service."""
    done = QtCore.pyqtSignal(list, str)     # (FoundChannels, resolved label)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, location, provider, path, radius_km, services,
                 keep_unlocated=False, parent=None):
        super().__init__(parent)
        self.location = location
        self.provider = provider            # an api.importers.ImportProvider
        self.path = path
        self.radius_km = radius_km
        self.services = services            # list of ticked service tags
        self.keep_unlocated = keep_unlocated

    def run(self):
        from .api.importers import ImportRequest
        p = self.provider
        ok, why = p.check()
        if not ok:
            self.failed.emit(f"{p.name}: {why}")
            return
        if p.file_filter and not self.path:
            self.failed.emit(f"{p.name}: choose a file first")
            return
        loc = geocode.resolve(self.location) if (self.location and p.needs_location) else None
        if loc:
            lat, lon, label = loc
        elif p.needs_location:
            self.failed.emit(f"could not resolve location: {self.location!r}")
            return
        else:
            # no location needed (a file): no geofence, and the bank is named after the file
            lat = lon = None
            label = (os.path.splitext(os.path.basename(self.path))[0] if self.path else "") \
                or self.location or p.name
        req = ImportRequest(location=self.location or "", lat=lat, lon=lon, radius_km=self.radius_km,
                            path=self.path or "", keep_unlocated=self.keep_unlocated)
        try:
            results = list(p.run(req) or [])
        except Exception as e:                       # provider I/O, parse, etc.
            self.failed.emit(f"{p.name}: {e}")
            return
        results = [c for c in results
                   if service_matches(c.service, self.services)]
        self.done.emit(results, label)


# -- the core's own import providers ------------------------------------------------------------------------
def _run_fcc(req):
    if not fcc_uls.has_data(_paths.uls_dir()):
        raise RuntimeError("no FCC data — Download it first")
    chans = fcc_uls.parse_uls_dir(_paths.uls_dir())
    return fcc_uls.query(chans, req.lat, req.lon, req.radius_km)


def _run_csv(req):
    # With a location, a City column may be geocoded so the Where box filters city-tagged CSVs
    # (cached across sessions); without one every row is kept.
    resolver = region = cache = None
    if req.lat is not None:
        resolver = geocode.resolve
        region = _region_hint(req.location)
        cache = _load_geocode_cache()
    res = csv_import.load_csv(req.path, req.lat, req.lon, req.radius_km,
                              keep_unlocated=req.keep_unlocated,
                              resolver=resolver, region=region or "",
                              geocode_cache=cache)
    if cache is not None:
        _save_geocode_cache(cache)
    return res


def _run_rr(req):
    if not rr.available():
        raise RuntimeError("no RadioReference account (Settings ▸ RadioReference account…)")
    return rr.RadioReferenceProvider().lookup_zip(req.location.strip())


def _rr_available():
    try:
        import zeep  # noqa: F401
    except ImportError:
        return False, "needs the zeep package (pip install zeep)"
    return (True, "") if rr.available() else (False, "no account (Settings ▸ RadioReference account…)")


def register_core_importers(fcc_widgets=None):
    """CSV file, FCC ULS, RadioReference, in the Import picker's order. ``fcc_widgets``: the app's FCC
    download / search controls, shown while FCC ULS is picked."""
    from .api import importers
    importers.register(importers.ImportProvider(
        "CSV file", _run_csv, needs_location=False, file_filter="CSV files (*.csv);;All files (*)",
        tooltip="A CSV file: every row (frequency, mode, name, tone, ...; see the README)."))
    importers.register(importers.ImportProvider(
        "FCC ULS", _run_fcc, widgets=fcc_widgets,
        available=lambda: (True, "") if fcc_uls.has_data(_paths.uls_dir()) else
        (False, "no FCC data — Download it first"),
        tooltip="Licenses from the downloaded FCC data, around the Where location."))
    importers.register(importers.ImportProvider(
        "RadioReference", _run_rr, available=_rr_available,
        tooltip="Your RadioReference Premium account's county data for the Where zip code."))


class GeocodeWorker(QtCore.QThread):
    """Resolve a location string to coordinates off the UI thread (Nominatim
    can be slow), so the live filter's Where box doesn't freeze the GUI."""

    done = QtCore.pyqtSignal(float, float, str)     # lat, lon, label
    failed = QtCore.pyqtSignal(str)

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.text = text

    def run(self):
        try:
            r = geocode.resolve(self.text)
        except Exception as e:                       # network / parse
            self.failed.emit(str(e))
            return
        if r:
            self.done.emit(float(r[0]), float(r[1]), str(r[2]))
        else:
            self.failed.emit(f"could not resolve “{self.text}”")


class DownloadWorker(QtCore.QThread):
    """Stream + extract an FCC ULS weekly dataset off the UI thread."""

    # pct (0 if size unknown), MB downloaded, MB total (0 if unknown)
    progress = QtCore.pyqtSignal(int, float, float)
    done = QtCore.pyqtSignal(str)               # extracted directory
    failed = QtCore.pyqtSignal(str)

    def __init__(self, dataset, dest_dir, parent=None):
        super().__init__(parent)
        self.dataset = dataset
        self.dest_dir = dest_dir
        self._cancel = False
        self._resp = None

    def cancel(self):
        self._cancel = True
        if self._resp is not None:          # unblock a stalled read immediately
            try:
                self._resp.close()
            except Exception:
                pass

    def run(self):
        last = [0]

        def prog(pct, got, total):
            # throttle UI+log to ~1 MB steps (read() fires every 64 KiB)
            if got - last[0] < 1_000_000 and (not total or got < total):
                return
            last[0] = got
            self.progress.emit(pct, got / 1e6, total / 1e6)

        def log(msg):
            sys.stderr.write(f"[fcc-dl] {msg}\n")
            sys.stderr.flush()

        try:
            fcc_uls.download_dataset(self.dataset, self.dest_dir, progress=prog,
                                     cancel=lambda: self._cancel, log=log,
                                     on_open=lambda r: setattr(self, "_resp", r))
            self.done.emit(self.dest_dir)
        except fcc_uls.DownloadCanceled:
            self.failed.emit("download canceled")
        except Exception as e:
            if self._cancel:
                self.failed.emit("download canceled")
            else:
                self.failed.emit(f"download failed: {e}")
