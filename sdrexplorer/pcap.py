# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""pcap export of captured payloads (the decode strip's Export .pcap...).

A payload carries its pcap LINKTYPE_* (api.Decode.linktype); one file holds one link type, so a capture
with several is written as one file per type (the extra ones get the type in their name).
"""
from __future__ import annotations

import os
import struct

LINKTYPE_RAW = 101          # raw IPv4/IPv6, no link header


def _header(linktype):
    return struct.pack("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, int(linktype))


def write_pcap(path, frames, linktype=LINKTYPE_RAW):
    """Write ``frames`` (iterable of ``(epoch_ms, raw_bytes)``) to a .pcap of ``linktype``. Returns the count."""
    n = 0
    with open(path, "wb") as fh:
        fh.write(_header(linktype))
        for ts_ms, raw in frames:
            sec = int(ts_ms // 1000)
            usec = int((ts_ms % 1000) * 1000)
            fh.write(struct.pack("<IIII", sec, usec, len(raw), len(raw)))
            fh.write(bytes(raw))
            n += 1
    return n


def write_by_linktype(path, entries):
    """``entries``: dicts with "ts" (epoch ms), "raw" (bytes) and "linktype" (None: not exportable).
    Returns [(path, linktype, count)] of the files written: ``path`` for the most common type, and
    ``<path stem>_lt<type>.pcap`` for each other one."""
    by = {}
    for e in entries:
        lt = e.get("linktype")
        if lt is not None:
            by.setdefault(int(lt), []).append((e.get("ts", 0), e["raw"]))
    out = []
    stem, ext_ = os.path.splitext(path)
    for i, (lt, frames) in enumerate(sorted(by.items(), key=lambda kv: -len(kv[1]))):
        p = path if i == 0 else f"{stem}_lt{lt}{ext_ or '.pcap'}"
        out.append((p, lt, write_pcap(p, frames, lt)))
    return out
