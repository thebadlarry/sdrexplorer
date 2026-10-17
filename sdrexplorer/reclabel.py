# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Decoder-derived labels for IQ recordings.

While a recording runs, every decode event the app receives (RDS fields, and every
module's api.Decode records) is tallied here, and the summary is written into the
capture's .json sidecar under "decoded". That turns every capture of a decodable
signal into a labeled training example, with no manual tagging:

    "decoded": {
      "freq_hz": 463562500,          # what the decoders were tuned to
      "label": "MODE-A",             # dominant protocol, or null if none/too few
      "label_frac": 0.97,            # its share of all decoded frames
      "protocols": {
        "MODE-A": {"frames": 212, "first_s": 0.4, "last_s": 13.9,
                   "source": "<module>", "<id>": [1], ...}
      }
    }

Counts are per decode event BEFORE the log de-duplication in app.py, so a
control channel re-sending the same broadcast every second still counts as
many frames (that repetition is exactly the evidence the signal is there).
A protocol needs MIN_FRAMES to become the label -- decoders occasionally fire
once on noise.
"""
from __future__ import annotations

import re
import time

MIN_FRAMES = 5          # decoded frames needed before a protocol becomes the label
MAX_VALUES = 16         # cap on distinct values kept per identity field



class RecordingLabels:
    """Accumulates decoder evidence for one recording. Not thread-safe; feed it
    from the Qt main thread (all the app's decode slots run there)."""

    def __init__(self, freq_hz):
        self.freq_hz = int(freq_hz)
        self._t0 = time.monotonic()
        self._p = {}                      # proto -> {"frames", "first_s", "last_s", fields...}

    # -- feeding ---------------------------------------------------------------
    def _hit(self, proto, source, **fields):
        now = round(time.monotonic() - self._t0, 2)
        e = self._p.get(proto)
        if e is None:
            e = self._p[proto] = {"frames": 0, "first_s": now, "last_s": now,
                                  "source": source}
        e["frames"] += 1
        e["last_s"] = now
        for k, v in fields.items():
            if v is None or v == "":
                continue
            vals = e.setdefault(k, [])
            if v not in vals and len(vals) < MAX_VALUES:
                vals.append(v)

    def feed_decode(self, record):
        """An api.Decode from a module: counted under its protocol, with its ids as the identity
        fields (lists of distinct values in the sidecar, as for the built-in decoders)."""
        if not record.counts or not record.protocol:
            return
        self._hit(record.protocol, record.source, **dict(record.ids))

    def feed_rds(self, d):
        self._hit("WFM-RDS", "gr-rds", pi=d.get("pi"),
                  ps=(d.get("ps") or "").strip() or None)

    # -- output ----------------------------------------------------------------
    def summary(self):
        total = sum(e["frames"] for e in self._p.values())
        label, frac = None, None
        if total:
            best = max(self._p, key=lambda k: self._p[k]["frames"])
            if self._p[best]["frames"] >= MIN_FRAMES:
                label, frac = best, round(self._p[best]["frames"] / total, 3)
        return {"freq_hz": self.freq_hz, "label": label, "label_frac": frac,
                "protocols": {k: dict(v) for k, v in self._p.items()}}
