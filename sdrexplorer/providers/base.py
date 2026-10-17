# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Shared types + geofencing for frequency-data providers."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from ..banks import Channel


@dataclass
class FoundChannel:
    """A frequency discovered from a data source, before it becomes a Channel."""
    freq: int                         # Hz
    label: str = ""
    service: str = ""                 # mapped service tag (Police/Fire/…)
    mode: str = "NFM"
    lat: Optional[float] = None
    lon: Optional[float] = None
    source: str = ""                  # "fcc", "csv", "radioreference"
    meta: dict = field(default_factory=dict)   # "place", "tone", "ext" ({module: {...}}) go on the Channel

    def to_channel(self) -> Channel:
        return Channel(freq=int(self.freq), mode=self.mode, label=self.label,
                       service=self.service, lat=self.lat, lon=self.lon,
                       source=self.source, city=self.meta.get("place", ""),
                       tone=self.meta.get("tone", ""),
                       ext={k: dict(v) for k, v in (self.meta.get("ext") or {}).items()},
                       tags=[self.service] if self.service else [])


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in kilometers."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2 +
         math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def geofence(items, lat, lon, radius_km, keep_unlocated=False):
    """Filter FoundChannels to those within ``radius_km`` of (lat, lon).

    Items without coordinates are dropped unless ``keep_unlocated`` is True
    (used when a source has no geo data and the user opts to keep everything).
    """
    out = []
    for it in items:
        if it.lat is None or it.lon is None:
            if keep_unlocated:
                out.append(it)
            continue
        if haversine_km(lat, lon, it.lat, it.lon) <= radius_km:
            out.append(it)
    return out
