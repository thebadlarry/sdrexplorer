# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Flowgraph tap points: where a module connects its own GNU Radio branch.

TAP_SOURCE   the raw band (complex, the capture rate, centered on radio.band_center_hz). Only exists while
             the radio runs: attach() on it is remembered and (re)connected on every start().
TAP_CHANNEL  the tuned channel (complex at radio.QUAD_RATE = 240 kHz, the demod frequency at 0 Hz). It
             always exists; when the capture rate changes the core rebuilds it and re-points every
             attached block at the new one.

attach(point, head, edges=[(a, b), ...]): a whole branch -- the head block on the point plus the edges
inside the branch, connected and disconnected together, so the flowgraph never holds a branch block left
without its input.

suspend(point) / resume(point): the core takes every branch off a point for a while (e.g. a mode that
needs the CPU for something else) and puts them back. attach / detach meanwhile are remembered.
"""
from __future__ import annotations

TAP_SOURCE = "source"
TAP_CHANNEL = "channel"
POINTS = (TAP_SOURCE, TAP_CHANNEL)


class Taps:
    """attach(point, block) / detach(point, block) for one ScannerRadio. Connections are made under the
    flowgraph lock, so a running radio keeps running."""

    def __init__(self, radio):
        self.radio = radio
        self._blocks = {p: [] for p in POINTS}
        self._edges = {}                 # id(head) -> [(a, b)] inside its branch
        self._suspended = set()

    def attached(self, point):
        return list(self._blocks[point])

    def attach(self, point, block, edges=()):
        """Connect ``block`` to ``point``. ``edges``: the branch's own connections downstream of it,
        made with it (before it, in one reconfiguration) and undone with it."""
        if point not in self._blocks:
            raise ValueError(f"unknown tap point {point!r} (one of {POINTS})")
        if any(b is block for b in self._blocks[point]):
            return
        self._blocks[point].append(block)
        self._edges[id(block)] = list(edges)
        node = self._node(point)
        live = node is not None and point not in self._suspended

        def go():
            for a, b in self._edges[id(block)]:
                self.radio.connect(a, b)
            if live:
                self.radio.connect(node, block)
        self._locked(go)

    def detach(self, point, block):
        if not any(b is block for b in self._blocks.get(point, [])):
            return
        self._blocks[point] = [b for b in self._blocks[point] if b is not block]
        edges = self._edges.pop(id(block), [])
        node = self._node(point)
        live = node is not None and point not in self._suspended

        def go():
            if live:
                try:
                    self.radio.disconnect(node, block)
                except Exception:
                    pass
            for a, b in reversed(edges):
                try:
                    self.radio.disconnect(a, b)
                except Exception:
                    pass
        self._locked(go)

    # -- called by ScannerRadio -------------------------------------------------------------------
    def suspended(self, point):
        return point in self._suspended

    def suspend(self, point):
        """(core) Disconnect every block on ``point`` until resume(). The caller holds the flowgraph lock
        (or the radio is stopped)."""
        if point in self._suspended:
            return
        self._suspended.add(point)
        node = self._node(point)
        for b in self._blocks[point]:
            if node is not None:
                try:
                    self.radio.disconnect(node, b)
                except Exception:
                    pass
            for e in reversed(self._edges.get(id(b), [])):
                try:
                    self.radio.disconnect(*e)
                except Exception:
                    pass

    def resume(self, point):
        """(core) Reconnect what suspend() took off (and what was attached meanwhile)."""
        if point not in self._suspended:
            return
        self._suspended.discard(point)
        node = self._node(point)
        for b in self._blocks[point]:
            for e in self._edges.get(id(b), []):
                self.radio.connect(*e)
            if node is not None:
                self.radio.connect(node, b)

    def source_targets(self):
        """Blocks to connect to the source at start() (the radio connects / disconnects them)."""
        return [] if TAP_SOURCE in self._suspended else list(self._blocks[TAP_SOURCE])

    def channel_rebuilt(self, old_chan, new_chan):
        """The channelizer was replaced (capture-rate change, flowgraph stopped): re-point."""
        if TAP_CHANNEL in self._suspended:
            return
        for b in self._blocks[TAP_CHANNEL]:
            try:
                self.radio.disconnect(old_chan, b)
            except Exception:
                pass
            self.radio.connect(new_chan, b)

    # ------------------------------------------------------------------------------------------------
    def _node(self, point):
        if point == TAP_CHANNEL:
            return self.radio.chan
        return self.radio.src if self.radio.running else None

    def _locked(self, fn):
        if self.radio.running:
            self.radio.lock()
            try:
                fn()
            finally:
                self.radio.unlock()
        else:
            fn()
