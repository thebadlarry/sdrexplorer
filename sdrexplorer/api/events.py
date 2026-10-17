# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The bus: topics (publish / subscribe), requests (ask, and get one answer), for the core and modules.

Handlers run on the Qt main thread (hand data from a worker thread to it through a Qt signal first).

Core topics (no prefix; payload keyword arguments):
    tuned            freq_hz, primary (True: the band was recentered; False: demod moved within the band),
                     how ("typed" | "click" | "nudge" | "channel" | "scan" | "restore" | a module's name)
    band_changed     center_hz, samp_rate
    mode_changed     name, picker ("mode")
    radio_started    replay (bool)
    radio_stopped    -
    recording_done   path, scope
    scan             state ("sweep": moving between channels | "hold": parked on an active one | "idle")
    decode           record (api.Decode)
    position         record (api.Position)
    annotations      source, items ([api.Annotation])
    rate_changed     samp_rate
    recording_started path, scope, start_time
    replay_seek      from_s, to_s: the replayed file jumped (decoders reset)
    replay_pause     paused, position_s
    replay_loop      enabled
    replay_wrap      position_s: a looping file started over
    station_changed  location (api.station.Location or None)

Module topics are owner-prefixed ("<module>.<name>", e.g. "sat.pass") and need no declaration in the core:
anything with a "." is accepted. Publishing a topic nobody subscribes to, or subscribing to one nobody
publishes, is fine. The same goes for requests: ``request()`` returns ``default`` when no one answers.
"""
from __future__ import annotations

import sys
import traceback
from collections import deque

CORE_TOPICS = ("tuned", "band_changed", "mode_changed", "radio_started", "radio_stopped", "recording_done",
               "scan", "decode", "position", "annotations", "rate_changed", "recording_started", "replay_seek",
               "replay_pause", "replay_loop", "replay_wrap", "station_changed")
EVENTS = CORE_TOPICS                       # older name, kept


class EventBus:
    TRACE_N = 500                          # recent traffic kept for the debug view

    def __init__(self):
        self._subs = {}
        self._answers = {}                 # request name -> (fn, owner)
        self.trace = deque(maxlen=self.TRACE_N)

    # -- topics ---------------------------------------------------------------------------------------
    def subscribe(self, topic, handler):
        self._check(topic)
        hs = self._subs.setdefault(topic, [])
        if handler not in hs:
            hs.append(handler)

    def unsubscribe(self, topic, handler):
        self._check(topic)
        hs = self._subs.get(topic, [])
        if handler in hs:
            hs.remove(handler)

    def publish(self, topic, **payload):
        """Call every subscriber; one that raises is reported and the rest still run."""
        self._check(topic)
        hs = list(self._subs.get(topic, ()))
        self.trace.append(("topic", topic, len(hs)))
        for h in hs:
            try:
                h(**payload)
            except Exception:
                print(f"[sdrexplorer] {topic} handler {getattr(h, '__qualname__', h)} failed:", file=sys.stderr)
                traceback.print_exc()

    emit = publish                         # older name, kept

    def subscribers(self, topic):
        return len(self._subs.get(topic, ()))

    # -- requests -------------------------------------------------------------------------------------
    def provide_request(self, name, fn, owner=""):
        """Answer request ``name`` with ``fn(**args)``. One provider per name: the first one wins."""
        self._check_request(name)
        if name in self._answers:
            print(f"[sdrexplorer] request {name!r} already answered by {self._answers[name][1]!r}; "
                  f"{owner!r} ignored", file=sys.stderr)
            return False
        self._answers[name] = (fn, owner)
        return True

    def can_answer(self, name):
        return name in self._answers

    def request(self, name, default=None, **args):
        """Ask; the provider's answer, or ``default`` if nobody provides ``name`` (or it failed)."""
        hit = self._answers.get(name)
        self.trace.append(("request", name, 1 if hit else 0))
        if hit is None:
            return default
        try:
            return hit[0](**args)
        except Exception:
            print(f"[sdrexplorer] request {name} ({hit[1]}) failed:", file=sys.stderr)
            traceback.print_exc()
            return default

    # -------------------------------------------------------------------------------------------------
    @staticmethod
    def _check(topic):
        if topic not in CORE_TOPICS and "." not in topic:
            raise ValueError(f"unknown topic {topic!r}: core topics are {CORE_TOPICS}; module topics "
                             f"are owner-prefixed, '<module>.<name>'")

    @staticmethod
    def _check_request(name):
        if "." not in name:
            raise ValueError(f"request names are owner-prefixed, '<module>.<name>': {name!r}")
