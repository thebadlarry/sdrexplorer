# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Shared in-memory store of geolocated targets for the live map.

Deliberately Qt-free and GNU-Radio-free: it is fed from the GUI thread by both
the ADS-B aircraft feed (``AdsbMonitor.aircraft`` -> ``_on_aircraft``) and modules'
positions (``api.Position`` -> ``publish_position``), and read once a second by the map
window's refresh timer. Keeping it dependency-free makes it trivially unit
testable offscreen.

A "target" is anything with a position and an identity that persists across
updates: an aircraft (keyed by ICAO) or a radio (keyed by its decoded station
id). Each keeps a bounded position track so the map can draw a trail.
"""
import math
import time
from collections import deque


def _finite(v):
    """True only for a real, finite number — rejects None, "", and NaN/inf.

    gr-adsb emits ``np.nan`` for a field (lat/lon/speed/…) that has not been
    resolved yet, so every numeric that reaches the store is screened here.
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


class Target:
    """One tracked entity. ``kind`` is ``"aircraft"`` or ``"radio"``."""
    __slots__ = ("id", "kind", "lat", "lon", "label", "attrs",
                 "first_seen", "last_seen", "track")

    def __init__(self, id, kind, now, track_len):
        self.id = id
        self.kind = kind
        self.lat = None            # last known good position (None until first fix)
        self.lon = None
        self.label = id
        self.attrs = {}            # kind-specific extras (altitude, speed, …)
        self.first_seen = now
        self.last_seen = now
        self.track = deque(maxlen=track_len)


class TargetStore:
    """Upsert-by-id store with bounded per-target position tracks.

    ``clock`` is injectable so tests don't depend on wall-clock time.
    """

    def __init__(self, track_len=60, clock=time.time):
        self._targets = {}
        self._track_len = track_len
        self._clock = clock

    def upsert(self, id, kind, lat=None, lon=None, label=None, **attrs):
        """Create or update a target. A finite lat/lon that differs from the
        last one is appended to the track; NaN/None positions and attrs are
        ignored (the previous good value is kept). Returns the Target."""
        id = str(id)
        now = self._clock()
        t = self._targets.get(id)
        if t is None:
            t = Target(id, kind, now, self._track_len)
            self._targets[id] = t
        t.kind = kind
        t.last_seen = now
        if label:
            t.label = str(label)
        for k, v in attrs.items():
            if v is None or (isinstance(v, float) and v != v):   # skip None/NaN
                continue
            t.attrs[k] = v
        if _finite(lat) and _finite(lon):
            lat, lon = float(lat), float(lon)
            pt = [round(lat, 6), round(lon, 6)]
            if not t.track or t.track[-1] != pt:   # de-dup repeated squitters
                t.track.append(pt)
            t.lat, t.lon = lat, lon
        return t

    def prune(self, max_age):
        """Drop targets not seen within ``max_age`` seconds."""
        now = self._clock()
        for tid in [i for i, t in self._targets.items()
                    if now - t.last_seen > max_age]:
            del self._targets[tid]

    def snapshot(self, positioned_only=True):
        """A JSON-serializable list of targets for the map. By default only
        targets with a resolved position are returned."""
        now = self._clock()
        out = []
        for t in self._targets.values():
            if positioned_only and (t.lat is None or t.lon is None):
                continue
            out.append({
                "id": t.id,
                "kind": t.kind,
                "lat": t.lat,
                "lon": t.lon,
                "label": t.label,
                "attrs": t.attrs,
                "age": round(now - t.last_seen, 1),
                "track": list(t.track),
            })
        return out

    def clear(self):
        self._targets.clear()

    def __len__(self):
        return len(self._targets)
