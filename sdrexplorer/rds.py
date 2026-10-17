# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""RDS (Radio Data System) station-metadata accumulation for WFM broadcast.

This module does NOT do block sync, CRC error
correction, or group parsing itself -- that's all handled by the gr-rds
out-of-tree GNU Radio module (radio.py builds the 57 kHz subcarrier
extraction / carrier recovery / symbol sync front end from stock GNU Radio
blocks, then hands the sliced bitstream to gr-rds's ``rds.decoder`` +
``rds.parser`` blocks). gr-rds's parser publishes one ``(type, msg)`` update
per decoded field (PI, PS, PTY, flags, RadioText, clock, AF); this module's
job is just to merge that update stream into "what's the current station"
state, deduping repeats.

Kept GNU-Radio/Qt-free so it's directly
unit-testable and importable without a running flowgraph.
"""
from __future__ import annotations

# gr-rds parser message types (rds.parser's "out" port publishes
# pmt::make_tuple(type, msg) -- see lib/parser_impl.cc in bastibl/gr-rds).
RDS_FIELD_TYPES = {
    0: "pi",        # Program Identification (hex string)
    1: "ps",        # Program Service name (8-char station name)
    2: "pty",       # Program Type (description string)
    3: "flags",     # TP/TA/MuSp/MoSt/AH/CMP/stPTY, as a 7-char '0'/'1' string
    4: "rt",        # RadioText
    5: "clock",     # Clock time (formatted date/time string)
    6: "af",        # Alternative Frequencies (formatted frequency list)
}


class RdsState:
    """Merges gr-rds parser (type, msg) updates into the current station's
    field dict. ``update()`` returns the merged dict when a field's value
    actually changes, or None for an unknown type / unchanged repeat (RDS
    groups repeat constantly, so most updates are dedup no-ops)."""

    def __init__(self):
        self._fields = {}

    def update(self, type_id, msg):
        field = RDS_FIELD_TYPES.get(int(type_id))
        if field is None:
            return None
        msg = str(msg)
        if self._fields.get(field) == msg:
            return None
        self._fields[field] = msg
        return dict(self._fields)

    def reset(self):
        """Clear accumulated state (call on retune -- a new station's fields
        shouldn't be mixed with the previous one's)."""
        self._fields = {}
