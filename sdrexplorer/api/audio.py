# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The core speaker: who may use it, and how a module's own audio gets to it.

Claims: the analog demod is always running. A decoder that produces its own audio claims the speaker while
it has a lock, and the analog path is muted however the squelch reads. The core asks ``analog_allowed()`` on
every squelch poll; a retune or a waterfall click ends every claim (``release_all``).

Inputs: ``open_input(owner, rate)`` gives a stream into the core's audio chain, mixed after the analog
volume / squelch gate and scaled by the same volume control. ``write()`` from any thread; when nothing is
written the input is silent. Inputs can be opened before the radio exists; the core connects them when
it does (``bind``).
"""
from __future__ import annotations

import threading

import numpy as np


class AudioInput:
    """A module's audio into the speaker. Written PCM is resampled to the core rate in the flowgraph."""

    MAX_S = 2.0                     # buffered audio beyond this is dropped (oldest first)

    def __init__(self, arbiter, owner, rate, channels=1):
        self.arbiter, self.owner = arbiter, owner
        self.rate, self.channels = int(rate), int(channels)
        self._lock = threading.Lock()
        self._buf = np.zeros(0, np.float32)
        self.dropped = 0            # samples dropped because the buffer was full
        self.block = None           # the flowgraph source (core side), set by bind
        self.closed = False

    def write(self, pcm):
        """int16 (full scale 32767) or float (full scale 1.0) samples; stereo is mixed to mono."""
        if self.closed:
            return
        x = np.asarray(pcm)
        if x.dtype == np.int16:
            x = x.astype(np.float32) / 32768.0
        else:
            x = x.astype(np.float32, copy=False)
        if self.channels > 1 and x.ndim == 1:
            x = x[: len(x) // self.channels * self.channels].reshape(-1, self.channels).mean(1)
        elif x.ndim > 1:
            x = x.mean(1)
        cap = int(self.MAX_S * self.rate)
        with self._lock:
            buf = np.concatenate([self._buf, x])
            if len(buf) > cap:
                self.dropped += len(buf) - cap
                buf = buf[-cap:]
            self._buf = buf

    def read(self, n):
        """(core) up to n samples, zero-padded."""
        out = np.zeros(n, np.float32)
        with self._lock:
            k = min(n, len(self._buf))
            out[:k] = self._buf[:k]
            self._buf = self._buf[k:]
        return out

    def buffered_s(self):
        with self._lock:
            return len(self._buf) / float(self.rate)

    def flush(self):
        with self._lock:
            self._buf = np.zeros(0, np.float32)

    def close(self):
        self.closed = True
        self.flush()
        self.arbiter._inputs = [i for i in self.arbiter._inputs if i is not self]
        if self.arbiter._binder is not None:
            self.arbiter._binder.detach_audio_input(self)


class AudioArbiter:
    def __init__(self):
        self._claims = {}             # owner -> (priority, label)
        self.changed = None
        self.analog_off = False       # "digital only": never pass analog audio
        self._muted_by = set()        # owners that asked for analog off (mute_analog)
        self._inputs = []
        self._binder = None           # the radio, once it exists (connects inputs into the flowgraph)

    # -- claims -----------------------------------------------------------------------------------------
    def claim(self, owner, priority=10, label=""):
        if self._claims.get(owner) == (priority, label):
            return
        self._claims[owner] = (priority, label)
        self._notify()

    def release(self, owner):
        if self._claims.pop(owner, None) is not None:
            self._notify()

    def release_all(self):
        """A retune leaves the channel: every claim ends at once, and queued decoded audio is dropped."""
        for i in self._inputs:
            i.flush()
        if self._claims:
            self._claims.clear()
            self._notify()

    def claimed(self, owner=None):
        return owner in self._claims if owner is not None else bool(self._claims)

    def owner(self):
        """The highest-priority claimant, or None."""
        return max(self._claims, key=lambda o: self._claims[o][0]) if self._claims else None

    def label(self):
        """The top claim's label ("" if none or unlabeled): shown on the signal meter."""
        o = self.owner()
        return self._claims[o][1] if o is not None else ""

    def mute_analog(self, owner, on=True):
        """A module asks for analog off without claiming the speaker (e.g. FSK warble while decoding)."""
        (self._muted_by.add if on else self._muted_by.discard)(owner)

    def analog_allowed(self):
        return not self._claims and not self.analog_off and not self._muted_by

    # -- inputs -----------------------------------------------------------------------------------------
    def open_input(self, owner, rate, channels=1):
        i = AudioInput(self, owner, rate, channels)
        self._inputs.append(i)
        if self._binder is not None:
            self._binder.attach_audio_input(i)
        return i

    def inputs(self):
        return list(self._inputs)

    def bind(self, radio):
        """(core) The radio exists: connect inputs opened so far, and later ones as they open."""
        self._binder = radio
        for i in self._inputs:
            radio.attach_audio_input(i)

    def _notify(self):
        if self.changed is not None:
            self.changed(self)
