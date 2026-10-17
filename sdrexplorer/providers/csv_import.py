# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Import frequencies from a user-supplied CSV, geofenced by location.

Column names are matched flexibly (case-insensitive, common aliases), so exports
from RadioReference, Chirp, SDRTrunk, ProScan, etc. mostly work as-is:

  frequency : freq, frequency, freq_mhz, output, rx        (MHz if < 10000, else Hz)
  label     : label, description, name, alpha, alpha_tag
  service   : service, tag, category, type, group
  mode      : mode, modulation
  lat/lon   : lat/latitude, lon/long/longitude

If the CSV has no coordinates, geofencing can't apply; pass keep_unlocated=True
to import everything (the UI offers this as a checkbox).
"""
from __future__ import annotations

import csv
from typing import Optional

from .base import FoundChannel, geofence

_ALIASES = {
    "freq": {"frequency", "freq", "freq_mhz", "frequency_mhz", "output",
             "rx", "rx_freq", "rxfreq"},
    "label": {"label", "description", "name", "alpha", "alpha_tag",
              "alphatag", "channel"},
    "service": {"service", "tag", "category", "type", "group", "svc"},
    "mode": {"mode", "modulation", "demod"},
    "lat": {"lat", "latitude", "y"},
    "lon": {"lon", "long", "longitude", "lng", "x"},
    "city": {"city", "place", "town", "municipality", "locality", "county"},
    "tone": {"tone", "tones", "pl", "ctcss", "dcs", "dpl", "nac", "cc",
             "color code", "colorcode", "squelch", "tone_freq", "ran"},
}

_MODE_MAP = {
    "fm": "NFM", "nfm": "NFM", "fmn": "NFM", "n": "NFM",
    "wfm": "WFM", "fmw": "WFM", "w": "WFM", "broadcast": "WFM",
    "am": "AM", "amn": "AM",
    "usb": "USB", "lsb": "LSB", "cw": "CW", "cw-u": "CW", "cwu": "CW", "cw-l": "CW", "cwl": "CW",
    # digital: preserved as-is (can't demod until Phase 3, but the bank should
    # record what each channel actually is). "P25E"/"P25e" = encrypted P25.
    "p25": "P25", "p25e": "P25", "p25 phase 1": "P25", "p25 phase 2": "P25",
    "dmr": "DMR", "dmre": "DMR", "nxdn": "NXDN", "nxdn48": "NXDN",
    "nxdn96": "NXDN", "nxdne": "NXDN", "ysf": "YSF", "dstar": "DSTAR",
    "d-star": "DSTAR", "tetra": "TETRA",
}


def _pick(header):
    """Map each known field to its column index in this header row."""
    idx = {}
    lower = [h.strip().lower() for h in header]
    for field, names in _ALIASES.items():
        for i, h in enumerate(lower):
            if h in names:
                idx[field] = i
                break
    return idx


def _parse_freq(raw) -> Optional[int]:
    try:
        v = float(str(raw).replace(",", "").strip())
    except ValueError:
        return None
    if v <= 0:
        return None
    return int(round(v)) if v >= 10_000 else int(round(v * 1e6))  # MHz vs Hz


def _norm_mode(raw) -> str:
    return _MODE_MAP.get(str(raw).strip().lower(), "NFM")


def fill_coords(rows, resolver, region="", cache=None, sleep=1.1):
    """Fill lat/lon for rows that lack coordinates but carry a place name
    (meta['place']), by geocoding "<place>, <region>" via ``resolver``.

    ``cache`` is a dict persisted by the caller so we geocode each unique place
    only once (Nominatim is rate-limited). ``sleep`` seconds pause between live
    lookups (skipped on cache hits)."""
    import time
    if cache is None:
        cache = {}
    for r in rows:
        if r.lat is not None:
            continue
        place = (r.meta or {}).get("place", "")
        if not place:
            continue
        key = f"{place}|{region}".lower()
        if key not in cache:
            q = f"{place}, {region}" if region else place
            hit = resolver(q)
            cache[key] = [hit[0], hit[1]] if hit else None
            if hit and sleep:
                time.sleep(sleep)
        c = cache[key]
        if c:
            r.lat, r.lon = c[0], c[1]
    return cache


def load_csv(path, center_lat=None, center_lon=None, radius_km=40.0,
             keep_unlocated=True, default_mode="NFM",
             resolver=None, region="", geocode_cache=None):
    """Return geofenced FoundChannels parsed from ``path``.

    If ``resolver`` is given and a filter center is set, rows that lack lat/lon
    but have a City/place column are geocoded first (so the "Where" field can
    filter city-tagged CSVs, not just ones with explicit coordinates)."""
    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration:
            return []
        idx = _pick(header)
        if "freq" not in idx:
            raise ValueError("CSV has no recognizable frequency column")
        for row in reader:
            if not row or len(row) <= idx["freq"]:
                continue
            hz = _parse_freq(row[idx["freq"]])
            if hz is None:
                continue

            def cell(f):
                i = idx.get(f)
                return row[i].strip() if i is not None and i < len(row) else ""

            lat = lon = None
            try:
                if cell("lat") and cell("lon"):
                    lat, lon = float(cell("lat")), float(cell("lon"))
            except ValueError:
                lat = lon = None
            mode = _norm_mode(cell("mode")) if cell("mode") else default_mode
            place = cell("city")
            meta = {}
            if place:
                meta["place"] = place
            if cell("tone"):
                meta["tone"] = cell("tone")   # "131.8 PL" / "307 NAC" / "CC 10 …"
            rows.append(FoundChannel(
                freq=hz, label=cell("label"), service=cell("service"),
                mode=mode, lat=lat, lon=lon, source="csv", meta=meta))

    if center_lat is None or center_lon is None:
        return rows                                   # no filter -> keep all
    if resolver is not None:
        fill_coords(rows, resolver, region, geocode_cache)
    return geofence(rows, center_lat, center_lon, radius_km,
                    keep_unlocated=keep_unlocated)
