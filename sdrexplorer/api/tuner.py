# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Who may move the band: a lease on the tuner.

Anything that retunes the hardware on its own -- a scan, the ADS-B park at 1090 MHz, a module searching the
band, a trunk follower, a satellite pass (Doppler) -- takes a lease first. One holder at a time:

  * ``acquire(owner, priority, reason, on_preempt)`` -> True if granted. A request of higher priority than
    the holder's pre-empts it: the holder's ``on_preempt(by_owner)`` is called (it should stop, and not
    retune again), then the newcomer holds the lease. Equal or lower priority is refused.
  * ``release(owner)``.
  * ``holder()`` -> (owner, priority, reason) or None; ``may_tune(owner)`` -> (ok, reason).
  * The user's own tuning (a typed frequency, a digit click, a bank channel) is always highest
    (``USER``): it pre-empts any lease. A waterfall click inside the band doesn't move the band, so it
    doesn't touch the lease.

``changed`` (if set) is called with the lease after every change, for the status bar.
"""
from __future__ import annotations

import sys
import traceback

USER = 1000                # the user tuning by hand: beats every lease
SCAN = 100                 # core priorities, for reference: a module picks its own
PARK = 80                  # a mode that parks the band (ADS-B)
SEARCH = 60                # a module searching bands on its own (lower than a park: it gives way)


class Tuner:
    def __init__(self):
        self._holder = None          # (owner, priority, reason, on_preempt)
        self.changed = None

    def holder(self):
        return None if self._holder is None else self._holder[:3]

    def acquire(self, owner, priority=50, reason="", on_preempt=None):
        h = self._holder
        if h is not None and h[0] == owner:
            self._holder = (owner, priority, reason, on_preempt)
            self._notify()
            return True
        if h is not None and priority <= h[1]:
            return False
        self._holder = (owner, priority, reason, on_preempt)
        if h is not None and h[3] is not None:
            try:
                h[3](owner)
            except Exception:
                print(f"[sdrexplorer] tuner: {h[0]}'s pre-empt handler failed:", file=sys.stderr)
                traceback.print_exc()
        self._notify()
        return True

    def release(self, owner):
        if self._holder is not None and self._holder[0] == owner:
            self._holder = None
            self._notify()
            return True
        return False

    def may_tune(self, owner):
        """(ok, reason): may ``owner`` move the band now (it holds the lease, or nobody does)?"""
        h = self._holder
        if h is None or h[0] == owner:
            return True, ""
        return False, f"the band is held by {h[0]}" + (f" ({h[2]})" if h[2] else "")

    def user_tunes(self):
        """The user tuned by hand: whoever holds the lease is pre-empted. Returns who that was (or None)."""
        h = self._holder
        if h is None:
            return None
        self._holder = None
        if h[3] is not None:
            try:
                h[3]("user")
            except Exception:
                print(f"[sdrexplorer] tuner: {h[0]}'s pre-empt handler failed:", file=sys.stderr)
                traceback.print_exc()
        self._notify()
        return h[0]

    def _notify(self):
        if self.changed is not None:
            self.changed(self)
