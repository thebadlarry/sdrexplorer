# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The core's optional features: what each needs, and whether it's there.

These stay in the core (they're small, or need only a library), but each switches itself off when its
library is missing. One place answers "what works here and why not", for Settings > Modules and
`--paths`. Probing is cheap: the libraries were already imported (or not) by radio.py / app.py.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Feature:
    name: str
    needs: str
    ok: bool
    why: str = ""


def _radio_sources():
    try:
        from . import paths
        paths.activate("gr_osmosdr")       # the patched build, when install.sh made one
        import osmosdr  # noqa: F401
        return True, ""
    except Exception as e:
        return False, f"gr-osmosdr not importable ({type(e).__name__}): only IQ file replay"


def _webengine():
    try:
        from PyQt5 import QtWebEngineWidgets  # noqa: F401
        return True, ""
    except Exception:
        return False, "PyQtWebEngine not installed (pip install PyQtWebEngine)"


def _radioreference():
    from .providers import radioreference as rr
    try:
        import zeep  # noqa: F401
    except Exception:
        return False, "zeep not installed (pip install zeep)"
    if not rr.available():
        return False, "no account (Settings ▸ RadioReference account…)"
    return True, ""


def probe():
    """[Feature] for every optional core feature."""
    from . import radio as R
    out = []
    ok, why = _radio_sources()
    out.append(Feature("HackRF / RTL-SDR", "gr-osmosdr", ok, why))
    out.append(Feature("RDS (WFM station names)", "gr-rds", R.RDS_AVAILABLE,
                       "" if R.RDS_AVAILABLE else "gr-rds not found (paths key gr_rds)"))
    out.append(Feature("ADS-B", "gr-adsb", R.ADSB_AVAILABLE,
                       "" if R.ADSB_AVAILABLE else "gr-adsb not found (paths key gr_adsb)"))
    ok, why = _webengine()
    out.append(Feature("Live map", "PyQtWebEngine", ok, why))
    ok, why = _radioreference()
    out.append(Feature("RadioReference import", "zeep + credentials", ok, why))
    out.append(Feature("GPU channelizer", "gr-sdrexplorer_gpu (CUDA)", R.CUDA_AVAILABLE,
                       "" if R.CUDA_AVAILABLE else "not built (gr-sdrexplorer_gpu/build) or no CUDA device"))
    return out


def report():
    rows = probe()
    w = max(len(f.name) for f in rows)
    return "\n".join(f"{f.name:<{w}}  {'yes' if f.ok else 'no '}  {f.needs}" + (f"  -- {f.why}" if f.why else "")
                     for f in rows)
