# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Signal-triggered sub-band recording: watch a selected window of the band and record it while
a signal is present.

TriggerMonitor is the state machine only (no Qt / GNU Radio, so it is unit-testable): feed it the
window's signal level over the noise floor a few times a second and it says when to start and stop
a recording. The app (ScannerApp._arm_trigger) polls ScannerRadio.window_level and acts on it.

  armed --(level >= threshold for attack_s)--> recording
  recording --(hang_s since the level last dropped below threshold - hysteresis without coming
      back to threshold, or max_s reached)--> armed (re-armed: every transmission becomes its own
      file) or disarmed if one_shot.
"""
from __future__ import annotations

TRIGGER_THRESHOLD_DB = 10.0      # window peak over the noise floor that counts as "a signal"
TRIGGER_HYSTERESIS_DB = 3.0      # ... and it must fall this far below it to count as gone
TRIGGER_ATTACK_S = 0.2           # above threshold this long before recording starts (no clicks)
TRIGGER_HANG_S = 2.0             # below it this long before the recording stops (speech pauses,
                                 # TDMA / burst gaps)
TRIGGER_MAX_S = 120.0            # cap one triggered recording
TRIGGER_PRE_S = 2.0              # pre-roll saved from the snapshot ring: the channel is usually
                                 # idle before a trigger, so the whole 10-12 s buffer is mostly
                                 # silence; 2 s covers the attack time and the transmission's start


class TriggerMonitor:
    def __init__(self, threshold_db=TRIGGER_THRESHOLD_DB, hang_s=TRIGGER_HANG_S,
                 max_s=TRIGGER_MAX_S, attack_s=TRIGGER_ATTACK_S,
                 hysteresis_db=TRIGGER_HYSTERESIS_DB, one_shot=False):
        self.threshold_db = float(threshold_db)
        self.hang_s = float(hang_s)
        self.max_s = float(max_s)
        self.attack_s = float(attack_s)
        self.hysteresis_db = float(hysteresis_db)
        self.one_shot = bool(one_shot)
        self.state = "armed"             # "armed" | "recording" | "done"
        self.count = 0                   # recordings triggered so far
        self._above_since = None
        self._below_since = None
        self._rec_since = None

    def feed(self, level_db, now):
        """One measurement (dB over the floor, or None if unavailable) at time ``now`` (s).
        Returns "start", "stop" or None."""
        if self.state == "done" or level_db is None:
            return None
        if self.state == "armed":
            if level_db >= self.threshold_db:
                if self._above_since is None:
                    self._above_since = now
                if now - self._above_since >= self.attack_s:
                    self.state, self._rec_since, self._below_since = "recording", now, None
                    self._above_since = None
                    self.count += 1
                    return "start"
            else:
                self._above_since = None
            return None
        # recording. Only the signal coming back to the start threshold resets the hang timer;
        # between (threshold - hysteresis) and threshold it neither starts nor resets it. The
        # window's peak-over-floor on an idle channel spikes to ~7-9 dB, so resetting on anything
        # above (threshold - hysteresis) kept a recording going through the silence after it.
        if level_db >= self.threshold_db:
            self._below_since = None
        elif level_db < self.threshold_db - self.hysteresis_db and self._below_since is None:
            self._below_since = now
        if (self._below_since is not None and now - self._below_since >= self.hang_s) or \
                now - self._rec_since >= self.max_s:
            self.state = "done" if self.one_shot else "armed"
            self._below_since = self._rec_since = None
            return "stop"
        return None
