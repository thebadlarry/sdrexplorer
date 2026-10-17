# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Channel + bank data model and JSON memory-file persistence.

A *channel* is one saved frequency with its mode, bandwidth, label, and scan
metadata. A *bank* is a named list of channels (e.g. "County Police",
"Discovered 2026-07-17"). The whole set persists to a single JSON memory file
so banks survive restarts — the feature the original WFM GUI lacked entirely.

Pure stdlib (dataclasses + json); no external deps.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

# Service tags used by the scan checkboxes and by the Phase-2 location fetch.
SERVICE_TAGS = [
    "Police", "Fire", "EMS", "Public Works", "Business",
    "Ham", "Aircraft", "Rail", "Marine", "Other",
]

# Modes understood in Phase 1 (analog). Digital modes are added in Phase 3.
ANALOG_MODES = ["NFM", "AM", "WFM", "USB", "LSB", "CW"]

# Reserved bank that collects channels (copied from any bank) to be checked
# periodically during a scan — the classic scanner "priority" feature.
PRIORITY_BANK = "★ Priority"


@dataclass
class Channel:
    freq: int                         # Hz
    mode: str = "NFM"                 # a core mode (NFM / AM / WFM / ...), a module's mode or decodable label
    bw: int = 0                       # channel bandwidth in Hz; 0 = mode default
    label: str = ""
    squelch: float = -70.0            # dB (analog squelch threshold)
    service: str = ""                 # one of SERVICE_TAGS (or free text)
    city: str = ""                    # municipality / place (for text search)
    notes: str = ""                   # free-form user notes (Save to memory)
    tone: str = ""                    # squelch/ID code: "131.8 PL", "307 NAC", …
    offset_hz: int = 0                # repeater TX split, signed (0 = simplex)
    ctcss_hz: float = 0.0             # CTCSS tone, Hz (0 = none)
    ctcss_tx: bool = False            # encode ctcss_hz on transmit
    ctcss_rx: bool = False            # gate RX audio on ctcss_hz (tone squelch)
    tags: list = field(default_factory=list)
    gains: Optional[dict] = None      # {"rf","if","bb"} or None → use global
    lat: Optional[float] = None
    lon: Optional[float] = None
    enabled: bool = True              # include this channel in a scan
    source: str = ""                  # provenance: "manual","discover","fcc",…
    ext: dict = field(default_factory=dict)   # {module name: {...}}: module fields (module API)
    # keys a newer core wrote that this one doesn't know: kept and written back unchanged
    extra: dict = field(default_factory=dict, repr=False, compare=False)

    def fields_for(self, owner: str) -> dict:
        """The fields module ``owner`` stored on this channel ({} if none)."""
        f = (self.ext or {}).get(owner)
        return dict(f) if isinstance(f, dict) else {}

    def as_dict(self) -> dict:
        d = asdict(self)
        extra = d.pop("extra") or {}
        if not d["ext"]:
            del d["ext"]
        return {**extra, **d}

    @classmethod
    def from_dict(cls, c: dict) -> "Channel":
        known = cls.__dataclass_fields__
        fields = {k: v for k, v in c.items() if k in known and k != "extra"}
        if not isinstance(fields.get("ext", {}), dict):
            fields["ext"] = {}
        return cls(**fields, extra={k: v for k, v in c.items() if k not in known})

    def display(self) -> str:
        lbl = self.label or "(unlabeled)"
        return f"{self.freq/1e6:10.4f} MHz  {self.mode:<4}  {lbl}"


# "<value> <KIND>" (e.g. "131.8 PL", "023 DPL", "307 NAC", "10 RAN") — the
# RadioReference tone/ID column. Color codes are written kind-first: "CC 10 TG 100 SL 1".
_TONE_RE = re.compile(r'^\s*([0-9A-Fa-f.]+)\s*(PL|DPL|DCS|NAC|RAN|CC)\b', re.I)
_CC_RE = re.compile(r'\bCC\s*(\d+)', re.I)
_TG_RE = re.compile(r'\bTG\s*(\d+)', re.I)


