# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""sdrexplorer's public module API.

Modules (separate packages, in their own repos) import only from ``sdrexplorer.api``. Everything
else under ``sdrexplorer.*`` is private and may change without notice. The contract, with a changelog, is
docs/module-api.md.

The core names no module, protocol or decoder: labels, protocols, topic and mode names are opaque strings
it routes. Modules find each other by capability (a label they decode, a topic, a request, a registry).

API_VERSION is (major, minor), versioned on its own, apart from the application's version: a minor bump only
adds things, a major bump may break modules. A module states the range it works with (``Module.api``, e.g.
">=1.0,<2.0"); the core won't load it outside that.

What is here:
    Module, ModeSpec, DemodSpec, MenuEntry, Overlay, Panel    the module class and what it registers
    TxModeSpec, TX_RATE                                       a module's transmit mode
    Decode, Position, Annotation                              records for the core's decode / map paths
    TAP_SOURCE, TAP_CHANNEL, Taps                             flowgraph tap points
    AudioArbiter, AudioInput                                  the speaker: claims, a module's own audio
    sources (SourceDriver, Caps, GainStage, DeviceInfo)       SDR devices: drivers register here
    radio (snapshot, band, open_channel, close_channel, ...)  what a module may ask of the radio
    Tuner                                                     who may move the band (a lease)
    EventBus, CORE_TOPICS                                     topics and requests
    ext                                                       extension registries
    store, proofs, paths, testing
    spectrum (SpectrumFrame, subscribe)                       the band's FFT frames
    worker (Worker, clean_env, ...)                           out-of-process workers
    importers (ImportProvider, ImportRequest), FoundChannel   Memories ▸ Import providers
    station (location, now, sample_time)                      where and when
    Channel, parse_tone                                       bank channels: Channel.ext holds module fields
"""
from __future__ import annotations

from .. import paths
from . import ext, importers, proofs, radio, secrets, sources, spectrum, station, store, worker
from ..banks import Channel, parse_tone
from .importers import FoundChannel
from .tuner import Tuner
from .audio import AudioArbiter, AudioInput
from .events import CORE_TOPICS, EVENTS, EventBus
from .module import DemodSpec, MenuEntry, ModeSpec, Module, Overlay, Panel, TxModeSpec
from .records import Annotation, Decode, Position
from .taps import TAP_CHANNEL, TAP_SOURCE, Taps

API_VERSION = (1, 0)
TX_RATE = 240_000            # a TxModeSpec's modulator output rate (complex baseband)
API_VERSION_STR = "%d.%d" % API_VERSION

__all__ = ["API_VERSION", "API_VERSION_STR", "Module", "ModeSpec", "DemodSpec", "MenuEntry", "Overlay", "TxModeSpec",
           "TX_RATE",
           "Panel", "Decode", "Position", "Annotation", "Taps", "TAP_SOURCE", "TAP_CHANNEL", "AudioArbiter",
           "AudioInput", "EventBus", "CORE_TOPICS", "EVENTS", "ext", "store", "proofs", "paths", "secrets", "sources",
           "Tuner", "radio", "spectrum", "worker", "importers", "station", "Channel", "parse_tone", "FoundChannel"]
