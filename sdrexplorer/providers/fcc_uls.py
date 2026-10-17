# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""FCC ULS public bulk-data provider (free, public domain).

The FCC publishes the Universal Licensing System as weekly pipe-delimited bulk
dumps. Land-mobile frequencies + transmitter coordinates live across four record
types, joined on the Unique System Identifier (USI, field 2 in every record):

  HD.dat  call sign, license status, radio service code
  EN.dat  entity (licensee) name
  FR.dat  frequency assigned (MHz)
  LO.dat  transmitter latitude/longitude (degrees-minutes-seconds)

``parse_uls_dir`` joins them into FoundChannels; ``query`` geofences by radius.
``download_dataset`` fetches + unzips a weekly "complete" file (``l_*.zip``;
the daily ``a_*.zip`` files are only that day's changes). Real dumps are large
(LMcomm ~80 MB, LMpriv ~420 MB zipped) and updated weekly (Sunday), so the
intended flow is: download once, then query the cached directory — not
download-per-fetch.

Honesty note: ULS radio-service codes group public safety together; they do NOT
reliably separate Police vs Fire vs EMS (that granularity is what curated
sources like RadioReference add). We map to broad buckets and tag the rest with
the raw service code for the user to refine.
"""
from __future__ import annotations

import json
import math
import os
import socket
import sqlite3
import tempfile
import urllib.error
import urllib.request
import zipfile
from typing import Optional

from .base import FoundChannel, geofence, haversine_km

# 0-indexed field positions per the FCC ULS public-access record format.
_HD_CALL, _HD_STATUS, _HD_SERVICE = 4, 5, 6
_EN_TYPE, _EN_NAME = 5, 7
_FR_FREQ = 10
_LO = dict(lat_d=19, lat_m=20, lat_s=21, lat_dir=22,
           lon_d=23, lon_m=24, lon_s=25, lon_dir=26,
           city=12, county=13, state=14)

# broad radio-service-code -> friendly bucket (not exhaustive)
SERVICE_CODE_MAP = {
    "PW": "Public Safety", "PX": "Public Safety", "QA": "Public Safety",
    "SG": "Public Safety", "SL": "Public Safety",
    "GE": "Public Safety", "YE": "Public Safety",          # 800 MHz public safety, conventional / trunked
    "GJ": "Business", "GU": "Business",                   # 800 MHz industrial / business, conventional
    "YJ": "Business", "YU": "Business",                   # 800 MHz industrial / business, trunked
    "IG": "Business", "YG": "Business", "GB": "Business", "GX": "Business",
    "YW": "Business", "IK": "Business",
    "HA": "Ham", "HV": "Ham",
    "MC": "Marine", "MG": "Marine",
    "RR": "Rail", "RS": "Rail",
}

# weekly bulk files (land mobile). See https://www.fcc.gov/uls/transactions/daily-weekly
DATASETS = {
    "LMpriv": "https://data.fcc.gov/download/pub/uls/complete/l_LMpriv.zip",
    "LMcomm": "https://data.fcc.gov/download/pub/uls/complete/l_LMcomm.zip",
    "LMbcast": "https://data.fcc.gov/download/pub/uls/complete/l_LMbcast.zip",
}


def _rows(path):
    """Yield split pipe-delimited fields from a .dat file, if it exists."""
    if not os.path.exists(path):
        return
    with open(path, encoding="latin-1", errors="replace") as fh:
        for line in fh:
            yield line.rstrip("\n").split("|")


def _dms(deg, minutes, sec, direction):
    try:
        d = abs(float(deg)) + float(minutes) / 60.0 + float(sec) / 3600.0
    except (ValueError, TypeError):
        return None
    if str(direction).strip().upper() in ("S", "W"):
        d = -d
    return round(d, 6)


def _service(code):
    code = (code or "").strip().upper()
    return SERVICE_CODE_MAP.get(code, code or "Land Mobile")


def parse_uls_dir(path, active_only=True):
    """Join HD/EN/FR/LO .dat files in ``path`` into FoundChannels."""
    out = []
    for usi, hz, call, service, name, place, lat, lon in _iter_channels(
            path, active_only):
        label = " · ".join(x for x in (name, place) if x) or call
        out.append(FoundChannel(freq=hz, label=label[:80], service=service,
                                mode="NFM", lat=lat, lon=lon, source="fcc",
                                meta={"call": call, "usi": usi}))
    return out


def _iter_channels(path, active_only=True):
    """Yield (usi, hz, call, service, name, place, lat, lon) per licensed
    frequency, joining HD/EN/FR/LO (lat/lon None when LO has no coordinates)."""
    # HD: USI -> (call, service, status)
    hd = {}
    for r in _rows(os.path.join(path, "HD.dat")):
        if len(r) <= _HD_SERVICE or r[0] != "HD":
            continue
        usi = r[1]
        status = r[_HD_STATUS].strip().upper()
        if active_only and status not in ("A", ""):
            continue
        hd[usi] = (r[_HD_CALL].strip(), _service(r[_HD_SERVICE]))

    # EN: USI -> licensee name (prefer entity_type L)
    en = {}
    for r in _rows(os.path.join(path, "EN.dat")):
        if len(r) <= _EN_NAME or r[0] != "EN":
            continue
        usi, name = r[1], r[_EN_NAME].strip()
        if name and (usi not in en or r[_EN_TYPE].strip().upper() == "L"):
            en[usi] = name

    # LO: USI -> (lat, lon, place) from the first location with valid coords
    lo = {}
    for r in _rows(os.path.join(path, "LO.dat")):
        if len(r) <= _LO["lon_dir"] or r[0] != "LO":
            continue
        usi = r[1]
        if usi in lo:
            continue
        lat = _dms(r[_LO["lat_d"]], r[_LO["lat_m"]], r[_LO["lat_s"]],
                   r[_LO["lat_dir"]])
        lon = _dms(r[_LO["lon_d"]], r[_LO["lon_m"]], r[_LO["lon_s"]],
                   r[_LO["lon_dir"]])
        if lat is None or lon is None or (lat == 0 and lon == 0):
            continue
        place = ", ".join(x for x in (r[_LO["city"]].strip(),
                                      r[_LO["state"]].strip()) if x)
        lo[usi] = (lat, lon, place)

    # FR: one channel per (USI, frequency)
    seen = set()
    for r in _rows(os.path.join(path, "FR.dat")):
        if len(r) <= _FR_FREQ or r[0] != "FR":
            continue
        usi = r[1]
        if usi not in hd:
            continue
        try:
            mhz = float(r[_FR_FREQ])
        except ValueError:
            continue
        if mhz <= 0:
            continue
        hz = int(round(mhz * 1e6))
        key = (usi, hz)
        if key in seen:
            continue
        seen.add(key)
        call, service = hd[usi]
        lat = lon = None
        place = ""
        if usi in lo:
            lat, lon, place = lo[usi]
        yield usi, hz, call, service, en.get(usi, call), place, lat, lon


# Emission designator -> what it usually is on land mobile. The designator only
# gives bandwidth + modulation class, so these are the conventional readings
# (FCC / APCO / ETSI / NXDN Forum usage), not proof of the protocol: 8K10F1E is
# what P25 Phase 1 is licensed as, but any 8.1 kHz digital-FM voice would be too.
# Keyed by the first 6 characters (bandwidth + modulation + signal type) where
# the information letter (E voice, D data, W both) doesn't change the name.
_EMISSION_NAMES = {
    # analog FM voice: 25 kHz ("wide", ±5 kHz) vs 12.5 kHz ("narrow", ±2.5 kHz)
    "20K0F3": "FM voice (25 kHz)", "16K0F3": "FM voice (25 kHz)",
    "15K0F3": "FM voice (25 kHz)", "13K6F3": "FM voice (25 kHz)",
    "11K3F3": "NFM voice (12.5 kHz)", "11K2F3": "NFM voice (12.5 kHz)",
    "11K0F3": "NFM voice (12.5 kHz)", "10K0F3": "NFM voice (12.5 kHz)",
    "6K00F3": "NFM voice (6.25 kHz)",
    # tone-signaling data on FM (MDC1200, FleetSync, DTMF/5-tone)
    "20K0F2": "FM tone data (25 kHz)", "16K0F2": "FM tone data (25 kHz)",
    "15K0F2": "FM tone data (25 kHz)", "13K6F2": "FM tone data (25 kHz)",
    "11K2F2": "FM tone data (12.5 kHz)", "11K0F2": "FM tone data (12.5 kHz)",
    "6K00F2": "FM tone data (6.25 kHz)",
    # P25 (C4FM / LSM / H-DQPSK)
    "8K10F1": "P25 Phase 1", "8K10F7": "P25 Phase 1",
    "8K70D1": "P25 Phase 1 (LSM simulcast)", "9K80D7": "P25 Phase 2 (TDMA)",
    # DMR (MotoTRBO etc.)
    "7K60FX": "DMR", "7K60F7": "DMR (likely)", "7K60F1": "DMR (likely)",
    # NXDN / IDAS (4K00 is also dPMR)
    "4K00F1": "NXDN48 / dPMR (6.25 kHz)", "4K00F7": "NXDN48 (6.25 kHz)",
    "4K00F2": "NXDN48 / dPMR data (6.25 kHz)",
    "8K30F1": "NXDN96 (12.5 kHz)", "8K30F7": "NXDN96 (12.5 kHz)",
    # mixed analog/digital: typically an analog trunked system with a data
    # control channel (SmartNet / EDACS) or analog voice with digital signaling
    "20K0F9": "FM analog + digital (25 kHz, trunked?)",
    "14K0F9": "FM analog + digital (25 kHz, trunked?)",
    "11K2F9": "FM analog + digital (12.5 kHz)", "10K0F9": "FM analog + digital (12.5 kHz)",
    "2K80J3": "SSB voice", "3K00J3": "SSB voice", "6K00A3": "AM voice",
    "100HN0": "unmodulated carrier",
}

# search filter: type -> designator prefixes (bandwidth + modulation + signal)
EMISSION_FILTERS = {
    "FM voice (25 kHz)": ["20K0F3", "16K0F3", "15K0F3", "13K6F3"],
    "NFM voice (12.5 kHz)": ["11K3F3", "11K2F3", "11K0F3", "10K0F3", "6K00F3"],
    "P25 Phase 1": ["8K10F1", "8K10F7", "8K70D1"],
    "P25 Phase 2": ["9K80D7"],
    "DMR": ["7K60FX", "7K60F7", "7K60F1"],
    "NXDN48 / dPMR": ["4K00F1", "4K00F7", "4K00F2"],
    "NXDN96": ["8K30F1", "8K30F7"],
    "FM analog + digital (trunked?)": ["20K0F9", "14K0F9", "11K2F9", "10K0F9"],
}

# ITU emission designator letters, for designators not named above
_ITU_MOD = {"N": "carrier", "A": "AM", "H": "SSB full carrier", "R": "SSB reduced carrier",
            "J": "SSB", "B": "ISB", "C": "VSB", "F": "FM", "G": "PM", "D": "linear (AM+PM)",
            "P": "pulse", "K": "pulse", "L": "pulse", "M": "pulse", "Q": "pulse",
            "V": "pulse", "W": "combined", "X": "other"}
_ITU_SIG = {"0": "no modulation", "1": "digital", "2": "digital on tone", "3": "analog",
            "7": "multi-channel digital", "8": "multi-channel analog",
            "9": "analog+digital", "X": "other"}
_ITU_INFO = {"N": "", "A": "telegraphy", "B": "telegraphy", "C": "fax", "D": "data",
             "E": "voice", "F": "video", "W": "voice+data", "X": "other"}


def _bandwidth_hz(bw):
    """'11K2' -> 11200, '100H' -> 100, '1M25' -> 1250000 (None if not that form)."""
    for i, ch in enumerate(bw):
        if ch in "HKMG":
            try:
                whole = float(bw[:i] or "0")
                frac = float("0." + bw[i + 1:]) if bw[i + 1:] else 0.0
            except ValueError:
                return None
            return (whole + frac) * {"H": 1, "K": 1e3, "M": 1e6, "G": 1e9}[ch]
    return None


def emission_name(designator):
    """Readable name for one emission designator: "16K0F3E" -> "FM (wide, 25 kHz)",
    "8K10F1D" -> "P25 Phase 1". Unknown ones are decoded from the ITU letters
    ("12K5F1D" -> "FM digital data, 12.5 kHz"). Returns "" for an empty or
    malformed designator."""
    d = (designator or "").strip().upper()
    if len(d) < 7:
        return ""
    name = _EMISSION_NAMES.get(d[:6])
    if name:
        return name
    bw, mod, sig, info = d[:4], d[4], d[5], d[6]
    hz = _bandwidth_hz(bw)
    if hz is None or mod not in _ITU_MOD:
        return ""
    parts = [_ITU_MOD[mod], _ITU_SIG.get(sig, ""), _ITU_INFO.get(info, "")]
    if sig == "0" and mod == "N":
        parts = ["unmodulated carrier"]
    text = " ".join(p for p in parts if p)
    width = (f"{hz / 1e6:g} MHz" if hz >= 1e6 else f"{hz / 1e3:g} kHz" if hz >= 1e3
             else f"{hz:g} Hz")
    return f"{text}, {width}"


def emission_kind(designators):
    """Readable names for a comma-joined designator list ("11K2F3E,8K10F1E" ->
    "NFM voice (12.5 kHz) / P25 Phase 1"), without repeats. Named types come
    first, then the ones only decoded from their ITU letters."""
    named, generic = [], []
    for d in (designators or "").split(","):
        n = emission_name(d)
        dst = named if d.strip().upper()[:6] in _EMISSION_NAMES else generic
        if n and n not in named and n not in generic:
            dst.append(n)
    return " / ".join(named + generic)


def emission_details(designators):
    """One "designator — name" line per designator (for a tooltip)."""
    return "\n".join(f"{d.strip()} — {emission_name(d) or '?'}"
                     for d in (designators or "").split(",") if d.strip())


# ---------------------------------------------------------------------------
#  Search index: the joined HD/EN/FR/LO/EM records in one SQLite file next to
#  the .dat files, so the downloaded data can be searched (location, frequency,
#  text, service) in well under a second instead of re-parsing ~2 GB each time.
INDEX_NAME = "uls_index.sqlite"
_INDEX_VERSION = 2                  # 2: 800 MHz service codes named (an older index is rebuilt)
_EM_USI, _EM_FREQ, _EM_DESIG = 1, 7, 9


def index_path(path):
    return os.path.join(path, INDEX_NAME)


def index_fresh(path):
    """True if the index exists, has this code's layout, and is newer than the
    .dat files it was built from (a re-download extracts newer files)."""
    ip = index_path(path)
    if not os.path.exists(ip):
        return False
    t = os.path.getmtime(ip)
    for f in ("HD.dat", "FR.dat", "EN.dat", "LO.dat", "EM.dat"):
        p = os.path.join(path, f)
        if os.path.exists(p) and os.path.getmtime(p) > t:
            return False
    try:
        con = sqlite3.connect(f"file:{ip}?mode=ro", uri=True)
        try:
            v = con.execute("SELECT value FROM meta WHERE key='version'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(v) and v[0] == str(_INDEX_VERSION)


def _emissions(path):
    """(USI, Hz) -> comma-joined emission designators, from EM.dat."""
    em = {}
    for r in _rows(os.path.join(path, "EM.dat")):
        if len(r) <= _EM_DESIG or r[0] != "EM":
            continue
        d = r[_EM_DESIG].strip()
        if not d:
            continue
        try:
            hz = int(round(float(r[_EM_FREQ]) * 1e6))
        except ValueError:
            continue
        key = (r[_EM_USI], hz)
        cur = em.get(key)
        if cur is None:
            em[key] = d
        elif d not in cur.split(","):
            em[key] = cur + "," + d
    return em


def build_index(path, log=None):
    """(Re)build ``uls_index.sqlite`` in ``path`` from its .dat files. Takes
    ~1 min for LMpriv; call from a worker thread. Returns the row count."""
    def _log(m):
        if log is not None:
            log(m)

    _log("reading emission designators …")
    em = _emissions(path)
    # data date from the dump's "counts" file ("File Creation Date: …")
    created = ""
    try:
        with open(os.path.join(path, "counts"), encoding="latin-1") as fh:
            first = fh.readline()
        if ":" in first:
            created = first.split(":", 1)[1].strip()
    except OSError:
        pass

    tmp = index_path(path) + ".part"
    try:
        os.remove(tmp)
    except OSError:
        pass
    con = sqlite3.connect(tmp)
    try:
        con.executescript("""
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE ch (freq INTEGER, call TEXT, name TEXT, place TEXT,
                             service TEXT, emission TEXT, lat REAL, lon REAL,
                             usi TEXT);
        """)

        _log("reading licenses …")
        n = [0]

        def rows():
            for usi, hz, call, service, name, place, lat, lon in \
                    _iter_channels(path):
                n[0] += 1
                yield (hz, call, name, place, service, em.get((usi, hz), ""),
                       lat, lon, usi)
        con.executemany("INSERT INTO ch VALUES (?,?,?,?,?,?,?,?,?)", rows())
        _log(f"indexing {n[0]} channels …")
        con.executescript("""
            CREATE INDEX ch_freq ON ch(freq);
            CREATE INDEX ch_lat ON ch(lat);
        """)
        svcs = con.execute("SELECT service, COUNT(*) n FROM ch GROUP BY service "
                           "ORDER BY n DESC").fetchall()
        con.executemany("INSERT INTO meta VALUES (?,?)",
                        [("version", str(_INDEX_VERSION)), ("created", created),
                         ("rows", str(n[0])), ("services", json.dumps(svcs))])
        con.commit()
    finally:
        con.close()
    os.replace(tmp, index_path(path))
    _log("index ready")
    return n[0]


def index_info(path):
    """{"created": data date, "rows": n, "services": [(service, count), …]}."""
    con = sqlite3.connect(f"file:{index_path(path)}?mode=ro", uri=True)
    try:
        meta = dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()
    return {"created": meta.get("created", ""), "rows": int(meta.get("rows", 0)),
            "services": [tuple(s) for s in json.loads(meta.get("services", "[]"))]}


def search(path, lat=None, lon=None, radius_km=None, fmin_hz=None, fmax_hz=None,
           text="", service="", emission="", limit=5000):
    """Search the index. Returns (FoundChannels sorted by frequency, total
    matches). Each result's ``meta`` has call, usi, emission, kind (readable
    emission names), place and, when a location is given, ``dist_km``. With a
    location, only channels that have coordinates match (as ``query``).
    ``emission`` is an EMISSION_FILTERS key: channels licensed for that type."""
    where, args = [], []
    if emission:
        # designators are stored comma-joined: match a prefix at the start or
        # after a comma ("11K2F3E,8K10F1E" matches "8K10F1")
        ors = []
        for p in EMISSION_FILTERS[emission]:
            ors.append("(emission LIKE ? OR emission LIKE ?)")
            args += [f"{p}%", f"%,{p}%"]
        where.append("(" + " OR ".join(ors) + ")")
    if fmin_hz is not None:
        where.append("freq >= ?")
        args.append(int(fmin_hz))
    if fmax_hz is not None:
        where.append("freq <= ?")
        args.append(int(fmax_hz))
    if text:
        like = f"%{text.strip()}%"
        where.append("(name LIKE ? OR call LIKE ? OR place LIKE ?)")
        args += [like, like, like]
    if service:
        where.append("service = ?")
        args.append(service)
    geo = lat is not None and lon is not None and radius_km
    if geo:                          # bounding box in SQL, exact radius below
        dlat = radius_km / 111.0
        dlon = radius_km / (111.0 * max(0.01, math.cos(math.radians(lat))))
        where.append("lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?")
        args += [lat - dlat, lat + dlat, lon - dlon, lon + dlon]
    sql = ("SELECT freq, call, name, place, service, emission, lat, lon, usi "
           "FROM ch")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY freq"
    con = sqlite3.connect(f"file:{index_path(path)}?mode=ro", uri=True)
    try:
        cur = con.execute(sql, args)
        out, total = [], 0
        for freq, call, name, place, service_, emission, la, lo, usi in cur:
            dist = None
            if geo:
                dist = haversine_km(lat, lon, la, lo)
                if dist > radius_km:
                    continue
            total += 1
            if len(out) >= limit:
                continue             # keep counting the matches
            label = " · ".join(x for x in (name, place) if x) or call
            out.append(FoundChannel(
                freq=freq, label=label[:80], service=service_, mode="NFM",
                lat=la, lon=lo, source="fcc",
                meta={"call": call, "usi": usi, "name": name, "place": place,
                      "emission": emission, "kind": emission_kind(emission),
                      "dist_km": dist}))
    finally:
        con.close()
    return out, total


def query(channels, lat, lon, radius_km=40.0, services=None):
    """Geofence parsed channels and optionally filter to given service buckets."""
    found = geofence(channels, lat, lon, radius_km, keep_unlocated=False)
    if services:
        want = {s.lower() for s in services}
        found = [c for c in found if c.service.lower() in want]
    return found


class DownloadCanceled(Exception):
    """Raised inside download_dataset when the cancel callback returns True."""


def has_data(path):
    """True if ``path`` holds the .dat files we need to parse."""
    return bool(path) and os.path.isdir(path) and all(
        os.path.exists(os.path.join(path, f))
        for f in ("HD.dat", "FR.dat"))


def download_dataset(dataset, dest_dir, progress=None, cancel=None, on_open=None,
                     log=None, timeout=45, chunk=65536, max_stalls=12):
    """Stream + unzip a weekly ULS dataset into ``dest_dir``. Returns the dir.

    The FCC download server is slow and prone to mid-stream stalls, so this is
    RESUMABLE: when a read stalls (socket timeout) or the stream ends short, it
    reconnects with an HTTP ``Range`` header and continues from where it left
    off, up to ``max_stalls`` consecutive failures (reset by any good read).

    Streams to a unique ``.part`` temp file in small (64 KiB) reads so ``cancel``
    is polled often (never buffers the file in RAM) and two downloads can't
    collide. Callbacks: ``progress(pct, got, total)`` (bytes; total=0 if the
    server didn't report a size), ``on_open(response)`` (to ``.close()`` a
    blocked read), ``log(str)`` (human status for the terminal). The short
    ``timeout`` bounds how long a stalled connection can block before it retries.

    Call from a worker thread, not the UI thread.
    """
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; choose {list(DATASETS)}")
    os.makedirs(dest_dir, exist_ok=True)
    url = DATASETS[dataset]
    fd, part = tempfile.mkstemp(dir=dest_dir, prefix=f"l_{dataset}_",
                                suffix=".part")
    os.close(fd)

    def _chk_cancel():
        if cancel is not None and cancel():
            raise DownloadCanceled()

    def _log(m):
        if log is not None:
            log(m)

    # urllib doesn't reliably honor the per-call timeout across redirect hops,
    # so pin it process-wide for the duration (restored in finally). This is
    # what actually bounds a stalled connect/read and lets cancel+retry work.
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(timeout)

    total = 0
    got = 0
    stalls = 0
    fh = open(part, "wb")
    try:
        while True:
            _chk_cancel()
            # No custom User-Agent: data.fcc.gov answers 403 to "sdrexplorer/0.1"
            # (2026-10-09) but serves urllib's default and curl's.
            headers = {}
            if got:
                headers["Range"] = f"bytes={got}-"
                _log(f"resuming at {got/1e6:.1f} MB")
            else:
                _log("connecting to data.fcc.gov …")
            try:
                resp = urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=timeout)
            except DownloadCanceled:
                raise
            except urllib.error.HTTPError as e:
                # a refusal (403/404/...) won't change on retry: fail at once
                # with the server's answer instead of looking like a timeout
                if 400 <= e.code < 500 and e.code not in (408, 429):
                    raise RuntimeError(
                        f"data.fcc.gov refused the download: HTTP {e.code} "
                        f"{e.reason} ({url})") from None
                _chk_cancel()
                stalls += 1
                _log(f"HTTP {e.code} {e.reason}; retry {stalls}/{max_stalls}")
                if stalls > max_stalls:
                    raise
                continue
            except Exception as e:
                _chk_cancel()
                stalls += 1
                _log(f"connect failed ({e}); retry {stalls}/{max_stalls}")
                if stalls > max_stalls:
                    raise
                continue
            if on_open is not None:
                on_open(resp)
            if got and resp.getcode() == 200:   # server ignored Range -> restart
                fh.seek(0)
                fh.truncate()
                got = 0
            if not total:
                cr = resp.headers.get("Content-Range")
                if cr and "/" in cr:
                    total = int(cr.rsplit("/", 1)[-1])
                elif resp.headers.get("Content-Length"):
                    total = int(resp.headers["Content-Length"]) + got
            _log(f"connected — {total/1e6:.1f} MB" if total
                 else "connected — size unknown (streaming)")

            short = False
            try:
                while True:
                    _chk_cancel()
                    try:
                        buf = resp.read(chunk)
                    except DownloadCanceled:
                        raise
                    except Exception:      # stall/timeout -> resume with Range
                        _chk_cancel()
                        short = True
                        break
                    if not buf:
                        break
                    fh.write(buf)
                    got += len(buf)
                    stalls = 0
                    if progress is not None:
                        pct = int(100 * got / total) if total else 0
                        progress(pct, got, total)
            finally:
                try:
                    resp.close()
                except Exception:
                    pass

            if total and got >= total:
                break                       # complete
            if not short and not total:
                break                       # clean EOF, size unknown -> done
            _chk_cancel()
            stalls += 1
            _log(f"stalled at {got/1e6:.1f} MB; retry {stalls}/{max_stalls}")
            if stalls > max_stalls:
                raise TimeoutError(
                    f"download stalled after {got}/{total or '?'} bytes "
                    f"({max_stalls} retries)")
    except BaseException:
        fh.close()
        try:
            os.remove(part)
        except OSError:
            pass
        socket.setdefaulttimeout(prev_timeout)
        raise
    fh.close()
    socket.setdefaulttimeout(prev_timeout)

    _log("extracting …")
    with zipfile.ZipFile(part) as zf:
        zf.extractall(dest_dir)
    try:
        os.remove(part)
    except OSError:
        pass
    _log("done")
    return dest_dir