def parse_tone(s):
    """Parse a tone/ID string into ``{"kind", "value", "raw"}`` or None.

    kind ∈ {PL, DPL, NAC, RAN, CC}; value is a float Hz (PL/CTCSS), a
    zero-padded code string (DPL/DCS), or an int (NAC is hex, RAN/CC decimal).
    These are what tell co-channel stations apart (the same frequency reused by
    different agencies), so decoding them lets us label the RIGHT station."""
    if not s:
        return None
    s = str(s).strip()
    m = _TONE_RE.match(s)
    if m:
        val, kind = m.group(1), m.group(2).upper()
        kind = "DPL" if kind == "DCS" else kind
        try:
            if kind == "PL":
                v = float(val)
            elif kind == "DPL":
                v = val.zfill(3)
            elif kind == "NAC":              # network access code — hex
                v = int(val, 16)
            else:                            # RAN / CC — decimal
                v = int(val)
        except ValueError:
            return None
        return {"kind": kind, "value": v, "raw": s}
    cc = _CC_RE.search(s)                    # "CC 10 TG 100 SL 1"
    if cc:
        out = {"kind": "CC", "value": int(cc.group(1)), "raw": s}
        tg = _TG_RE.search(s)
        if tg:
            out["tgid"] = int(tg.group(1))
        return out
    return None


@dataclass
class Bank:
    name: str
    channels: list = field(default_factory=list)   # list[Channel]

    def add(self, ch: Channel) -> None:
        self.channels.append(ch)

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.channels):
            del self.channels[index]


class BankStore:
    """Owns all banks and the memory file. Load once, mutate, save."""

    def __init__(self, path: str):
        self.path = path
        self.banks: dict[str, Bank] = {}
        self.active: Optional[str] = None
        self.load()
        self.banks.setdefault(PRIORITY_BANK, Bank(PRIORITY_BANK))  # always present

    def priority(self) -> Bank:
        """The reserved priority bank (auto-created)."""
        return self.banks.setdefault(PRIORITY_BANK, Bank(PRIORITY_BANK))

    # -- persistence ---------------------------------------------------------
    def load(self) -> None:
        if not os.path.exists(self.path):
            # seed with an empty default bank so the UI has something to show
            self.banks = {"Default": Bank("Default")}
            self.active = "Default"
            return
        try:
            with open(self.path) as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            self.banks = {"Default": Bank("Default")}
            self.active = "Default"
            return
        self.banks = {}
        for name, chans in raw.get("banks", {}).items():
            bank = Bank(name)
            for c in chans:
                # tolerate unknown/missing keys across versions (unknown ones are kept)
                bank.channels.append(Channel.from_dict(c))
            self.banks[name] = bank
        if not self.banks:
            self.banks = {"Default": Bank("Default")}
        self.active = raw.get("active") or next(iter(self.banks))
        if self.active not in self.banks:
            self.active = next(iter(self.banks))

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        data = {
            "active": self.active,
            "banks": {name: [ch.as_dict() for ch in bank.channels]
                      for name, bank in self.banks.items()},
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.path)   # atomic

    # -- bank management -----------------------------------------------------
    def bank(self, name: Optional[str] = None) -> Bank:
        return self.banks[name or self.active]

    def create_bank(self, name: str) -> Bank:
        if name not in self.banks:
            self.banks[name] = Bank(name)
        return self.banks[name]

    def delete_bank(self, name: str) -> None:
        if name in self.banks and len(self.banks) > 1:
            del self.banks[name]
            if self.active == name:
                self.active = next(iter(self.banks))

    def set_active(self, name: str) -> None:
        if name in self.banks:
            self.active = name

    def add_channel(self, ch: Channel, bank_name: Optional[str] = None) -> None:
        self.bank(bank_name).add(ch)
