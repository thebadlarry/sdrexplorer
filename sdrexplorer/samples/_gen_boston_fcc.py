#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Build boston_fcc_sample.csv: a small sample channel list for Memories ▸ Import ▸ CSV file.

Made from the FCC ULS Land Mobile data (public domain) that sdrexplorer downloads (Memories ▸ Download FCC
data, LMpriv and / or LMcomm): active licenses within RADIUS_KM of Boston City Hall, a few of each emission
type, spread across the frequency range. It shows what an import looks like; a license says what may be on
the air, not what is.

    python3 -m sdrexplorer.samples._gen_boston_fcc [uls_dir]      (default: the paths key uls_dir)
"""
from __future__ import annotations

import csv
import os
import sys

from sdrexplorer import paths
from sdrexplorer.providers import fcc_uls

CENTER = (42.3601, -71.0589)          # Boston City Hall
RADIUS_KM = 25.0
PER_TYPE = 20                          # channels per emission type
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boston_fcc_sample.csv")

# emission filter (fcc_uls.EMISSION_FILTERS) -> the mode written to the CSV (csv_import maps it)
TYPES = [("NFM voice (12.5 kHz)", "NFM"), ("FM voice (25 kHz)", "NFM"), ("P25 Phase 1", "P25"),
         ("DMR", "DMR"), ("NXDN48 / dPMR", "NXDN"), ("NXDN96", "NXDN"),
         ("FM analog + digital (trunked?)", "NFM")]

READABLE = set(fcc_uls.SERVICE_CODE_MAP.values())


def service(s):
    """The provider's readable service, or "Other" for a radio service code it doesn't name."""
    return s if s in READABLE else "Other"


def city(place):
    """"BOSTON, MA" / "Boston, Ma" -> "Boston, MA"."""
    town, _, state = (place or "").rpartition(",")
    return f"{town.strip().title()}, {state.strip().upper()}" if town else (place or "").title()


def pick(rows, n):
    """n rows spread evenly over the (frequency-sorted) list, one per frequency."""
    seen, uniq = set(), []
    for r in rows:
        if r.freq not in seen:
            seen.add(r.freq)
            uniq.append(r)
    if len(uniq) <= n:
        return uniq
    return [uniq[round(i * (len(uniq) - 1) / (n - 1))] for i in range(n)]


def main(argv):
    uls = argv[1] if len(argv) > 1 else paths.get("uls_dir")
    if not fcc_uls.has_data(uls):
        sys.exit(f"no FCC ULS data in {uls}: download it first (Memories ▸ Download FCC data)")
    if not fcc_uls.index_fresh(uls):
        fcc_uls.build_index(uls)
    out, used = [], set()
    for emission, mode in TYPES:
        rows, _total = fcc_uls.search(uls, lat=CENTER[0], lon=CENTER[1], radius_km=RADIUS_KM,
                                      emission=emission, limit=100_000)
        rows = [r for r in rows if r.freq not in used]
        for r in pick(rows, PER_TYPE):
            used.add(r.freq)
            out.append((r, mode))
    out.sort(key=lambda x: x[0].freq)
    with open(OUT, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Frequency", "Mode", "Service", "Label", "City", "Latitude", "Longitude", "License",
                    "Emission"])
        for r, mode in out:
            m = r.meta
            w.writerow([f"{r.freq / 1e6:.5f}", mode, service(r.service), (m.get("name") or m.get("call"))[:60],
                        city(m.get("place")), f"{r.lat:.4f}", f"{r.lon:.4f}", m.get("call"),
                        m.get("kind") or m.get("emission")])
    print(f"{len(out)} channels -> {OUT}")


if __name__ == "__main__":
    main(sys.argv)
