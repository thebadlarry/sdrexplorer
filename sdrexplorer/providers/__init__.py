# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Pluggable frequency-data providers.

All providers return lists of ``FoundChannel`` (see base.py) which the UI turns
into bank ``Channel`` records. Sources:

  * geocode      — location string (GPS / zip / city,state) -> lat/lon
  * fcc_uls      — FCC ULS public bulk data, geofenced (free, public domain)
  * csv_import   — user CSV, geofenced (bring-your-own data, e.g. RR exports)
  * radioreference — official RR web-service API (needs the user's own creds)

No site scraping — RadioReference is only ever touched through its official API.
"""
