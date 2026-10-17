# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The station (module API): where the receiver is, and what time a sample was received.

    from sdrexplorer.api import station

    station.location()           # Location(lat, lon, alt_m, name) or None (not set)
    station.now()                # wall-clock time of what is being received now: time.time() live; the
                                 # replayed file's own time (its start + the playback position) in replay
    station.sample_time(i)       # the time of sample i of the current source (live: counted from the
                                 # radio's start, so only as good as the host clock and the source's rate)
    station.replaying()          # True while an IQ file plays

The location is set by the user (Settings ▸ Station location…), saved in config/station.json, and announced
on the "station_changed" topic. A recording's sidecar carries "start_time" (epoch seconds of its first
sample); older files get theirs from the YYYYMMDD_HHMMSS stamp in their name.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Optional

from .. import paths

FILE = "station.json"


@dataclass
class Location:
    lat: float
    lon: float
    alt_m: float = 0.0
    name: str = ""                     # what the user typed ("Boston, MA"), for display


_clock = {"radio": None}               # bound by the app (bind_radio)
_listeners = []                        # fn(location) -- the app forwards these onto the bus


def _path():
    return paths.config_file(FILE)


def location() -> Optional[Location]:
    try:
        with open(_path()) as fh:
            d = json.load(fh)
        return Location(float(d["lat"]), float(d["lon"]), float(d.get("alt_m", 0.0)), str(d.get("name", "")))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def set_location(lat, lon, alt_m=0.0, name=""):
    """Save the station location (lat/lon in degrees). ``lat=None`` clears it."""
    p = _path()
    if lat is None:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass
        loc = None
    else:
        lat, lon = float(lat), float(lon)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"not a position: {lat}, {lon}")
        loc = Location(lat, lon, float(alt_m or 0.0), str(name or ""))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w") as fh:
            json.dump(asdict(loc), fh, indent=1)
        os.replace(p + ".tmp", p)
    for fn in list(_listeners):
        fn(loc)
    return loc


def on_change(fn):
    _listeners.append(fn)


def bind_radio(radio):
    """The app's radio: the clock for now() / sample_time()."""
    _clock["radio"] = radio


def replaying():
    r = _clock["radio"]
    return bool(r is not None and r.running and r.is_replay())


def file_start_time(meta, path=None):
    """Epoch seconds of a recording's first sample: its sidecar's "start_time", else the stamp in its name."""
    t = (meta or {}).get("start_time")
    if isinstance(t, (int, float)):
        return float(t)
    from ..recindex import recording_time
    dt = recording_time(os.path.basename(path or (meta or {}).get("file", "") or ""))
    return dt.timestamp() if dt is not None else None


def _replay_start():
    r = _clock["radio"]
    rp = (r.replay or {}) if r is not None else {}
    return file_start_time(rp.get("meta") or {}, rp.get("path"))


def now():
    if replaying():
        r = _clock["radio"]
        t0, pos = _replay_start(), r.replay_position_s()
        if t0 is not None and pos is not None:
            return t0 + pos
    return time.time()


def sample_time(index):
    """Time of sample ``index`` of the current source (capture rate). Replay: the file's start + index / rate.
    Live: the radio's start time + index / rate. None when nothing runs."""
    r = _clock["radio"]
    if r is None or not r.running:
        return None
    rate = float(r.samp_rate)
    if r.is_replay():
        rp = r.replay or {}
        rate = float((rp.get("meta") or {}).get("samp_rate", rate)) or rate
        t0 = _replay_start()
    else:
        t0 = getattr(r, "started_at", None)
    return None if t0 is None else t0 + float(index) / rate
