# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Signal sources: SDR devices (and later network / file inputs) as a registry, so new hardware needs no core
change. The core builds its UI from what a device reports (``Caps``), never from its name.

A driver lists the devices it can open (``enumerate``), describes one (``caps``), and opens it (``open``)
into a ``SourceHandle`` whose ``block`` is a GNU Radio source of complex samples at the chosen rate. The
core's own driver (sdrexplorer.sources.osmosdr_driver) covers gr-osmosdr's native HackRF / RTL-SDR support
and every SoapySDR device (LimeSDR, Airspy, bladeRF, USRP, PlutoSDR, ...). A module adds a driver with
``Module.sources()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class GainStage:
    name: str                  # as the driver names it ("RF", "LNA", "TIA", ...)
    lo: float
    hi: float
    step: float = 1.0
    default: Optional[float] = None


@dataclass
class Caps:
    """What a device can do. Unknown is None / empty, and the core then offers nothing for it."""
    freq_range: tuple = (0.0, 0.0)          # Hz
    rate_ranges: list = field(default_factory=list)   # [(lo, hi)] Hz the device accepts
    rates: list = field(default_factory=list)          # discrete rates, when the device has a fixed list
    bandwidths: list = field(default_factory=list)     # analog filter settings, Hz (or [(lo, hi)])
    gains: list = field(default_factory=list)          # [GainStage]
    agc: bool = False
    antennas: list = field(default_factory=list)
    ppm: bool = False                        # frequency correction
    dc_spike: bool = True                    # has a DC spike to tune around
    tx: bool = False
    tx_why: str = ""                         # why there's no transmit, when tx is False
    full_duplex: bool = False

    def accepts_rate(self, rate):
        if self.rates:
            return any(abs(rate - r) < 1 for r in self.rates)
        return any(lo - 1 <= rate <= hi + 1 for lo, hi in self.rate_ranges)


@dataclass
class DeviceInfo:
    driver: str                # the SourceDriver's name
    key: str                   # stable id for settings: "<family>:<serial>"
    label: str                 # shown in the SDR picker
    args: str = ""             # what the driver needs to open it
    family: str = ""           # "hackrf", "rtlsdr", "lime", ...: for settings migration and display only
    serial: str = ""
    present: bool = True       # False: remembered, not attached now
    soapy: bool = False        # (driver detail) listed through SoapySDR
    soapy_args: Optional[dict] = None   # (driver detail) SoapySDR's description of the same radio


class SourceHandle:
    """An opened device. ``block`` is its GNU Radio source (complex out). Setters take effect while
    running; ones a device lacks are ignored."""
    block = None

    def set_center(self, hz):
        raise NotImplementedError

    def set_rate(self, rate):
        raise NotImplementedError

    def set_gain(self, stage, db):
        pass

    def set_bandwidth(self, hz):
        pass

    def set_antenna(self, name):
        pass

    def set_freq_corr(self, ppm):
        pass

    def close(self):
        self.block = None


class SourceDriver:
    name = ""                  # unique
    title = ""

    def probe(self):
        """(ok, reason): is the library this driver needs there?"""
        return True, ""

    def enumerate(self):
        """[DeviceInfo] attached now. May take a second or two: the core calls it off the UI thread."""
        return []

    def caps(self, dev):
        """Caps for a device (may open it briefly; not while it's streaming)."""
        return Caps()

    def open(self, dev, settings):
        """A SourceHandle for ``dev``, configured from ``settings`` (rate, center, gains, ...)."""
        raise NotImplementedError

    def sink_args(self, dev):
        """What a transmit sink needs to open this device, or None (no transmit)."""
        return None


_drivers = {}


def register(driver):
    _drivers.setdefault(driver.name, driver)


def drivers():
    return dict(_drivers)


def get(name):
    return _drivers.get(name)
