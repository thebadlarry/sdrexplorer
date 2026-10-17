# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""RadioReference provider — OFFICIAL web-service API only, never scraping.

RadioReference gates its curated data behind a paid Premium subscription and an
official SOAP API. This client:

  * reads the user's own credentials from the keystore (api.secrets, owner "core", name
    "radioreference": JSON {username, password, app_key}), set in Settings ▸ RadioReference account…;
    an older config.json "radioreference" section (below) is moved there once and blanked,
  * is fully inert when no credentials are configured (``available()`` → False),
  * uses the `zeep` SOAP library if installed, else explains how to add it.

config.json:
    {
      "radioreference": {
        "username": "you",
        "password": "secret",
        "app_key": "optional-app-key"
      }
    }

We DO NOT scrape radioreference.com HTML. If you don't have a subscription, use
the FCC ULS provider (free, public domain) or CSV import instead.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from .. import paths as _paths
from .base import FoundChannel

WSDL = "http://api.radioreference.com/soap2/?wsdl&v=latest"

def _config_path():
    """RadioReference credentials: config.json in the user's config directory
    (~/.config/sdrexplorer; see sdrexplorer.paths)."""
    return _paths.config_file("config.json")


SECRET_OWNER, SECRET_NAME = "core", "radioreference"


def _migrate_config_json():
    """An account in the old config.json moves into the keystore; the file keeps its other keys."""
    p = _config_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    rr = cfg.get("radioreference") or {}
    if not (rr.get("username") and rr.get("password")):
        return None
    set_creds(rr.get("username", ""), rr.get("password", ""), rr.get("app_key", ""))
    cfg["radioreference"] = {"_moved": "to the keystore (secrets.json); Settings ▸ RadioReference account…"}
    try:
        tmp = p + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass
    return creds()


def creds() -> Optional[dict]:
    """{username, password, app_key} from the keystore, or None."""
    from ..api import secrets
    raw = secrets.get(SECRET_OWNER, SECRET_NAME)
    if raw:
        try:
            cfg = json.loads(raw)
        except ValueError:
            cfg = {}
        if cfg.get("username") and cfg.get("password"):
            return cfg
    return None


def set_creds(username, password, app_key=""):
    """Store the account in the keystore (both blank: forget it)."""
    from ..api import secrets
    if not (username or password):
        secrets.delete(SECRET_OWNER, SECRET_NAME)
        return
    secrets.set(SECRET_OWNER, SECRET_NAME, json.dumps(
        {"username": username, "password": password, "app_key": app_key or ""}))


def _creds() -> Optional[dict]:
    return creds() or _migrate_config_json()


def available() -> bool:
    """True only if the user has configured RadioReference credentials."""
    return _creds() is not None


class RadioReferenceProvider:
    """Thin wrapper over the official RR SOAP API. Constructed only when
    ``available()`` is True."""

    def __init__(self):
        cfg = _creds()
        if cfg is None:
            raise RuntimeError(
                "RadioReference credentials not configured. Add them to "
                f"{_config_path()} (see providers/radioreference.py docstring), or use "
                "the FCC ULS / CSV providers instead.")
        try:
            from zeep import Client
        except ImportError as e:      # pragma: no cover - optional dep
            raise RuntimeError(
                "The RadioReference API needs the 'zeep' SOAP library: "
                "pip install zeep") from e
        self._cfg = cfg
        self._client = Client(WSDL)
        self._auth = {
            "username": cfg["username"],
            "password": cfg["password"],
            "appKey": cfg.get("app_key", ""),
            "version": "latest",
            "style": "rpc",
        }

    def lookup_zip(self, zipcode: str):
        """Return FoundChannels for the county containing ``zipcode``.

        RR flow: zip → county id → categories → subcategories → frequencies.
        Wrapped defensively; on any API error returns an empty list so the UI
        can fall back to another provider.
        """
        try:
            svc = self._client.service
            zinfo = svc.getZipcodeInfo(zipcode, self._auth)
            county_id = zinfo["ctid"]
            cats = svc.getCountyCategories(county_id, self._auth) or []
            out = []
            for cat in cats:
                for sub in (cat.get("subcats") or []):
                    freqs = svc.getSubcatFreqs(sub["scid"], self._auth) or []
                    for f in freqs:
                        hz = int(round(float(f["out"]) * 1e6))
                        out.append(FoundChannel(
                            freq=hz, label=f.get("alpha") or f.get("descr", ""),
                            service=cat.get("cat", ""), mode="NFM",
                            source="radioreference",
                            meta={"tone": f.get("tone", "")}))
            return out
        except Exception:
            return []
