# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""What a module may ask of the radio, as plain functions over the core's ScannerRadio, so a
module never reaches into its attributes.

    snapshot(radio, n, timeout)          the next n raw band samples (complex64), or None
    band(radio)                          (center_hz, samp_rate, running)
    source(radio)                        {"replay": bool, "meta": the replayed file's sidecar or {}, "path"}
    open_channel(radio, center_hz, rate, path)
                                         a narrowband channel off the band, written to a cs16 file
    close_channel(radio, ch)
    on_channels_closed(radio, fn)        fn([channels]) when the core closes channels (band moved, radio stop)
    on_retune(radio, fn) / off_retune    fn(offset_hz) each time the channel tap moves to another frequency
"""
from __future__ import annotations


def band(radio):
    return int(radio.band_center_hz), int(radio.samp_rate), bool(radio.running)


def source(radio):
    """What the band comes from: {"replay": True, "meta": {...}, "path": file} while an IQ file plays (meta is
    its .json sidecar: "scope" "band" / "sub" / ..., "samp_rate", "rf_bw_hz", ...), else {"replay": False,
    "meta": {}, "path": None}."""
    if not radio.is_replay():
        return {"replay": False, "meta": {}, "path": None}
    rp = radio.replay or {}
    return {"replay": True, "meta": dict(rp.get("meta") or {}), "path": rp.get("path")}


def snapshot(radio, n, timeout=5.0):
    """The next ``n`` contiguous raw band samples (complex64 at the capture rate, centered on the band
    center), or None when the radio isn't running / timed out. Blocks: call it off the Qt thread."""
    return radio.snapshot_iq(int(n), timeout)


def snapshot_tap(radio, on=True):
    """Keep the snapshot tap attached between snapshot() calls (cheap while idle), for a caller that
    snapshots every cycle and wants arm()/collect() to overlap with its own processing. Returns the tap
    (``capture(n)``, ``arm(n)`` / ``armed(n)`` / ``collect()``), or None when detached. Attach / detach on the
    Qt thread; ``on=None`` only returns the current tap (safe from a worker thread)."""
    if on is not None:
        radio.enable_snapshot_tap(bool(on))
    return radio.snapshot_tap


def open_channel(radio, center_hz, rate, path=None):
    """A channel at ``center_hz`` (anywhere in the captured band) at ``rate`` (a divisor of 240 kHz, e.g.
    48 kHz), written to ``path`` as cs16. Returns its handle (a dict: center_hz, rate, path, t0). Several can run at once; the core closes them all when the band moves or the radio stops
    (see on_channels_closed). Raises ValueError outside the band."""
    if path is None:
        raise ValueError("open_channel needs a path (a cs16 file)")
    return radio.open_channel(path, center_hz, rate=rate)


def close_channel(radio, ch):
    radio.close_channel(ch)


def channels(radio):
    return list(radio.channels())


def on_retune(radio, fn):
    """Call ``fn(offset_hz)`` each time the channel (TAP_CHANNEL) is moved to another frequency: a tune,
    a waterfall click, a scan landing on a channel, the radio starting. It runs on whatever thread tuned
    (often not the Qt thread), so it should only drop state -- e.g. flush audio queued from the old channel.
    ``offset_hz`` is the new channel's offset from the band center. Several callers may register."""
    hooks = radio.__dict__.setdefault("retune_hooks", [])
    if fn not in hooks:
        hooks.append(fn)


def off_retune(radio, fn):
    hooks = radio.__dict__.get("retune_hooks", [])
    if fn in hooks:
        hooks.remove(fn)


def on_channels_closed(radio, fn):
    """fn([closed channels]) when the core closes them itself (a band move, the radio stopping)."""
    radio.on_channels_closed = fn
