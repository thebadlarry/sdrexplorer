# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Records modules hand to the core: decode events, positions, annotations.

The core treats every string in them as opaque: ``protocol`` / ``label`` names, ``ids`` keys, ``source``.
It shows them, counts them and passes them on; it never interprets them.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Decode:
    """One decode event, for the decode log, the payload pane, recording labels and other modules.

    source     the publishing module's name
    protocol   what was decoded (a label string, e.g. "MODE-1200"): a recording's label is the dominant one
    text       the decode-log line (the core adds the time and the station tag)
    ids        system / unit identities ({"RIC": 1234567}); tallied into a recording's sidecar
    fields     anything else, for other modules
    payload    raw bytes (optional); ``linktype`` (pcap LINKTYPE_*) makes them pcap-exportable
    files      paths of files the decoder produced (images, data)
    dedup_key  the log's repeat filter: a record with the same key as the last one within
               ``dedup_s`` isn't logged again (it still counts for recordings and reaches subscribers)
    counts     False: not tallied into a recording's label (for noise-prone lines)
    log        False: not written to the decode log (still counted and published)
    freq_hz    the frequency it was decoded on (default: the tuned frequency)
    ref        ties records of one event together (any hashable): a burst's log lines and its payload. The
               decode strip's "Follow log filter" shows the payloads of the log lines a filter matches; a
               record carrying both a log line and a payload is linked without one
    ext        other modules' additions, {module: {...}}
    """
    source: str
    protocol: str
    text: str = ""
    ids: dict = field(default_factory=dict)
    fields: dict = field(default_factory=dict)
    payload: Optional[bytes] = None
    linktype: Optional[int] = None
    files: list = field(default_factory=list)
    dedup_key: Any = None
    dedup_s: float = 30.0
    counts: bool = True
    log: bool = True
    freq_hz: Optional[int] = None
    t: float = field(default_factory=time.time)
    ref: Any = None
    ext: dict = field(default_factory=dict)


@dataclass
class Position:
    """A located radio / aircraft / station for the map. ``kind`` picks the marker style."""
    id: str
    kind: str
    lat: float
    lon: float
    label: str = ""
    source: str = ""
    attrs: dict = field(default_factory=dict)
    ext: dict = field(default_factory=dict)


@dataclass
class Annotation:
    """A labeled signal on the waterfall (ui.annotate). The core draws ``label`` as a tag beside the
    signal: "✓" when ``confirmed``, "?" when ``confidence`` is below 0.7; when the signal stops, its tag
    stays on the waterfall row it was last seen on (``trail``). ``raw`` is the source's own label (what
    request_decode / Label as... use), ``label`` what is shown."""
    freq_hz: float
    bw_hz: float
    label: str
    confidence: float = 1.0
    confirmed: bool = False
    detail: str = ""
    source: str = ""
    raw: str = ""
    snr_db: float = 0.0
    trail: bool = True
    ext: dict = field(default_factory=dict)

    def as_dict(self):
        """The dict form the core's waterfall code uses."""
        d = {"freq_hz": int(round(self.freq_hz)), "bw_hz": float(self.bw_hz), "snr_db": float(self.snr_db),
             "label": self.label, "raw_label": self.raw or self.label, "conf": float(self.confidence),
             "confirmed": bool(self.confirmed), "source": self.source, "detail": self.detail,
             "trail": bool(self.trail)}
        d.update(self.ext)
        return d

    @classmethod
    def from_dict(cls, d, source=""):
        known = {"freq_hz", "bw_hz", "snr_db", "label", "raw_label", "conf", "confirmed", "source", "detail",
                 "trail"}
        return cls(freq_hz=d["freq_hz"], bw_hz=d.get("bw_hz", 0.0), label=d.get("label", ""),
                   confidence=d.get("conf", 1.0), confirmed=bool(d.get("confirmed")), detail=d.get("detail", ""),
                   source=d.get("source") or source, raw=d.get("raw_label", ""), snr_db=d.get("snr_db", 0.0),
                   trail=d.get("trail", True), ext={k: v for k, v in d.items() if k not in known})
