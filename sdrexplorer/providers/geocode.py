# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Resolve a location string to (lat, lon, label).

Accepts:
  * GPS: "34.05,-118.25" or "34.05 -118.25"     (offline, no network)
  * ZIP: "90210"                                 (Nominatim / OpenStreetMap)
  * City/State/address: "Beverly Hills, CA"      (Nominatim / OpenStreetMap)

Nominatim is a free, no-key geocoder. Per its usage policy we send a descriptive
User-Agent and keep lookups occasional (one per Fetch). Network failures return
None so callers can fall back to manual GPS entry.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.parse
import urllib.request
from typing import Optional, Tuple

_ipv4_patched = False


def prefer_ipv4():
    """Make socket.getaddrinfo return IPv4 addresses first (process-wide,
    idempotent). On hosts where IPv6 is advertised but black-holed, urllib
    otherwise stalls ~30 s on the dead IPv6 connect before falling back to
    IPv4 — this makes those lookups (geocoding, FCC downloads) resolve at once.
    IPv6 is still tried as a fallback if IPv4 fails."""
    global _ipv4_patched
    if _ipv4_patched:
        return
    _orig = socket.getaddrinfo

    def _gai(*args, **kwargs):
        res = _orig(*args, **kwargs)
        res.sort(key=lambda a: 0 if a[0] == socket.AF_INET else 1)
        return res

    _gai._orig = _orig
    socket.getaddrinfo = _gai
    _ipv4_patched = True

NOMINATIM = ("https://nominatim.openstreetmap.org/search?q={q}"
             "&format=json&limit=1&countrycodes=us")
_UA = "sdrexplorer/0.1 (HackRF scanner; frequency lookup)"

_GPS_RE = re.compile(
    r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")


def resolve(text: str, timeout: float = 8.0) -> Optional[Tuple[float, float, str]]:
    prefer_ipv4()                    # avoid the ~30 s IPv6-connect stall
    text = (text or "").strip()
    if not text:
        return None

    m = _GPS_RE.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon, f"{lat:.4f},{lon:.4f}"

    return _nominatim(text, timeout)


def _nominatim(query: str, timeout: float) -> Optional[Tuple[float, float, str]]:
    url = NOMINATIM.format(q=urllib.parse.quote(query))
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except Exception:
        return None
    if not data:
        return None
    hit = data[0]
    return float(hit["lat"]), float(hit["lon"]), hit.get("display_name", query)
