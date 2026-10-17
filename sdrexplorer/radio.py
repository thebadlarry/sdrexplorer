# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Mode-switchable analog RX engine for HackRF (GNU Radio 3.10).

Front end (shared by every mode):

    osmosdr HackRF @ 2.4 Msps, offset-tuned to (freq - OFFSET)
      -> freq_xlating_fir_filter_ccf  (decim 10, shift +OFFSET -> baseband)
      -> DemodChain (per-mode hier block, complex@240k -> float@48k)
      -> volume -> audio sink

The offset-tune trick (source at freq-OFFSET, xlating shifts +OFFSET back to
baseband) keeps the wanted channel 300 kHz away from the HackRF DC spike — the
same pattern proven in the WFM reference receiver.

A wideband log-power-FFT probe hangs off the raw source for the scanner to read
channel power without disturbing the audio path.

Mode switching rebuilds only the DemodChain (wrapped in a hier_block2) inside a
lock()/unlock() so the front end and audio sink stay put.
"""
from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import sys
import tempfile
import threading
import time
from fractions import Fraction

import numpy as np

from gnuradio import analog, audio, blocks, digital, gr
from gnuradio import filter as gr_filter
from gnuradio.filter import firdes
from gnuradio.fft import logpwrfft

from . import rds as _rdsdec
from . import iqring as _iqring
from .api.taps import TAP_CHANNEL, Taps
from . import paths as _paths
from . import sources as _sources_quiet  # noqa: F401  (quiets the SDR libraries' device-search logs)

# gr-rds (RDS block sync/CRC/group parsing: https://github.com/bastibl/gr-rds,
# maint-3.10), built against this GNU Radio 3.10 install and installed to a local
# prefix (paths key "gr_rds"; paths.activate preloads its .so, which isn't on the
# loader path), or a distro / conda gnuradio-rds package.
_rds = None
try:
    _paths.activate("gr_rds")
    import rds as _rds
except Exception:
    _rds = None

RDS_AVAILABLE = _rds is not None

# --- fixed front-end constants ----------------------------------------------
SAMP_RATE = 2_400_000                # default capture bandwidth (Hz)
# selectable capture bandwidths: all exact multiples of QUAD_RATE (240 kHz) so
# STAGE1_DECIM is the only thing that varies with capture width — QUAD_RATE,
# STAGE2_DECIM, AUDIO_RATE, and the whole DemodChain stay fixed regardless of
# which one is picked. All are HackRF-clean rates under its 20 Msps ceiling;
# RTL-SDR is restricted to the first entry (see SDR_DEVICES / app.py).
SAMP_RATE_OPTIONS = [2_400_000, 4_800_000, 9_600_000, 14_400_000, 19_200_000]
OFFSET = 300_000                     # offset-tune away from the HackRF DC spike
QUAD_RATE = 240_000                  # fixed rate after stage-1 channelizer
# Least time between two flowgraph reconfigurations (lock / unlock) while a device streams. Each one stops
# and restarts every block; gr-osmosdr's RTL-SDR source restarts librtlsdr's USB reader thread, and a second
# reconfiguration within ~0.1 s of the first tears the source down while that thread is still starting: an
# exception in a scheduler thread, which aborts the process (seen entering a module's mode: the demod swap,
# then its decode branch). 0.25 s leaves a margin; a file replay has no such thread and isn't held back.
RECONFIG_GAP_S = 0.25
AUDIO_RATE = 48_000
STAGE2_DECIM = QUAD_RATE // AUDIO_RATE     # 5
NFFT = 2048
DC_GUARD = 100_000                   # ± this around DC is untrustworthy (spike)
MAX_SELECTION_BW_HZ = 5_000_000      # cap on a drag-selected sub-band recording
SNAPSHOT_SECONDS = 10                # default length of the retroactive IQ snapshot buffer
CHANNEL_RATE = 48_000                # extra channels' default rate (48 kHz: fits signals ~40 kHz wide)
SNAPSHOT_POST_S = 2.0                # a snapshot keeps capturing this long after the click

# --- transmit (NFM/AM/WFM/USB/LSB — see radio.TX_MODES) ----------------------
# Mirrors the RX front end in reverse: a per-mode modulator produces complex
# audio at QUAD_RATE (240k) — the same rate DemodChain operates at, and a
# modest-enough interp factor (x5) for filter design — then a second
# interpolation stage brings it up to TX_SAMP_RATE for the HackRF sink (x10,
# matching stage1_decim on the RX side). Jumping straight from AUDIO_RATE to a
# 2.4 Msps quad_rate (interp x50) fails: optfir can't design that filter
# ("insufficient extremals"). Half-duplex: TxChain only ever runs while the
# ScannerRadio source is stopped (see app.py _toggle_ptt).
TX_SAMP_RATE = SAMP_RATE
AM_MOD_INDEX = 0.8            # < 1 so the AM TX envelope never goes negative
# < 1/(1+AM_MOD_INDEX) so peak envelope (carrier + full modulation, 0.9) stays
# under the TX sink's unity full scale — headroom against clipping (which
# sounds like static/splatter on a receiver).
AM_CARRIER_AMPLITUDE = 0.5


# --- optional in-process ADS-B decoder (gr-adsb, mhostetter maint-3.10) -------
# Pure-Python GNU Radio blocks (framer/demod/decoder): its python/ dir (paths key
# "gr_adsb", or the old ADSB_PATH variable) goes on sys.path. No CMake build. If absent,
# the ADS-B mode degrades to a note and everything else runs normally.
import pmt                           # core GNU Radio, always present
try:
    _paths.activate("gr_adsb")
    import adsb as _adsb              # gr-adsb (pure-Python blocks) via PYTHONPATH
    ADSB_AVAILABLE = True
except Exception:
    _adsb = None
    ADSB_AVAILABLE = False

if ADSB_AVAILABLE:
    # gr-adsb's framer computes a burst SNR as median(noise window before the
    # pulse); when a candidate pulse sits at sample 0 that window is an empty
    # slice, so numpy warns "Mean of empty slice" / "invalid value ... divide"
    # and the burst gets a NaN SNR (harmless — that candidate is just dropped).
    # It fires from a GR scheduler thread we don't own, so scope-suppress the two
    # benign RuntimeWarnings from gr-adsb's framer module rather than let them
    # spam stderr during decoding.
    import warnings as _warnings
    for _msg in (r"Mean of empty slice", r"invalid value encountered in divide"):
        _warnings.filterwarnings("ignore", message=_msg, category=RuntimeWarning,
                                 module=r"(numpy|adsb)\..*")

ADSB_FREQ = 1_090_000_000            # ADS-B 1090ES center frequency
ADSB_RATE = 2_000_000                # gr-adsb needs fs % 1 Msym == 0; 2.4M -> 2.0M

# --- optional CUDA GPU acceleration OOT module (gr-sdrexplorer_gpu) ----------
# If the compiled gr-sdrexplorer_gpu OOT module is found (paths key
# "gr_sdrexplorer_gpu", default gr-sdrexplorer_gpu/build in this checkout), enable CUDA-
# accelerated channelization (multi-channel DDC). Without it the app runs fine
# on CPU via the standard freq_xlating_fir_filter block.
try:
    _paths.activate("gr_sdrexplorer_gpu")
    import sdrexplorer_gpu_python as _sdrexplorer_gpu
    CUDA_AVAILABLE = True
    print("sdrexplorer: CUDA GPU acceleration available (gr-sdrexplorer_gpu)")
except Exception:
    _sdrexplorer_gpu = None
    CUDA_AVAILABLE = False

# Devices come from source drivers (sdrexplorer.sources / api.sources): whatever is attached, listed by key
# ("hackrf:<serial>"), with its gain stages, rates and transmit from what the driver reports. The old fixed
# names still work as aliases for the first device of that family. Legacy table, kept for callers that
# check a name; selection uses the driver. HackRF supports the full
# SAMP_RATE_OPTIONS range; RTL-SDR is restricted to the default (2.4 Msps) by
# the app UI — it never supported the wideband multichannel case anyway.
SDR_DEVICES = {
    "HackRF": "numchan=1 hackrf=0",
    "RTL-SDR": "numchan=1 rtl=0",
    "IQ file": None,       # replay a saved .cs16 capture (no hardware); see set_replay
}
IQ_FILE_DEVICE = "IQ file"


# ADS-B emitter category (DO-260B). Only aircraft-identification messages
# (type code 1-4) carry it, in the 3-bit subfield immediately after the type
# code. This is the ONLY airframe-class hint ADS-B broadcasts over the air —
# it's coarse (Light/Heavy/Rotorcraft/UAV/…), not an exact type like "C-130J".
# Value = (human label, coarse class the map turns into a distinct icon).
_ADSB_CATEGORY = {
    (2, 1): ("Surface emergency vehicle", "ground"),
    (2, 3): ("Surface service vehicle",   "ground"),
    (2, 4): ("Ground obstruction",        "ground"),
    (2, 5): ("Ground obstruction",        "ground"),
    (2, 6): ("Ground obstruction",        "ground"),
    (2, 7): ("Ground obstruction",        "ground"),
    (3, 1): ("Glider/sailplane",          "glider"),
    (3, 2): ("Lighter-than-air",          "balloon"),
    (3, 3): ("Parachutist",               "light"),
    (3, 4): ("Ultralight/hang-glider",    "light"),
    (3, 6): ("UAV",                       "uav"),
    (3, 7): ("Space vehicle",             "jet"),
    (4, 1): ("Light",                     "light"),
    (4, 2): ("Small",                     "light"),
    (4, 3): ("Large",                     "large"),
    (4, 4): ("High-vortex large",         "heavy"),
    (4, 5): ("Heavy",                     "heavy"),
    (4, 6): ("High performance",          "jet"),
    (4, 7): ("Rotorcraft",                "helo"),
}


def _bits_to_int(bits):
    """MSB-first bit sequence (ints / numpy scalars) -> int."""
    v = 0
    for b in bits:
        v = (v << 1) | (int(b) & 1)
    return v


def adsb_emitter_category(bits):
    """(label, ac_class) for an ADS-B extended-squitter frame, or None when the
    message isn't an aircraft-identification message or carries no category.

    ``bits`` is the full 112-bit ME frame as decoded by gr-adsb: the type code
    is bits[32:37] and the 3-bit emitter-category subfield is bits[37:40] (the
    bits just before the 8 callsign characters that start at bit 40)."""
    if bits is None or len(bits) < 40:
        return None
    tc = _bits_to_int(bits[32:37])
    if tc not in (1, 2, 3, 4):
        return None
    ca = _bits_to_int(bits[37:40])
    if ca == 0:
        return None                         # "no category information"
    return _ADSB_CATEGORY.get((tc, ca))


class AdsbMsgSink(gr.sync_block):
    """Message-only sink for gr-adsb's decoder 'decoded' port: convert each PDU's
    PMT metadata dict to a Python dict and push it onto ``out_queue`` for the app
    (drop on Full). A plain Python message block — reliable in this GR build."""

    def __init__(self, out_queue):
        gr.sync_block.__init__(self, name="adsb_msg_sink", in_sig=None, out_sig=None)
        self._q = out_queue
        self._cat = {}                  # icao -> (label, ac_class), from ID messages
        self.message_port_register_in(pmt.intern("decoded"))
        self.set_msg_handler(pmt.intern("decoded"), self._on_msg)

    def _on_msg(self, msg):
        try:
            meta = pmt.to_python(pmt.car(msg))   # decoder packs the aircraft dict in car
        except Exception:
            return
        if not isinstance(meta, dict):
            return
        icao = str(meta.get("icao") or "").strip()
        # The emitter category rides only in aircraft-identification messages;
        # decode it from the raw bit vector (pmt.cdr) and cache per ICAO so every
        # later position/velocity dict for that plane carries its class too.
        try:
            cat = adsb_emitter_category(pmt.to_python(pmt.cdr(msg)))
            if cat and icao:
                self._cat[icao] = cat
        except Exception:
            pass
        cat = self._cat.get(icao)
        if cat:
            meta["emitter_category"], meta["ac_class"] = cat
        try:
            self._q.put_nowait(meta)
        except queue.Full:
            pass


AIN_BUFFER_ITEMS = 1024      # module audio inputs: output buffer per block (GNU Radio's minimum here)


class _AudioInputSource(gr.sync_block):
    """Flowgraph side of an api.AudioInput: what the module wrote, else silence. It runs at the speaker's
    pace (the audio sink pulls through the mixer), so it never blocks on the module."""

    def __init__(self, ain):
        gr.sync_block.__init__(self, f"audio_input_{ain.owner}", in_sig=None, out_sig=[np.float32])
        self._ain = ain

    def work(self, input_items, output_items):
        out = output_items[0]
        out[:] = self._ain.read(len(out))
        return len(out)


class ReplayGate(gr.basic_block):
    """Pause for IQ-file replay. Paused, it stops reading its input -- so the file source stops
    and the play position freezes -- and emits zeros instead, so everything downstream (throttle,
    waterfall, audio, decoders) keeps running on silence instead of stalling the flowgraph."""

    def __init__(self):
        gr.basic_block.__init__(self, name="replay_gate", in_sig=[np.complex64],
                                out_sig=[np.complex64])
        self.paused = False

    def general_work(self, input_items, output_items):
        out = output_items[0]
        if self.paused:
            out[:] = 0
            self.produce(0, len(out))
            return gr.WORK_CALLED_PRODUCE
        n = min(len(input_items[0]), len(out))
        out[:n] = input_items[0][:n]
        self.consume(0, n)
        return n


class SnapshotTap(gr.sync_block):
    """Raw wideband IQ grabber (api.radio.snapshot / snapshot_tap). Idle it
    only counts samples; capture(n) arms it, and the next n contiguous source
    samples are copied out and returned to the (non-flowgraph) caller. So the
    flowgraph never waits on the caller and the copy cost is only paid for
    the ~0.4 s snapshots actually requested (~1/s)."""

    def __init__(self):
        gr.sync_block.__init__(self, name="band_snapshot",
                               in_sig=[np.complex64], out_sig=None)
        import threading
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._buf = None                 # armed: np array being filled
        self._fill = 0

    def capture(self, n, timeout=5.0):
        """Block until n contiguous samples have been captured; None on timeout."""
        self.arm(n)
        return self.collect(timeout)

    def arm(self, n):
        """Start filling a new n-sample snapshot now (replaces any pending one); collect() it later.
        Lets a caller capture the next snapshot while it is still processing this one."""
        with self._lock:
            self._buf = np.empty(int(n), np.complex64)
            self._fill = 0
            self._done.clear()

    def armed(self, n):
        """Whether an n-sample snapshot is pending (being filled or ready)."""
        with self._lock:
            return self._buf is not None and len(self._buf) == int(n)

    def collect(self, timeout=5.0):
        """The armed snapshot once full (blocks up to timeout); None on timeout or if none is armed."""
        if not self._done.wait(timeout):
            with self._lock:
                self._buf = None
            return None
        with self._lock:
            buf, self._buf = self._buf, None
        return buf

    def work(self, input_items, output_items):
        x = input_items[0]
        if self._buf is not None:
            with self._lock:
                b = self._buf
                if b is not None and not self._done.is_set():
                    k = min(len(x), len(b) - self._fill)
                    b[self._fill:self._fill + k] = x[:k]
                    self._fill += k
                    if self._fill >= len(b):
                        self._done.set()
        return len(x)


class RdsMsgSink(gr.sync_block):
    """Message-only sink for gr-rds's ``rds.parser`` 'out' port: it publishes
    ``pmt::make_tuple(type, msg)`` for each decoded field (PI, PS, PTY,
    flags, RadioText, clock, AF -- see ``lib/parser_impl.cc`` in
    bastibl/gr-rds). Feed each update through an ``rds.RdsState`` to merge it
    into the current station's fields and push the merged dict onto
    ``out_queue`` on every actual change. A plain Python message block
    (``in_sig=None, out_sig=None``) — reliable in this GR build, same shape
    as ``AdsbMsgSink`` above."""

    def __init__(self, out_queue):
        gr.sync_block.__init__(self, name="rds_msg_sink", in_sig=None, out_sig=None)
        self._q = out_queue
        self._state = _rdsdec.RdsState()
        self.message_port_register_in(pmt.intern("out"))
        self.set_msg_handler(pmt.intern("out"), self._on_msg)

    def _on_msg(self, msg):
        try:
            type_id = pmt.to_long(pmt.tuple_ref(msg, 0))
            text = pmt.to_python(pmt.tuple_ref(msg, 1))
        except Exception:
            return
        d = self._state.update(type_id, text)
        if d is None:
            return
        try:
            self._q.put_nowait(d)
        except queue.Full:
            pass

    def reset(self):
        """Clear accumulated station state (call on retune)."""
        self._state.reset()


class Mode:
    """Parameters that define one analog demodulation mode. ``kind`` picks the demod chain ("fm", "wfm",
    "am", "ssb", "cw", "silent"); the name is only a label, so a module's mode (api.DemodSpec) is built the same way
    as the core's. ``tones``: FM with the CTCSS squelch / tone decode taps."""

    def __init__(self, name, default_bw, max_dev=0.0, deemph=0.0,
                 has_squelch=True, sideband=None, bw_step=None, pitch=0.0, kind=None, tones=False):
        self.name = name
        self.kind = kind or ("ssb" if sideband else "fm")
        self.tones = bool(tones)
        self.default_bw = default_bw     # nominal channel bandwidth, Hz
        self.max_dev = max_dev           # FM peak deviation, Hz (FM modes)
        self.deemph = deemph             # de-emphasis tau, s (0 = none)
        self.has_squelch = has_squelch
        self.sideband = sideband         # None (symmetric) / "usb" / "lsb"
        # a dragged marker edge snaps to multiples of this (Hz) instead of the standard channel
        # widths (spectrum.BW_SNAPS_HZ): SSB / CW filters are continuously variable
        self.bw_step = bw_step
        self.pitch = pitch               # CW: the beat note (Hz) the carrier is heard at


MODES = {
    # WFM broadcast: wide, demod at 240k then resample /5 to 48k, 75us deemph.
    "WFM": Mode("WFM", default_bw=200_000, max_dev=75_000, deemph=75e-6,
                has_squelch=False, kind="wfm"),
    # Narrowband FM (public safety / business): ~12.5 kHz channel, 5 kHz dev.
    "NFM": Mode("NFM", default_bw=12_500, max_dev=5_000, deemph=75e-6, tones=True),
    # AM (aircraft band etc.): envelope detect, ~8 kHz channel.
    "AM": Mode("AM", default_bw=8_000, kind="am"),
    # SSB (HF voice): suppressed-carrier, one sideband only. default_bw is the
    # width of the passband ABOVE (usb) or BELOW (lsb) the SSB_LOW_CUT_HZ guard
    # against the suppressed carrier — see DemodChain and demod_edges().
    "USB": Mode("USB", default_bw=2_700, sideband="usb", bw_step=1_000),
    "LSB": Mode("LSB", default_bw=2_700, sideband="lsb", bw_step=1_000),
    # CW (Morse): tuned ON the carrier (the marker sits on the signal, the readout is the carrier
    # frequency), a narrow filter around it, then a beat-frequency oscillator shifts it up so the
    # carrier is heard as a CW_PITCH_HZ tone -- see DemodChain.
    "CW": Mode("CW", default_bw=500, bw_step=50, pitch=700.0, kind="cw"),
}


def mode_from_spec(name, spec):
    """A Mode for a module's mode entry from its api.DemodSpec."""
    return Mode(name, default_bw=int(spec.bw), max_dev=float(spec.max_dev), deemph=float(spec.deemph),
                has_squelch=spec.squelch and spec.kind != "silent", sideband=spec.sideband, bw_step=spec.bw_step,
                pitch=float(spec.pitch), kind=spec.kind, tones=spec.tones)

# The core's TX-capable modes (see TxChain): they mirror radio.mode.name (the analog
# RX mode currently tuned). CW and modules' receive modes have no modulator; a module
# adds its own transmit modes (api.TxModeSpec), picked in the main window's "TX:"
# selector independently of the receive mode.
TX_MODES = ("NFM", "AM", "WFM", "USB", "LSB")

# US amateur radio allocations (Hz), as (lo, hi, band name). This is the ONLY
# place transmit is allowed by default: app._start_tx refuses to key a frequency
# outside these ranges unless the operator has ticked the licensed-operator
# override (Settings > Transmit). It covers the HF/VHF/UHF bands this app is used
# on; it is deliberately conservative (band EDGES, not sub-band privilege limits —
# the operator's license class governs where within a band they may transmit).
# Edges are the ITU Region 2 / FCC Part 97 band limits. 60 m is the five discrete
# channels' envelope. These are a safety floor, not a band plan.
US_HAM_BANDS = [
    (1_800_000, 2_000_000, "160 m"),
    (3_500_000, 4_000_000, "80 m"),
    (5_330_500, 5_406_400, "60 m"),      # 5 channels; envelope of the band
    (7_000_000, 7_300_000, "40 m"),
    (10_100_000, 10_150_000, "30 m"),
    (14_000_000, 14_350_000, "20 m"),
    (18_068_000, 18_168_000, "17 m"),
    (21_000_000, 21_450_000, "15 m"),
    (24_890_000, 24_990_000, "12 m"),
    (28_000_000, 29_700_000, "10 m"),
    (50_000_000, 54_000_000, "6 m"),
    (144_000_000, 148_000_000, "2 m"),
    (219_000_000, 220_000_000, "1.25 m (219)"),
    (222_000_000, 225_000_000, "1.25 m"),
    (420_000_000, 450_000_000, "70 cm"),
    (902_000_000, 928_000_000, "33 cm"),
    (1_240_000_000, 1_300_000_000, "23 cm"),
]


def in_us_ham_band(hz):
    """(ok, band_name) — whether ``hz`` falls inside a US amateur allocation
    (see US_HAM_BANDS). band_name is "" when out of band. Band edges are
    inclusive. Used to gate transmit; not a license-class privilege check."""
    hz = float(hz)
    for lo, hi, name in US_HAM_BANDS:
        if lo <= hz <= hi:
            return True, name
    return False, ""


# Conventional US repeater input/output splits per band. The app auto-fills the
# TX offset box from this when the user tunes into a band (overridable). Each
# entry: band name -> (magnitude Hz, [(lo, hi, sign)] sub-band sign rules). The
# SIGN (does the repeater input sit above or below its output?) is set by the
# output sub-band on the bands where the convention flips (2 m, 1.25 m); on the
# others the standard split direction is fixed. Magnitudes/splits are the common
# ARRL band-plan values; the operator can always override in Settings > Transmit.
_STD_OFFSETS = {
    "10 m":        (100_000,     [(0, 1e12, -1)]),            # -100 kHz
    "6 m":         (1_000_000,   [(51_620_000, 51_980_000, -1),
                                  (52_000_000, 54_000_000, -1)]),  # -1 MHz (common)
    "2 m":         (600_000,     [(145_200_000, 145_500_000, -1),   # 145.2-145.5 -> -
                                  (146_610_000, 147_000_000, -1),   # 146.61-147.00 -> -
                                  (147_000_000, 147_400_000, +1)]), # 147.00-147.40 -> +
    "1.25 m":      (1_600_000,   [(0, 1e12, -1)]),            # -1.6 MHz
    "70 cm":       (5_000_000,   [(0, 1e12, -1)]),            # -5 MHz (sign below)
    "33 cm":       (25_000_000,  [(0, 1e12, -1)]),            # -25 MHz
    "23 cm":       (12_000_000,  [(0, 1e12, -1)]),            # -12 MHz
}


def standard_repeater_offset(hz):
    """Signed repeater TX offset (Hz) for a tuned RX (output) frequency ``hz``, by the US band
    convention (see _STD_OFFSETS); 0 if the band has no standard split (simplex) or is out of band.
    This is a convenience default the UI fills on a band change — always operator-overridable."""
    ok, band = in_us_ham_band(hz)
    if not ok or band not in _STD_OFFSETS:
        return 0
    mag, rules = _STD_OFFSETS[band]
    for lo, hi, sign in rules:
        if lo <= hz <= hi:
            return sign * mag
    return 0


# Guard band (Hz) between the suppressed carrier (demod_hz, i.e. 0 Hz at
# baseband after the channelizer) and the near edge of an SSB passband — the
# standard ~300 Hz voice high-pass every SSB rig applies. Mode.default_bw is
# the width of the passband beyond this guard, so USB occupies
# [SSB_LOW_CUT_HZ, SSB_LOW_CUT_HZ + bw] and LSB occupies
# [-(SSB_LOW_CUT_HZ + bw), -SSB_LOW_CUT_HZ], both relative to demod_hz.
SSB_LOW_CUT_HZ = 300

# RDS (WFM broadcast station metadata — see start_rds). Constants and filter/
# loop-tuning values are ported from gr-rds's own reference receiver
# (bastibl/gr-rds, examples/rds_rx.grc) rather than re-derived, since they're
# a working, tuned design. That reference mixes its 57 kHz subcarrier down
# from a composite FM signal and lands on a 19 kHz complex processing rate
# (16 samples/symbol at the 1187.5 bps RDS bit rate) before RRC matched
# filtering / AGC / symbol sync / Costas carrier recovery — start_rds()
# reaches that same 19 kHz rate from QUAD_RATE (240 kHz) in two stages
# (freq_xlating decim 8 -> 30 kHz, then a 19/30 rational resample -> 19 kHz)
# instead of the reference's 320 kHz front end, so every constant past that
# point (RRC alpha/ntaps, AGC gain/reference, symbol_sync/Costas loop
# bandwidths) carries over unchanged.
RDS_SUBCARRIER_HZ = 57_000
RDS_BIT_RATE = 1187.5                # RDS data rate
RDS_PROC_RATE = 19_000               # complex rate feeding the RRC/AGC/symbol-sync stage
RDS_XLATE_DECIM = 8                  # QUAD_RATE (240k) -> 30k
RDS_RESAMP_INTERP = 19               # 30k -> 19k (30_000 * 19 // 30 == RDS_PROC_RATE)
RDS_RESAMP_DECIM = 30

# Standard 50-tone EIA CTCSS set (Hz), for repeater access + RX tone squelch.
CTCSS_TONES = [
    67.0, 69.3, 71.9, 74.4, 77.0, 79.7, 82.5, 85.4, 88.5, 91.5,
    94.8, 97.4, 100.0, 103.5, 107.2, 110.9, 114.8, 118.8, 123.0, 127.3,
    131.8, 136.5, 141.3, 146.2, 151.4, 156.7, 159.8, 162.2, 165.5, 167.9,
    171.3, 173.8, 177.3, 179.9, 183.5, 186.2, 189.9, 192.8, 196.6, 199.5,
    203.5, 206.5, 210.7, 218.1, 225.7, 229.1, 233.6, 241.8, 250.3, 254.1,
]

# Blind CTCSS decode (see DemodChain's NFM branch / ScannerRadio.detected_ctcss):
# a low-rate vector probe polled from Python and analyzed via a single
# real FFT (one np.fft.rfft covering ALL 50 candidate tones at once, vs. 50
# separate per-tone Goertzel passes — ~50x less work per poll for the exact
# same numbers, since a full-spectrum FFT is what you'd get from unrolling
# Goertzel across every bin anyway). 1 kHz is ample margin above the highest
# standard tone (254.1 Hz); the probe vector length sets both frequency
# resolution (~0.5 Hz bins at this rate/length — well under the ~2+ Hz
# spacing between adjacent standard tones) and update latency (~2s —
# CTCSS_PROBE_LEN / CTCSS_PROBE_RATE).
CTCSS_PROBE_RATE = 1_000
CTCSS_PROBE_LEN = 2_048
# Empirically validated (see the loopback test that drove this design): real
# tones scored energy ratios in the hundreds; silence scored ~0. This is a
# wide margin, not a tuned-to-the-edge threshold.
CTCSS_DETECT_RATIO = 5.0
# Precomputed once (not per-poll): the rfft bin nearest each standard tone.
_CTCSS_RFFT_FREQS = np.fft.rfftfreq(CTCSS_PROBE_LEN, d=1.0 / CTCSS_PROBE_RATE)
_CTCSS_BIN_IDX = np.array(
    [int(np.argmin(np.abs(_CTCSS_RFFT_FREQS - hz))) for hz in CTCSS_TONES])


class DemodChain(gr.hier_block2):
    """complex @ QUAD_RATE  ->  float @ AUDIO_RATE, per mode.

    Squelch is NOT done here — it's a software gate in ScannerRadio driven by the
    same probe measurement the UI shows, so the threshold matches the SIG readout
    (a GNU Radio pwr_squelch_cc uses an unrelated dB scale, which is confusing).
    """

    def __init__(self, mode: Mode, bw: int):
        gr.hier_block2.__init__(
            self, f"demod_{mode.name}",
            gr.io_signature(1, 1, gr.sizeof_gr_complex),
            gr.io_signature(1, 1, gr.sizeof_float))
        bw = bw or mode.default_bw
        self.ctcss_squelch = None    # RX tone squelch (NFM only — see below)
        self.ctcss_probe = None      # blind tone decode vector probe (NFM only — see below)

        if mode.kind == "wfm":
            demod = analog.quadrature_demod_cf(
                QUAD_RATE / (2.0 * math.pi * mode.max_dev))
            resamp = gr_filter.rational_resampler_fff(1, STAGE2_DECIM)
            deemph = analog.fm_deemph(AUDIO_RATE, mode.deemph)
            self.connect(self, demod, resamp, deemph, self)
            return

        if mode.kind == "silent":
            # a module's mode that makes its own audio (api.AudioInput) or none: zeros at AUDIO_RATE, paced
            # by the channel like every other mode (no demod, nothing to hear)
            self.connect(self, blocks.keep_one_in_n(gr.sizeof_gr_complex, STAGE2_DECIM),
                         blocks.complex_to_real(1), blocks.multiply_const_ff(0.0), self)
            return

        if mode.kind == "cw":
            # CW: the carrier is at 0 Hz after the channelizer. Decimate to 48k with a loose
            # filter first (cheap at 240k), then the narrow CW filter at 48k (an FFT filter: at
            # 500 Hz it needs >1000 taps), then a BFO -- a rotator shifting the carrier up to
            # mode.pitch -- and .real gives the tone. Upper side only, so a signal just above the
            # carrier is heard higher, one below lower, as on a rig's CW-U. AGC as for SSB.
            pre = firdes.low_pass(1.0, QUAD_RATE, 3_000, 2_000)
            decim = gr_filter.fir_filter_ccf(STAGE2_DECIM, pre)
            half = max(50.0, bw / 2.0)
            narrow = firdes.low_pass(1.0, AUDIO_RATE, half, max(60.0, half / 2.0))
            chfilt = gr_filter.fft_filter_ccf(1, narrow)
            bfo = blocks.rotator_cc(2.0 * math.pi * mode.pitch / AUDIO_RATE)
            to_real = blocks.complex_to_real(1)
            agc = analog.agc2_ff(0.1, 0.01, 0.3, 1.0)
            self.connect(self, decim, chfilt, bfo, to_real, agc, self)
            return

        if mode.kind == "ssb":
            # Phasing-method SSB demod: a complex band-pass FIR picks out only
            # the wanted sideband (one side of the suppressed carrier at 0 Hz
            # baseband) while decimating 240k -> 48k in the same block, then
            # .real recovers the audio directly — no BFO/mixer needed since
            # the channelizer already put the carrier at baseband. AGC stands
            # in for a real rig's RF/AF gain riding the (constant-amplitude-
            # free) SSB envelope, same role blocks.agc2_ff plays for AM below.
            lo, hi = ((SSB_LOW_CUT_HZ, SSB_LOW_CUT_HZ + bw) if mode.sideband == "usb"
                     else (-(SSB_LOW_CUT_HZ + bw), -SSB_LOW_CUT_HZ))
            taps = firdes.complex_band_pass(1.0, QUAD_RATE, lo, hi, SSB_LOW_CUT_HZ)
            chfilt = gr_filter.fir_filter_ccc(STAGE2_DECIM, taps)
            to_real = blocks.complex_to_real(1)
            agc = analog.agc2_ff(0.1, 0.01, 0.3, 1.0)
            self.connect(self, chfilt, to_real, agc, self)
            return

        # FM / AM: narrow channel filter + decimate to 48k, then demodulate.
        cutoff = max(2_500, bw / 2)
        taps = firdes.low_pass(1.0, QUAD_RATE, cutoff, cutoff / 2)
        chfilt = gr_filter.fir_filter_ccf(STAGE2_DECIM, taps)

        if mode.kind == "fm" and not mode.tones:
            # FM discriminator, with de-emphasis only when the mode has one (a data mode's audio,
            # e.g. an FSK warble, has none)
            demod = analog.quadrature_demod_cf(
                AUDIO_RATE / (2.0 * math.pi * mode.max_dev))
            if mode.deemph:
                self.connect(self, chfilt, demod, analog.fm_deemph(AUDIO_RATE, mode.deemph), self)
            else:
                self.connect(self, chfilt, demod, self)
        elif mode.kind == "fm":
            demod = analog.quadrature_demod_cf(
                AUDIO_RATE / (2.0 * math.pi * mode.max_dev))
            deemph = analog.fm_deemph(AUDIO_RATE, mode.deemph)
            self.connect(self, chfilt, demod, deemph, self)
            # RX tone squelch: tap the discriminator BEFORE de-emphasis (CTCSS
            # is sub-audible, 67-254 Hz, and de-emphasis is a treble shelf that
            # leaves it alone either way, but this keeps the tap unambiguous).
            # Always built (negligible cost) so enabling/disabling it is a
            # Python-side flag on ScannerRadio, not a flowgraph rebuild — same
            # as manual/auto power squelch. Its own output is unused; only
            # .unmuted() is polled, so it terminates in a null_sink.
            self.ctcss_squelch = analog.ctcss_squelch_ff(
                AUDIO_RATE, 100.0, 0.01, 0, 0, False)
            self.connect(demod, self.ctcss_squelch, blocks.null_sink(gr.sizeof_float))
            # Blind CTCSS decode: tap the same discriminator point (before
            # de-emphasis) as the squelch above — that one gates on a single
            # USER-SELECTED tone; this is for showing WHICH tone is actually
            # present. Tried running one ctcss_squelch_ff per standard tone
            # first (mirrors the single one above); its built-in selectivity
            # wasn't clean enough to tell same-ballpark standard tones apart
            # running side by side (verified: falsely co-triggered on an
            # unrelated nearby tone in testing). Instead: decimate way down
            # (sub-audible tones top out at 254 Hz, so ~1 kHz is plenty of
            # margin) into a vector probe the UI polls and Goertzel-analyzes
            # in Python (detected_ctcss) — same "periodic vector probe read by
            # a Qt timer" shape the spectrum FFT already uses, and verified
            # against known tones end-to-end (100.0/179.9/67.0 Hz all
            # correctly identified, silence correctly identified as none).
            # NOTE: doesn't decode DCS/DPL — a different digital sub-audible
            # scheme (a 134.8 bps bitstream, not a tone), needing a real
            # correlator this isn't.
            tone_taps = firdes.low_pass(1.0, AUDIO_RATE, 300, 100)
            tone_decim = gr_filter.fir_filter_fff(
                AUDIO_RATE // CTCSS_PROBE_RATE, tone_taps)
            s2v = blocks.stream_to_vector(gr.sizeof_float, CTCSS_PROBE_LEN)
            self.ctcss_probe = blocks.probe_signal_vf(CTCSS_PROBE_LEN)
            self.connect(demod, tone_decim, s2v, self.ctcss_probe)
        elif mode.kind == "am":
            mag = blocks.complex_to_mag(1)
            dcblock = gr_filter.dc_blocker_ff(64, True)   # strip the carrier
            agc = analog.agc2_ff(0.1, 0.01, 0.3, 1.0)
            self.connect(self, chfilt, mag, dcblock, agc, self)
        else:
            raise ValueError(f"unknown demod kind {mode.kind!r} (mode {mode.name})")


class ScannerRadio(gr.top_block):
    """The live flowgraph. One HackRF, one demod chain at a time."""

    def __init__(self, freq_hz=154_000_000, mode_name="NFM",
                 audio_out=True, device="HackRF",
                 samp_rate=SAMP_RATE, use_gpu=None):
        gr.top_block.__init__(self, "sdrexplorer")
        self.device = device or "HackRF"     # a device key, an old fixed name, or IQ_FILE_DEVICE
        self.devinfo = None                  # api.sources.DeviceInfo of the device that's open (or will be)
        self.dev_settings = {}               # its saved settings (sources.settings): gains by stage, ppm, ...
        self.handle = None                   # api.sources.SourceHandle while running from a device
        self.samp_rate = samp_rate if samp_rate in SAMP_RATE_OPTIONS else SAMP_RATE
        self.stage1_decim = self.samp_rate // QUAD_RATE
        self.src = None                 # flowgraph source NODE feeding the pipeline
        self._hw = None                 # osmosdr device handle (None in IQ-file replay)
        self.replay = None              # {"path","meta","loop"} when replaying a file
        self._is_replay = False
        self._replay_blocks = []        # file-source chain, torn down on stop()
        self.replay_kept_vfo = False    # last replay start kept the user's VFO (vs the file's channel)
        self.running = False
        # Two frequencies: the HARDWARE band center (fixed FFT/waterfall center)
        # and the DEMOD frequency (what's actually decoded/played). Tuning inside
        # the visible band moves only the demod (via the channelizer's frequency
        # shift), leaving the waterfall centered where it is. A "primary" retune
        # (set_frequency) recenters the whole band on a target, offset so the
        # target dodges the DC spike.
        self.band_center_hz = int(freq_hz) - OFFSET   # hardware center
        self.demod_hz = int(freq_hz)                  # decoded frequency
        # Gains are strictly per-device: dev_settings["gains"] = {stage: dB}, kept per device key in
        # sources.json. There is no device-agnostic gain here -- a single global triple used to get
        # projected onto whichever device was active (one device's gains applied to the other).
        self._squelch_db = -70.0        # manual absolute squelch level (dB)
        self._auto_squelch = False      # auto = open at floor + margin
        self._auto_margin = 6.0         # dB above the measured noise floor
        self._volume = 0.4
        self._gate_open = True          # software-squelch audio gate
        self._silenced = False          # latched hard-mute for clean shutdown
        self._inputs_muted = False      # modules' audio inputs off (a scan sweeping)
        self._bw_override = None        # dragged bandwidth (Hz); None = mode default
        self.adsb_q = None              # queue.Queue of decoded aircraft dicts (ADS-B)
        self._adsb_blocks = []          # gr-adsb decode branch edges (or empty)
        self._adsb_msgedges = []        # gr-adsb message-port connections
        self._adsb_sink = None          # AdsbMsgSink (PDU -> adsb_q)
        self._adsb_framer = None        # gr-adsb framer (for live threshold tuning)
        self._adsb_threshold = 0.010    # framer power threshold (gr-adsb default)
        self.rds_q = None               # queue.Queue of decoded RDS field dicts (WFM)
        self._rds_blocks = []           # parallel gr-rds decode branch edges (or empty)
        self._rds_msgedges = []         # gr-rds message-port connections
        self._rds_sink = None           # RdsMsgSink ((type,msg) pmt -> rds_q)
        self._adsb_suspended = []       # analog edges detached while ADS-B runs
        self.snapshot_tap = None        # SnapshotTap while a module keeps it attached (api.radio.snapshot_tap)
        # module tap points (api.taps): blocks modules attach to the source / the channel
        self.taps = Taps(self)

        # The osmosdr source is NOT built here — it's created in start() for the
        # selected device (so construction never opens/needs hardware, and Stop
        # can fully release the device). Everything downstream of the source is
        # built once below and stays; start()/stop() only (dis)connect the source.
        # use_gpu=None means auto (GPU if the OOT module built/loaded); passing
        # False forces the CPU path even when CUDA is available (--no-gpu).
        self.use_gpu = CUDA_AVAILABLE if use_gpu is None else (bool(use_gpu) and CUDA_AVAILABLE)
        self._build_channelizer()

        self.vol = blocks.multiply_const_ff(self._volume)
        self.audio_error = ""           # why there is no speaker (no sound device), shown by the app
        if audio_out:
            try:
                self.audio_sink = audio.sink(AUDIO_RATE, "", True)
            except RuntimeError as e:   # no sound device / audio server: run silent rather than not at all
                self.audio_error = str(e) or "no audio output device"
                print(f"sdrexplorer: no audio output ({self.audio_error}): running without sound",
                      file=sys.stderr)
                self.audio_sink = blocks.null_sink(gr.sizeof_float)
        else:                       # headless: no ALSA device required
            self.audio_sink = blocks.null_sink(gr.sizeof_float)

        self.extra_modes = {}           # module modes (register_mode), beside MODES
        self.mode = MODES[mode_name]
        self.demod = DemodChain(self.mode, 0)
        self.connect(self.chan, self.demod, self.vol, self.audio_sink)
        self._ain = []                  # (api.AudioInput, source, resampler, gain, mixer port): module audio

        # IQ recorder tap on the raw source (full 2.4 Msps band centered at
        # band_center_hz). Written as INTERLEAVED SIGNED INT16 (scale 32767)
        # because that's what common SDR tools' IQ-file sources read natively
        # (iq_size=2, iq_signed=True; many have no float32 path). The file_sink is
        # constructed pointed at /dev/null then closed, so it discards input
        # until start_record() opens a real file — no stray file, no idle writes.
        self._recording = False
        self._rec_scope = "band"
        self._channels = []            # extra channel handles (open_channel)
        self._lock_depth = 0           # nested lock() calls (GNU Radio's lock nests too)
        self._last_unlock = 0.0        # monotonic time of the last reconfiguration (see RECONFIG_GAP_S)
        self.on_channels_closed = None   # callback([handles]) when the radio closes them
        self._subband_blocks = None    # (filt, conv, sink) while a subband recording runs
        self._subband_meta = None      # {"center_hz", "bw_hz", "samp_rate"} for record_meta()
        self.iq_conv = blocks.complex_to_interleaved_short(False, 32767.0)
        self.iq_sink = blocks.file_sink(gr.sizeof_short, os.devnull, False)
        self.iq_sink.set_unbuffered(False)
        self.iq_sink.close()
        self.connect(self.iq_conv, self.iq_sink)

        # narrowband recorder tap on the channelizer output: just the demod
        # window (the tuned channel shifted to baseband, QUAD_RATE wide), same
        # cs16 format. Fed off self.chan in parallel with the demod chain.
        # retroactive snapshot buffer: the last snapshot_seconds of the same
        # cs16 stream, kept in RAM (iqring.py). Sized per sample rate in start();
        # 0 s disables it (Settings ▸ IQ snapshot…).
        self.snapshot_seconds = SNAPSHOT_SECONDS
        self.iq_ring = _iqring.IQRing()
        self.connect(self.iq_conv, self.iq_ring)

        self.chan_iq_conv = blocks.complex_to_interleaved_short(False, 32767.0)
        self.chan_iq_sink = blocks.file_sink(gr.sizeof_short, os.devnull, False)
        self.chan_iq_sink.set_unbuffered(False)
        self.chan_iq_sink.close()
        self.connect(self.chan, self.chan_iq_conv, self.chan_iq_sink)

    def _make_logpwrfft(self, avg_alpha, average):
        """Log-power FFT probe: GPU (cuFFT, gr-sdrexplorer_gpu) when available,
        else GNU Radio's stock logpwrfft_c. Same math either way (blackman-harris
        window, single-pole IIR average over linear power, nlog10 scaling), so
        probe.level() is a drop-in NFFT-length vector regardless of which ran."""
        if self.use_gpu:
            try:
                return _sdrexplorer_gpu.logpwrfft_cuda(
                    self.samp_rate, NFFT, 2, 30, avg_alpha, average, True)
            except Exception:
                pass
        return logpwrfft.logpwrfft_c(
            self.samp_rate, NFFT, 2, 30, avg_alpha, average, None, True)

    # -- capture bandwidth -----------------------------------------------------
    def _build_channelizer(self):
        """(Re)build the stage-1 channelizer (self.chan: shift +OFFSET to
        baseband, decimate self.samp_rate -> QUAD_RATE) and both log-power FFT
        probes, at the current self.samp_rate/self.stage1_decim. Does NOT wire
        self.chan to anything downstream — the caller connects/reconnects those
        edges (they depend on blocks built elsewhere: self.demod,
        self.chan_iq_conv)."""
        taps = firdes.low_pass(1.0, self.samp_rate, 100_000, 50_000)
        if self.use_gpu:
            try:
                self.chan = _sdrexplorer_gpu.multichannel_ddc_cuda(
                    decim_factor=self.stage1_decim,
                    sample_rate=float(self.samp_rate),
                    channel_freqs=[float(OFFSET)],
                    taps=taps,
                    device_id=0
                )
            except Exception:
                self.use_gpu = False
                self.chan = gr_filter.freq_xlating_fir_filter_ccf(
                    self.stage1_decim, taps, OFFSET, self.samp_rate)
        else:
            self.chan = gr_filter.freq_xlating_fir_filter_ccf(
                self.stage1_decim, taps, OFFSET, self.samp_rate)

        # DISPLAY probe: a running-average FFT (avg_alpha 0.7) for a smooth
        # spectrum/waterfall. NOT used for scan decisions — the average blends
        # across retunes, so during a sweep it still carries the previous
        # (frequency-adjacent) channel's energy for several frames.
        self.lpf = self._make_logpwrfft(0.7, True)
        self.probe = blocks.probe_signal_vf(NFFT)
        self.connect(self.lpf, self.probe)

        # MEASUREMENT probe: INSTANTANEOUS (average=False) FFT off the same
        # source, read by channel_power(). No IIR memory, so once the pipeline
        # has flushed after a retune each read reflects ONLY the current band —
        # this is what stops the scanner from reading a lingering adjacent
        # channel as if it were the one it just tuned to.
        self.lpf_meas = self._make_logpwrfft(1.0, False)
        self.probe_meas = blocks.probe_signal_vf(NFFT)
        self.connect(self.lpf_meas, self.probe_meas)

    def _rewire_chan_branches(self, old_chan):
        """After _build_channelizer() replaces self.chan, re-point any active
        parallel decode branch's first edge (old self.chan -> its own
        discriminator/filter) at the new object. set_samp_rate is the only
        caller that replaces self.chan while RDS may already be
        running off it — without this, their stored edge lists still
        reference the now-dead old channelizer object, which GNU Radio
        rejects at the next start() (an unconnected mandatory input on a
        block still registered in the graph), taking the whole flowgraph —
        and the SDR claim — down with it."""
        for edges in (self._rds_blocks,):
            if edges and edges[0][0] is old_chan:
                target = edges[0][1]
                self.disconnect(old_chan, target)
                self.connect(self.chan, target)
                edges[0] = (self.chan, target)

    def set_samp_rate(self, rate):
        """Change the capture bandwidth (Hz), one of SAMP_RATE_OPTIONS. Rebuilds
        the stage-1 channelizer and both FFT probes; stops the flowgraph first
        if running (the caller is responsible for restarting it — mirrors
        set_device()). Returns True if the rate was applied."""
        rate = int(rate)
        if rate not in SAMP_RATE_OPTIONS:
            return False
        if rate == self.samp_rate:
            return True
        if self.running:
            self.stop()
        # Every fixed-rate consumer of the channelizer output: these run at
        # QUAD_RATE regardless of capture bandwidth, so they're kept and just
        # re-pointed at the new self.chan. Missing one here leaves it wired to
        # the dead old channelizer, which then has no input and start() fails
        # ("insufficient connected input ports") -- shown in the UI only as
        # "HackRF: not connected". (Modules' blocks: Taps.channel_rebuilt below.)
        chan_consumers = [self.demod, self.chan_iq_conv]
        chan_consumers = [b for b in chan_consumers if b is not None]
        for b in chan_consumers:
            self.disconnect(self.chan, b)
        self.disconnect(self.lpf, self.probe)
        self.disconnect(self.lpf_meas, self.probe_meas)
        old_chan = self.chan
        self.samp_rate = rate
        self.stage1_decim = self.samp_rate // QUAD_RATE
        self._build_channelizer()
        self._rewire_chan_branches(old_chan)
        self.taps.channel_rebuilt(old_chan, self.chan)
        for b in chan_consumers:
            self.connect(self.chan, b)
        return True

    # -- tuning --------------------------------------------------------------
    @property
    def band_center_hz(self):
        """Hardware band center. Every move marks the snapshot ring, so a
        snapshot only holds samples of the band it is labeled with."""
        return self._band_center_hz

    @band_center_hz.setter
    def band_center_hz(self, hz):
        hz = int(hz)
        ring = getattr(self, "iq_ring", None)
        moved = hz != getattr(self, "_band_center_hz", None)
        if ring is not None and moved:
            ring.mark(hz)
        self._band_center_hz = hz
        if moved and getattr(self, "_channels", None):
            self._close_all_channels()          # their offsets now point at other frequencies

    @property
    def freq_hz(self):
        """The frequency being demodulated (what the UI shows/plays)."""
        return self.demod_hz

    def current_bw(self):
        """Effective demod bandwidth (Hz) — a dragged override if set, else the
        mode's nominal default. Drives the marker width, squelch measurement
        window, and tunable span."""
        return self._bw_override or self.mode.default_bw

    def demod_edges(self):
        """(lo_hz, hi_hz) offsets from demod_hz spanning the current passband
        — one-sided for SSB (matching DemodChain's band-pass, see
        SSB_LOW_CUT_HZ), symmetric ± bw/2 for every other mode. Used by the
        spectrum widget to draw/hit-test the demod marker correctly."""
        bw = self.current_bw()
        if self.mode.sideband == "usb":
            return SSB_LOW_CUT_HZ, SSB_LOW_CUT_HZ + bw
        if self.mode.sideband == "lsb":
            return -(SSB_LOW_CUT_HZ + bw), -SSB_LOW_CUT_HZ
        return -bw / 2, bw / 2

    def _update_chan_freq(self, offset):
        if self.use_gpu and hasattr(self.chan, "set_channel_freqs"):
            self.chan.set_channel_freqs([float(offset)])
        else:
            self.chan.set_center_freq(offset)
        for fn in list(getattr(self, "retune_hooks", ())):   # api.radio.on_retune
            try:
                fn(offset)
            except Exception:
                import traceback
                traceback.print_exc()

    def set_frequency(self, hz):
        """Primary retune: recenter the band on ``hz`` (offset so it dodges the
        DC spike) and demod it. Moves the waterfall."""
        if self._is_replay:
            # a file's center is fixed; "retune" just moves the demod within it
            self.set_demod(int(hz))
            return
        hz = int(hz)
        self.demod_hz = hz
        self.band_center_hz = hz - OFFSET
        if self.handle is not None:
            self.handle.set_center(self.band_center_hz)
        self._update_chan_freq(OFFSET)          # target sits at +OFFSET

    def demod_span(self):
        """Half-width (Hz) of the tunable range around the band center — the
        whole captured band minus just enough for the channel to fit inside it,
        so the tunable range matches the displayed waterfall (mode-aware)."""
        guard = self.current_bw() / 2 + 3_000
        return self.samp_rate / 2 - guard

    def set_demod(self, hz):
        """Tune the demod to ``hz`` WITHIN the current band by shifting the
        channelizer — the hardware/waterfall stay put. Clamped only by how much
        room the channel needs at the band edge. Returns the clamped freq."""
        half = self.demod_span()
        lo = self.band_center_hz - half
        hi = self.band_center_hz + half
        hz = int(max(lo, min(hi, hz)))
        self.demod_hz = hz
        self._update_chan_freq(hz - self.band_center_hz)
        return hz

    def retune_only(self, hz):
        """Move the hardware band center for a transient power reading (scan /
        discover sweep). Updates band_center so the probe FFT stays labeled
        correctly; leaves the demod frequency (and self.chan, and everything
        downstream of it — audio, modules' decoders) untouched, so it goes stale for the
        duration of the sweep like the audio path already does."""
        if self._is_replay:
            return                       # no hardware to sweep during file replay
        self.band_center_hz = int(hz) - OFFSET
        if self.handle is not None:
            self.handle.set_center(self.band_center_hz)

    # -- mode ----------------------------------------------------------------
    def set_mode(self, name, bw=0):
        """``name``: a core mode (MODES) or a module's mode registered with register_mode()."""
        mode = MODES.get(name) or self.extra_modes.get(name)
        if mode is None:
            return
        self._bw_override = None            # a mode change resets to the default bw
        with self._reconfig():
            self.disconnect(self.chan, self.demod)
            self.disconnect(self.demod, self.vol)
            self.mode = mode
            self.demod = DemodChain(self.mode, bw)
            self.connect(self.chan, self.demod, self.vol)

    def set_bandwidth(self, bw):
        """Apply a dragged demod bandwidth (Hz): record the override (so the
        marker/measurement/span follow it) and rebuild the demod filter. Rebuild
        is under lock() like set_mode, so call this on drag-END, not per pixel.
        WFM ignores the filter width, so there it only affects marker/span."""
        self._bw_override = int(bw)
        with self._reconfig():
            self.disconnect(self.chan, self.demod)
            self.disconnect(self.demod, self.vol)
            self.demod = DemodChain(self.mode, self._bw_override)
            self.connect(self.chan, self.demod, self.vol)

    def register_mode(self, mode):
        """A module's analog mode (radio.mode_from_spec), selectable with set_mode()."""
        if mode.name in MODES:
            raise ValueError(f"{mode.name} is a core mode")
        self.extra_modes[mode.name] = mode

    # -- module audio inputs (api.AudioInput) --------------------------------------------------
    # The speaker chain is chan -> demod -> vol -> audio_sink. A module's audio is added after vol,
    # through its own gain (= the volume, so one knob sets both), as soon as the first input opens.
    def _mixer(self):
        if getattr(self, "_mix", None) is None:
            self._mix = blocks.add_ff()
            self._mix_ports = 1                 # port 0: the analog path
            with self._reconfig():
                self.disconnect(self.vol, self.audio_sink)
                self.connect(self.vol, (self._mix, 0))
                self.connect(self._mix, self.audio_sink)
        return self._mix

    def attach_audio_input(self, ain):
        """Connect an input into the mixer. A closed input's chain is reused (ports never pile up): at the
        same rate its source is handed to the new input; at another rate the old chain is disconnected and
        the new one takes its mixer port."""
        mix = self._mixer()
        gain_k = 0.0 if (self._silenced or self._inputs_muted) else self._volume
        old = next((e for e in self._ain if e[0].closed), None)
        if old is not None and old[0].rate == ain.rate:
            _, src, rs, gain, port = old
            src._ain = ain
            gain.set_k(gain_k)
            self._ain[self._ain.index(old)] = (ain, src, rs, gain, port)
            ain.block = src
            return
        src = _AudioInputSource(ain)
        q = Fraction(AUDIO_RATE, ain.rate)
        rs = (gr_filter.rational_resampler_fff(q.numerator, q.denominator)
              if q != 1 else blocks.copy(gr.sizeof_float))
        gain = blocks.multiply_const_ff(gain_k)
        # The source never waits (it writes silence when the module has written nothing), so it runs
        # ahead of the speaker by however much buffer it is given: GNU Radio's default (~8k items) is a
        # second of silence queued in front of anything the module writes. Small buffers keep the
        # latency ~0.15 s.
        for b in (src, rs, gain):
            b.set_max_output_buffer(AIN_BUFFER_ITEMS)
        with self._reconfig():
            if old is not None:
                port = old[4]
                self.disconnect(old[1], old[2], old[3], (mix, port))
                self._ain.remove(old)
            else:
                port = self._mix_ports
                self._mix_ports += 1
            self.connect(src, rs, gain, (mix, port))
        ain.block = src
        self._ain.append((ain, src, rs, gain, port))

    def detach_audio_input(self, ain):
        """The input closed: its chain stays connected, silent, until the next input reuses it."""
        for a, src, rs, gain, port in self._ain:
            if a is ain:
                gain.set_k(0.0)

    def _apply_input_gain(self):
        off = self._silenced or getattr(self, "_inputs_muted", False)
        for a, src, rs, gain, port in getattr(self, "_ain", []):
            gain.set_k(0.0 if (off or a.closed) else self._volume)

    def set_inputs_muted(self, muted):
        """Silence every module audio input (api.AudioInput) without touching the analog path: a scan
        sweeping past channels shouldn't play what a decoder makes of each one."""
        self._inputs_muted = bool(muted)
        self._apply_input_gain()

    # -- RDS parallel decode branch (gr-rds, WFM station metadata) -----------
    def start_rds(self):
        """Build a parallel 57 kHz-subcarrier decode chain off the
        channelizer for WFM's RDS station metadata (PS/RadioText/PTY/...).
        Own quadrature_demod_cf (composite FM @ 240k, independent of the main
        DemodChain, so the audio path is untouched),
        then the front end ported from gr-rds's reference receiver (57 kHz
        mixdown -> 19 kHz complex -> RRC/Manchester matched filter -> AGC ->
        symbol sync -> BPSK Costas loop -> differential decode — see the
        RDS_* constants above), handing the sliced bitstream to gr-rds's
        rds.decoder (block sync + CRC correction) -> rds.parser (group
        parsing) -> RdsMsgSink, which pushes merged station-field dicts onto
        self.rds_q."""
        if not RDS_AVAILABLE or self._rds_blocks:
            return
        self.rds_q = queue.Queue(maxsize=256)
        disc = analog.quadrature_demod_cf(QUAD_RATE / (2.0 * math.pi * 75_000))
        xlate = gr_filter.freq_xlating_fir_filter_fcc(
            RDS_XLATE_DECIM, firdes.low_pass(1.0, QUAD_RATE, 7_500, 5_000),
            RDS_SUBCARRIER_HZ, QUAD_RATE)                       # 240k float -> 30k cplx
        resamp = gr_filter.rational_resampler_ccc(
            RDS_RESAMP_INTERP, RDS_RESAMP_DECIM)                # 30k -> 19k cplx
        rrc = firdes.root_raised_cosine(
            1.0, RDS_PROC_RATE, RDS_PROC_RATE / 8.0, 1.0, 151)
        # Matched filter for the biphase/Manchester subcarrier waveform: an
        # RRC pulse minus itself delayed by one half-bit (8 samples at 16
        # sps) — ported directly from gr-rds's rrc_taps_manchester.
        mf_taps = [rrc[n] - rrc[n + 8] for n in range(len(rrc) - 8)]
        mf = gr_filter.fir_filter_ccc(1, mf_taps)
        try:
            agc = analog.agc_cc(2e-3, 0.585, 53.0, 1000.0)
        except TypeError:                     # GNU Radio < 3.10.6 (Debian 12): no max_gain argument
            agc = analog.agc_cc(2e-3, 0.585, 53.0)
            if hasattr(agc, "set_max_gain"):
                agc.set_max_gain(1000.0)
        bpsk = digital.constellation_bpsk().base()
        sync = digital.symbol_sync_cc(
            digital.TED_ZERO_CROSSING, RDS_PROC_RATE / RDS_BIT_RATE, 0.01, 1.0,
            1.0, 0.1, 1, bpsk, digital.IR_MMSE_8TAP, 128, [])
        costas = digital.constellation_receiver_cb(
            bpsk, 2.0 * math.pi / 100, -0.002, 0.002)
        diff = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        decoder = _rds.decoder(False, False)
        parser = _rds.parser(False, False, 0)
        sink = RdsMsgSink(self.rds_q)
        # constellation_receiver_cb's ports 1-3 (error/phase/freq debug, float)
        # and port 4 (recovered complex symbol) aren't needed — terminate them.
        null_f = blocks.null_sink(gr.sizeof_float)
        null_c = blocks.null_sink(gr.sizeof_gr_complex)
        edges = [(self.chan, disc), (disc, xlate), (xlate, resamp),
                 (resamp, mf), (mf, agc), (agc, sync), (sync, costas),
                 (costas, diff), (diff, decoder),
                 ((costas, 1), (null_f, 0)), ((costas, 2), (null_f, 1)),
                 ((costas, 3), (null_f, 2)), ((costas, 4), null_c)]
        with self._reconfig():
            for a, b in edges:
                self.connect(a, b)
            self.msg_connect(decoder, pmt.intern("out"), parser, pmt.intern("in"))
            self.msg_connect(parser, pmt.intern("out"), sink, pmt.intern("out"))
        self._rds_blocks = edges           # keep block refs alive + exact edges
        self._rds_msgedges = [(decoder, "out", parser, "in"),
                              (parser, "out", sink, "out")]
        self._rds_sink = sink

    def stop_rds(self):
        """Tear down the RDS decode branch (leaves the audio path untouched)."""
        if not self._rds_blocks:
            return
        with self._reconfig():
            for a, tp, b, bp in reversed(self._rds_msgedges):
                try:
                    self.msg_disconnect(a, pmt.intern(tp), b, pmt.intern(bp))
                except Exception:
                    pass
            for a, b in reversed(self._rds_blocks):
                try:
                    self.disconnect(a, b)
                except Exception:
                    pass
        self._rds_blocks = []
        self._rds_msgedges = []
        self._rds_sink = None
        self.rds_q = None

    # -- ADS-B decode branch (gr-adsb, off the raw 2.4 Msps source) ----------
    def start_adsb(self):
        """Build the gr-adsb decode chain off the raw source and run it. Chain
        (matches gr-adsb's example + a HackRF DC fix):
          src → resample 2.4→2.0 Msps → dc_blocker (kill HackRF DC spike)
              → complex_to_mag_squared (POWER — gr-adsb's threshold domain)
              → framer → demod →(msg)→ decoder →(msg)→ AdsbMsgSink → adsb_q.
        Relies on the OFFSET-tune (band center = 1090 − OFFSET, so 1090 sits at
        +OFFSET): that keeps the signal OFF the dc_blocker's DC notch while the
        blocker removes just the HackRF spike; magnitude demod is offset-insensitive
        so the pulse envelope is unaffected. Also rests the analog chain and the
        modules' channel branches (useless at 1090) to free CPU. Requires the radio running."""
        if self._adsb_blocks or not ADSB_AVAILABLE or not self.running:
            return
        self.set_frequency(ADSB_FREQ)            # offset-tune: 1090 at +OFFSET
        self._bw_override = ADSB_RATE            # marker spans the ~2 MHz processed band
        self.adsb_q = queue.Queue(maxsize=256)
        # resample self.samp_rate -> ADSB_RATE (gr-adsb needs exactly 2.0 Msps,
        # fs % 1 Msym == 0). Ratio must track the selected capture bandwidth —
        # a fixed 5/6 (2.4M->2.0M) only happened to be correct at the default
        # rate; at any other SAMP_RATE_OPTIONS choice it silently resampled to
        # the wrong rate and gr-adsb would decode nothing.
        g = math.gcd(self.samp_rate, ADSB_RATE)
        rs = gr_filter.rational_resampler_ccc(
            interpolation=ADSB_RATE // g, decimation=self.samp_rate // g)
        dcb = gr_filter.dc_blocker_cc(64, True)  # remove the HackRF DC spike (at DC)
        mag = blocks.complex_to_mag_squared(1)   # power; gr-adsb's framer threshold domain
        framer = _adsb.framer(int(ADSB_RATE), self._adsb_threshold)
        demod = _adsb.demod(int(ADSB_RATE))
        demod_sink = blocks.null_sink(gr.sizeof_float)   # demod's stream output
        # msg_filter MUST be exactly "All Messages" or "Extended Squitter Only" —
        # gr-adsb's decoder gates every parity branch on that literal, so any other
        # value (e.g. "All") silently decodes nothing. "Conservative" repairs 1-bit
        # CRC errors; print_level non-"Brief" avoids gr-adsb's curses UI.
        decoder = _adsb.decoder("All Messages", "Conservative", "None")
        self._adsb_framer = framer
        self._adsb_sink = AdsbMsgSink(self.adsb_q)
        stream_edges = [(self.src, rs), (rs, dcb), (dcb, mag), (mag, framer),
                        (framer, demod), (demod, demod_sink)]
        # SUSPEND everything ADS-B doesn't need, to free CPU and stop the
        # audio-sink underruns: the whole analog audio chain
        # (chan→demod→vol→audio_sink) and every module branch on the channel
        # tap (taps.suspend). Every listed edge is fully removed so no block is
        # left half-connected (which GR rejects on unlock). chan itself stays
        # (it feeds the probes/recorder).
        mix = getattr(self, "_mix", None)
        suspend = [(self.chan, self.demod), (self.demod, self.vol),
                   (self.vol, (mix, 0)) if mix is not None else (self.vol, self.audio_sink)]
        # With modules' audio inputs the speaker path ends in a mixer (add_ff) whose port 0 is the analog
        # audio. GNU Radio refuses to restart a graph with an unconnected input, so while ADS-B rests the
        # analog chain, port 0 is fed silence instead (paced by the audio sink, like the other ports).
        filler = [(blocks.null_source(gr.sizeof_float), (mix, 0))] if mix is not None else []
        msg_edges = [(demod, "demodulated", decoder, "demodulated"),
                     (decoder, "decoded", self._adsb_sink, "decoded")]
        done = []                               # (undo function) for a rollback
        self.lock()
        try:
            for a, b in suspend:
                try:
                    self.disconnect(a, b)
                    done.append(lambda a=a, b=b: self.connect(a, b))
                except Exception:
                    pass
            self.taps.suspend(TAP_CHANNEL)          # modules' channel branches rest too
            done.append(lambda: self.taps.resume(TAP_CHANNEL))
            for a, b in filler + stream_edges:
                self.connect(a, b)
                done.append(lambda a=a, b=b: self.disconnect(a, b))
            for a, ap, b, bp in msg_edges:
                self.msg_connect(a, pmt.intern(ap), b, pmt.intern(bp))
                done.append(lambda a=a, ap=ap, b=b, bp=bp:
                            self.msg_disconnect(a, pmt.intern(ap), b, pmt.intern(bp)))
            self.unlock()
        except Exception:
            # Put the graph back as it was and restart it: a graph left locked (or stopped by a failed
            # unlock, which has already dropped GNU Radio's lock count) never runs or stops again, and
            # the app hangs on the next stop / quit. So: (re)lock, undo, unlock.
            if not self._lock_depth:
                self.lock()
            for undo in reversed(done):
                try:
                    undo()
                except Exception:
                    pass
            try:
                self.unlock()
            except Exception:
                pass
            self.adsb_q = None
            self._adsb_sink = None
            self._adsb_framer = None
            raise
        self._adsb_suspended = suspend
        self._adsb_filler = filler
        # gr-adsb's decoder ctor calls logging.basicConfig(DEBUG); quiet it so it
        # doesn't flood stderr (we consume the message port, not its logging).
        import logging
        logging.getLogger().setLevel(logging.WARNING)
        self._adsb_blocks = stream_edges
        self._adsb_msgedges = msg_edges

    def stop_adsb(self):
        """Tear down the gr-adsb decode branch (leaves the source untouched)."""
        if not self._adsb_blocks:
            return
        with self._reconfig():
            for a, tp, b, bp in reversed(getattr(self, "_adsb_msgedges", [])):
                try:
                    self.msg_disconnect(a, pmt.intern(tp), b, pmt.intern(bp))
                except Exception:
                    pass
            for a, b in reversed(self._adsb_blocks):
                try:
                    self.disconnect(a, b)
                except Exception:
                    pass
            for a, b in getattr(self, "_adsb_filler", []):      # the silence on the mixer's analog port
                try:
                    self.disconnect(a, b)
                except Exception:
                    pass
            for a, b in getattr(self, "_adsb_suspended", []):   # restore the audio chain
                try:
                    self.connect(a, b)
                except Exception:
                    pass
            self.taps.resume(TAP_CHANNEL)
        self._adsb_suspended = []
        self._adsb_filler = []
        self._adsb_blocks = []
        self._adsb_msgedges = []
        self._adsb_sink = None
        self._adsb_framer = None
        self.adsb_q = None

    def set_adsb_threshold(self, v):
        """Live-adjust the gr-adsb framer's power detection threshold (it's very
        gain-dependent, like gr-adsb's own tunable). Persists for the next start."""
        self._adsb_threshold = float(v)
        if self._adsb_framer is not None:
            try:
                self._adsb_framer.set_threshold(float(v))
            except Exception:
                pass


    # -- SDR source lifecycle (start/stop/select device) ---------------------
    def _source_targets(self):
        t = [self.chan, self.lpf, self.lpf_meas, self.iq_conv]
        if self.snapshot_tap is not None:
            t.append(self.snapshot_tap)
        return t + self.taps.source_targets()

    # -- band snapshot tap (api.radio.snapshot_tap) ------------------------------
    def enable_snapshot_tap(self, on):
        """Attach/detach the SnapshotTap on the raw source. Only present while a
        module keeps it on, so it costs nothing otherwise. Survives stop()/
        start() via _source_targets()."""
        if on and self.snapshot_tap is None:
            tap = SnapshotTap()
            if self.running:
                self.lock()
                try:                           # a failed connect must not leave the flowgraph locked
                    self.connect(self.src, tap)
                finally:
                    self.unlock()
            self.snapshot_tap = tap
        elif not on and self.snapshot_tap is not None:
            tap, self.snapshot_tap = self.snapshot_tap, None
            if self.running:
                self.lock()
                try:
                    self.disconnect(self.src, tap)
                finally:
                    self.unlock()

    def start(self):
        """Wire the source (an osmosdr device, or a file-replay chain when the
        device is 'IQ file') to every downstream tap and run the flowgraph.
        Raises if a hardware device isn't present/openable, or a replay file is
        missing (caller shows the error)."""
        if self.running:
            return
        if self.device == IQ_FILE_DEVICE:
            self._is_replay = True
            self.src = self._build_replay_source()
        else:
            self._is_replay = False
            from . import sources as _sources
            dev = _sources.find(self.device)
            if dev is None:
                raise RuntimeError(f"{self.device}: no supported devices found (not connected?)")
            self.devinfo = dev
            st = dict(self.dev_settings)
            st.update(rate=self.samp_rate, center_hz=self.band_center_hz,
                      gains=dict(st.get("gains") or {}) or self._default_gains(dev))
            self.handle = _sources.open_device(dev, st)
            self._hw = self.handle.block
            self.src = self.handle.block
        connected = []
        try:
            for t in self._source_targets():
                self.connect(self.src, t)
                connected.append(t)
            # point the channelizer at the tuned channel within the (fixed) band
            self._update_chan_freq(self.demod_hz - self.band_center_hz)
            gr.top_block.start(self)
        except Exception:
            # Undo the half-done start. stop() returns early when not running,
            # so without this the source edges (and any replay chain) stay in
            # the graph and every later start() fails with "destination already
            # in use", which the UI shows as "not connected" until restarted.
            for t in connected:
                try:
                    self.disconnect(self.src, t)
                except Exception:
                    pass
            for a, b in zip(self._replay_blocks, self._replay_blocks[1:]):
                try:
                    self.disconnect(a, b)
                except Exception:
                    pass
            self._replay_blocks = []
            self.src = None
            self._hw = None             # drops the device handle -> releases the radio
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            self._is_replay = False
            raise
        # (re)size the snapshot ring for this run's rate; a new run starts it empty
        self._configure_ring()
        self.started_at = time.time()       # sample 0's time, live (api.station.sample_time)
        self.running = True
        # Re-apply the saved per-stage gains to the LIVE handle now that the flowgraph is streaming.
        # gr-osmosdr (HackRF especially: the RF amp 0/14) often drops gains set before the stream is
        # running -- open_device() applied them pre-start, but this is the authoritative apply, so
        # signals are at the saved gains without the user toggling a slider.
        self._apply_gains_live()

    def _in_recorded_band(self, hz, center, frate, meta):
        """Whether a demod frequency lies inside the file's recorded content: its rf_bw for a
        sub-band capture, else its sample rate (limited by the current capture rate it is
        replayed at). The VFO only has to be inside it -- not a whole channel's width from the
        edge: a 9-20 kHz window around one channel is narrower than the channel plus a margin,
        so that rule always failed for such files and every (re)start snapped the VFO back to
        the recorded channel."""
        width = float(meta.get("rf_bw_hz") or frate)
        width = min(width, float(self.samp_rate))
        return abs(int(hz) - int(center)) <= width / 2

    def _build_replay_source(self):
        """File-replay source chain: cs16 file -> complex @ self.samp_rate
        (resampled if the capture rate differs) -> throttle (real-time). Sets
        band_center / demod from the sidecar so the channelizer + decoders land on
        the recording's channel. Returns the terminal block to wire as the
        source."""
        r = self.replay or {}
        path = r.get("path")
        if not path or not os.path.exists(path):
            raise RuntimeError("no IQ file selected" if not path
                               else f"IQ file not found: {path}")
        meta = r.get("meta") or {}
        # float, not int: sub-band captures record a fractional rate (e.g.
        # 2.4 Msps / 55 = 43636.36...), and truncating it breaks the ratio below
        frate = float(meta.get("samp_rate", self.samp_rate)) or self.samp_rate
        center = int(meta.get("center_hz", self.band_center_hz))
        chan_hz = int(meta.get("chan_hz", center))
        self.band_center_hz = center            # the file's band is fixed: the waterfall must follow it
        # Keep the user's VFO (what they tuned / clicked on the waterfall) when it lies inside the
        # recorded content -- re-playing a file to demodulate a signal they just picked must not jump
        # back to the channel it happened to be recorded on. Only a VFO outside the recording falls
        # back to the file's channel (e.g. first load after tuning the HackRF somewhere else).
        self.replay_kept_vfo = self._in_recorded_band(self.demod_hz, center, frate, meta)
        if not self.replay_kept_vfo:
            self.demod_hz = chan_hz
        # Read WHOLE I/Q pairs (one 4-byte item = int16 I + int16 Q), then
        # split to a 2-vector of shorts for the converter. Reading single
        # shorts let every lock()/unlock() (mode change, a decoder rebuild, ...)
        # drop an odd number of them, after which each "sample" paired Q of
        # sample n with I of n+1 -- a garbage signal with strong mirror images.
        fsrc = blocks.file_source(2 * gr.sizeof_short, str(path), bool(r.get("loop")))
        # play position bookkeeping (replay_position_s): items played = base + the source's
        # output count, which restarts at 0 on every lock()/unlock() while the file carries on
        self._replay_rate = frate
        self._replay_items = os.path.getsize(path) // (2 * gr.sizeof_short)
        start = int(r.pop("start_item", 0) or 0)
        if 0 < start < self._replay_items:
            fsrc.seek(start, os.SEEK_SET)
        else:
            start = 0
        self._replay_base, self._replay_last_n = start, 0
        s2c = blocks.interleaved_short_to_complex(True)   # vector input: 2 shorts / item
        scale = blocks.multiply_const_cc(1.0 / 32768.0)   # cs16 was written *32767
        self.connect(fsrc, s2c, scale)
        tail = scale
        chain = [fsrc, s2c, scale]
        if abs(frate - self.samp_rate) > 1:               # bring it up to samp_rate
            # Reduce to a small interp/decim pair. The exact gcd of a truncated
            # fractional rate (43636 vs 2.4M -> 600000/10909) makes
            # rational_resampler design a ~20M-tap filter that runs at ~1x real
            # time, starving the waterfall and audio. Sub-band rates are
            # samp_rate/decim at record time, so a denominator cap recovers the
            # true ratio exactly (55/1, 21/1, 7/2 ...).
            q = Fraction(self.samp_rate / frate).limit_denominator(1000)
            rs = gr_filter.rational_resampler_ccc(
                interpolation=q.numerator, decimation=q.denominator)
            self.connect(tail, rs)
            tail = rs
            chain.append(rs)
        gate = ReplayGate()                               # pause (keeps the position)
        gate.paused = bool(r.get("paused"))
        self._replay_gate = gate
        thr = blocks.throttle(gr.sizeof_gr_complex, self.samp_rate)
        self.connect(tail, gate, thr)
        chain += [gate, thr]
        self._replay_blocks = chain
        return thr

    def set_replay(self, path, meta=None, loop=False):
        """Select IQ-file replay of ``path`` (with its .json ``meta``). Stops any
        running source; the caller then start()s. Returns True."""
        if self.running:
            self.stop()
        self.replay = {"path": str(path), "meta": dict(meta or {}), "loop": bool(loop)}
        self.device = IQ_FILE_DEVICE
        return True

    def set_replay_loop(self, loop):
        if self.replay is not None:
            self.replay["loop"] = bool(loop)

    def is_replay(self):
        return self._is_replay

    # -- replay position / seek (the playback bar) ----------------------------
    def replay_duration_s(self):
        """Length of the selected IQ file in seconds (None if no file / no rate)."""
        r = self.replay or {}
        path = r.get("path")
        if not path or not os.path.exists(path):
            return None
        rate = float((r.get("meta") or {}).get("samp_rate", self.samp_rate)) or self.samp_rate
        return os.path.getsize(path) / (2 * gr.sizeof_short) / rate

    def replay_position_s(self):
        """Seconds into the file now playing (None when not replaying). The file source's
        output count restarts at 0 on every lock()/unlock(), so a drop in it is folded into
        _replay_base -- call this regularly (the app polls it a few times a second). Reads a
        few blocks of buffering ahead of what is heard, a fraction of a second."""
        if not (self.running and self._is_replay and self._replay_blocks):
            r = self.replay or {}
            if "start_item" in r:                    # parked by a seek while stopped
                rate = float((r.get("meta") or {}).get("samp_rate", self.samp_rate)) or self.samp_rate
                return r["start_item"] / rate
            return None
        try:
            n = int(self._replay_blocks[0].nitems_written(0))
        except RuntimeError:
            n = self._replay_last_n
        if n < self._replay_last_n:                 # count restarted (lock/unlock)
            self._replay_base += self._replay_last_n
        self._replay_last_n = n
        items = self._replay_base + n
        if (self.replay or {}).get("loop") and self._replay_items:
            items %= self._replay_items
        else:
            items = min(items, self._replay_items)
        return items / self._replay_rate

    def set_replay_paused(self, paused):
        """Pause / resume an IQ-file replay in place (no flowgraph rebuild). Kept across a
        stop()/start() of the same file."""
        if self.replay is None:
            return False
        self.replay["paused"] = bool(paused)
        gate = getattr(self, "_replay_gate", None)
        if gate is not None and self.running and self._is_replay:
            gate.paused = bool(paused)
        self._replay_progress = None               # a pause must not look like EOF
        return True

    def replay_paused(self):
        return bool((self.replay or {}).get("paused"))

    def seek_replay(self, seconds):
        """Jump the IQ-file replay to ``seconds`` (clamped to the file). While playing the
        file source seeks in place; while stopped the position is kept for the next start().
        Marks the snapshot ring, since samples before and after a seek aren't contiguous."""
        dur = self.replay_duration_s()
        if dur is None or self.replay is None:
            return False
        rate = float((self.replay.get("meta") or {}).get("samp_rate", self.samp_rate)) or self.samp_rate
        item = int(max(0.0, min(float(seconds), dur)) * rate)
        if self.running and self._is_replay and self._replay_blocks:
            item = min(item, max(0, self._replay_items - 1))
            fsrc = self._replay_blocks[0]
            if not fsrc.seek(item, os.SEEK_SET):
                return False
            try:
                n = int(fsrc.nitems_written(0))
            except RuntimeError:
                n = self._replay_last_n
            self._replay_base, self._replay_last_n = item - n, n
            self._replay_progress = None             # EOF detection restarts from here
            self.iq_ring.mark(self.band_center_hz)
        else:
            self.replay["start_item"] = item
        return True

    REPLAY_STALL_S = 1.0     # file source idle this long (not looping) => EOF

    def replay_finished(self):
        """True once a NON-looping file replay has run out of file. The
        flowgraph doesn't stop by itself at EOF (the audio sink, probes and
        decoder branches keep it alive), so the app polls this to flip
        Start/Stop. Detected as the file source's output count no longer
        advancing -- comparing it to the file size doesn't work, because every
        lock()/unlock() (mode change, bandwidth drag, a snapshot tap) restarts
        the count from 0 while the file position carries on."""
        if not (self.running and self._is_replay and self._replay_blocks):
            self._replay_progress = None
            return False
        if (self.replay or {}).get("loop") or (self.replay or {}).get("paused"):
            self._replay_progress = None             # paused: stalled on purpose, not EOF
            return False
        try:
            n = self._replay_blocks[0].nitems_written(0)
        except RuntimeError:
            return False
        now = time.monotonic()
        last = getattr(self, "_replay_progress", None)
        if last is None or n != last[0]:
            self._replay_progress = (n, now)       # still moving (or counter reset)
            return False
        return now - last[1] >= self.REPLAY_STALL_S

    def lock(self):
        """gr.top_block.lock, held back until RECONFIG_GAP_S has passed since the last reconfiguration while a
        device streams (nested locks aren't delayed: only the outermost one restarts the graph)."""
        depth = getattr(self, "_lock_depth", 0)
        if depth == 0 and getattr(self, "running", False) and not getattr(self, "_is_replay", False):
            wait = getattr(self, "_last_unlock", 0.0) + RECONFIG_GAP_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._lock_depth = depth + 1
        self._impl.lock()                    # (gr.top_block forwards lock() to its C++ impl; there's no method)

    def unlock(self):
        try:
            gr.top_block.unlock(self)
        finally:
            self._lock_depth = max(0, getattr(self, "_lock_depth", 1) - 1)
            if self._lock_depth == 0:
                self._last_unlock = time.monotonic()

    @contextlib.contextmanager
    def _reconfig(self):
        """lock() ... unlock() around a reconfiguration, with the unlock guaranteed: a flowgraph left
        locked by a failed connect / disconnect never runs again, and the next stop() or quit waits on it
        forever (GNU Radio's wait() blocks while the lock count is above zero)."""
        self.lock()
        try:
            yield
        finally:
            self.unlock()

    def stop(self):
        """Stop the flowgraph and fully release the device (disconnect + drop the
        source), so another app/device can use it."""
        if not self.running:
            return
        self.stop_adsb()          # ADS-B taps self.src, which we're about to drop
        self._close_all_channels()        # they tap self.src too
        if self._recording:               # a sub-band recorder hangs off self.src: never leave it behind
            self.stop_record()            # (the app ends a recording itself first; this is the safety net)
        gr.top_block.stop(self)
        gr.top_block.wait(self)
        if self.src is not None:
            for t in self._source_targets():
                try:
                    self.disconnect(self.src, t)
                except Exception:
                    pass
        # tear down the file-replay chain so a later start() rebuilds cleanly
        for a, b in zip(self._replay_blocks, self._replay_blocks[1:]):
            try:
                self.disconnect(a, b)
            except Exception:
                pass
        self._replay_blocks = []
        self.src = None
        self._hw = None
        if self.handle is not None:
            self.handle.close()
            self.handle = None
        self._is_replay = False
        self.running = False

    def wait(self):
        if self.running:
            gr.top_block.wait(self)

    def set_device(self, name, settings=None):
        """Select a source: a device key (sources.devices()), an old fixed name ("HackRF", "RTL-SDR"),
        or IQ_FILE_DEVICE. Stops first if running; the caller restarts. ``settings``: that device's saved
        settings (gains by stage, ppm, antenna). Returns True if it's known."""
        if name != IQ_FILE_DEVICE:
            from . import sources as _sources
            if _sources.find(name) is None and name not in SDR_DEVICES:
                return False
        if self.running:
            self.stop()
        self.device = name
        self.dev_settings = dict(settings or {})
        # keep devinfo pointing at the SELECTED device even before it's started, so caps() (and the
        # gains dialog built from it) reflects this device, not whichever one was last open
        if name == IQ_FILE_DEVICE:
            self.devinfo = None
        else:
            from . import sources as _sources
            self.devinfo = _sources.find(name)
        return True

    def _default_gains(self, dev):
        """Per-device starting gains for a device with nothing saved yet: each stage at its driver
        default (GainStage.default), else the stage's lower bound. No cross-device values."""
        try:
            from . import sources as _sources
            stages = _sources.caps(dev).gains
        except Exception:
            stages = []
        out = {}
        for g in stages:
            out[g.name] = int(g.default if g.default is not None else g.lo)
        return out

    def _apply_gains_live(self):
        """Push the saved per-stage gains to the running handle. Called after the flowgraph starts, so
        gr-osmosdr honors them (it drops some gains set before the stream runs). Only stages the device
        reports are set -- an unknown name routes to the overall gain and corrupts it (see sources.open)."""
        if self.handle is None:
            return
        try:
            valid = {g.name for g in self.caps().gains} if self.caps() is not None else None
        except Exception:
            valid = None
        for stage, db in (self.dev_settings.get("gains") or {}).items():
            if valid is None or stage in valid:
                self.handle.set_gain(stage, db)

    def set_gain(self, stage, db):
        """A gain stage by the name the device reports (Caps.gains). The first change on a device starts
        from the values it was opened with, so its saved gains always hold every stage."""
        g = dict(self.dev_settings.get("gains") or {})
        if not g and self.devinfo is not None:
            g = self._default_gains(self.devinfo)
        g[stage] = db
        self.dev_settings["gains"] = g
        if self.handle is not None:
            self.handle.set_gain(stage, db)

    def set_freq_corr(self, ppm):
        self.dev_settings["ppm"] = float(ppm)
        if self.handle is not None:
            self.handle.set_freq_corr(ppm)

    def set_antenna(self, name):
        self.dev_settings["antenna"] = name
        if self.handle is not None:
            self.handle.set_antenna(name)

    def caps(self):
        """The current device's api.sources.Caps (None for an IQ file / no device)."""
        if self.device == IQ_FILE_DEVICE:
            return None
        from . import sources as _sources
        dev = self.devinfo or _sources.find(self.device)
        return _sources.caps(dev, self.handle) if dev is not None else None


    # -- audio (volume + software squelch gate) ------------------------------
    def _apply_audio(self):
        if self._silenced:              # latched shutdown mute wins over gate
            self.vol.set_k(0.0)
            return
        self.vol.set_k(self._volume if self._gate_open else 0.0)

    def set_volume(self, frac):
        self._volume = float(frac)
        self._apply_audio()
        self._apply_input_gain()

    def set_gate(self, open_):
        """Open/close the audio gate (software squelch, driven by the UI's
        channel-power poll so the threshold matches the SIG readout)."""
        self._gate_open = bool(open_)
        self._apply_audio()

    def mute(self):
        self.set_gate(False)

    def unmute(self):
        self.set_gate(True)

    def silence_audio(self):
        """Hard-mute every audio path at once — the analog volume multiplier and
        the modules' audio inputs — independent of the squelch gate and volume.
        For a clean shutdown: called first thing in closeEvent so no static burst
        escapes as the ADS-B branch reconnects the analog chain and the flowgraph
        stops. Deliberately does NOT go through _apply_audio (which respects the
        gate), so a stray gate/volume change can't un-silence it mid-teardown."""
        self._silenced = True           # latch: _apply_audio/_gain now force 0
        try:
            self.vol.set_k(0.0)
        except Exception:
            pass
        self._apply_input_gain()

    def set_squelch(self, db):
        self._squelch_db = float(db)     # manual absolute level (dB)

    def set_auto_squelch(self, on):
        self._auto_squelch = bool(on)

    def set_auto_margin(self, db):
        self._auto_margin = float(db)

    def squelch_open(self, sig, floor):
        """Decide if a channel breaks squelch. Auto mode tracks the measured
        noise floor (open at floor + margin), so it adapts to each band; manual
        mode uses an absolute level. Used by BOTH the parked mute and the
        scanner so they agree."""
        if self._auto_squelch:
            return (sig - floor) >= self._auto_margin
        return sig >= self._squelch_db

    def set_ctcss_freq(self, hz):
        """Retune the live RX tone-squelch block (NFM only — a no-op, and
        harmless, on any other mode where DemodChain didn't build one)."""
        sq = getattr(self.demod, "ctcss_squelch", None)
        if sq is not None:
            sq.set_frequency(float(hz))

    def ctcss_unmuted(self):
        """True (not blocking) when there's no CTCSS block for the current
        mode; otherwise whether the selected tone is currently present."""
        sq = getattr(self.demod, "ctcss_squelch", None)
        return True if sq is None else bool(sq.unmuted())

    def detected_ctcss(self):
        """The CTCSS tone (Hz) currently present in the RX audio, as a
        single-element list, or [] — blind decode, independent of
        set_ctcss_freq's selected tone. Empty outside NFM, when nothing's
        actually being received, or when whatever's present isn't a standard
        EIA tone (see CTCSS_TONES; DCS/DPL isn't decoded here at all)."""
        probe = getattr(self.demod, "ctcss_probe", None)
        if probe is None:
            return []
        data = np.asarray(probe.level(), dtype=np.float64)
        if data.size < CTCSS_PROBE_LEN or not np.isfinite(data).any():
            return []
        spec = np.abs(np.fft.rfft(data)) ** 2
        # Time-domain energy, NOT spec.sum() — a real FFT's bin-power total is
        # a Parseval-scaled (~N/2x) multiple of it, and CTCSS_DETECT_RATIO was
        # calibrated against the time-domain figure (matches the single-bin
        # Goertzel power scale directly, no extra factor to account for).
        total = float(np.sum(data * data)) or 1e-12
        tone_power = spec[_CTCSS_BIN_IDX]
        i = int(np.argmax(tone_power))
        return [CTCSS_TONES[i]] if (tone_power[i] / total) > CTCSS_DETECT_RATIO else []

    def set_band_center(self, hz):
        """Tune the hardware band center directly (used by block scanning to
        measure many channels from one tune). Does not touch the demod freq."""
        if self._is_replay:
            return                       # a file's band center is fixed
        self.band_center_hz = int(hz)
        if self.handle is not None:
            self.handle.set_center(self.band_center_hz)

    # -- IQ recording --------------------------------------------------------
    def start_record(self, path, scope="band", center_hz=None, bw_hz=None):
        """Begin writing raw IQ (interleaved int16) to ``path``.

        scope='band'    -> the full captured band (2.4 Msps, centered at
                           band_center_hz) — the whole spectrum, replayable.
        scope='demod'   -> just the demod window (the tuned channel at baseband,
                           QUAD_RATE = 240 kHz) — a small file for narrowband
                           offline analysis of the channel.
        scope='subband' -> an arbitrary user-selected sub-band of the captured
                           band (``center_hz`` ± ``bw_hz``/2, up to
                           MAX_SELECTION_BW_HZ) — for e.g. capturing a
                           hopping range wider than the demod window without
                           recording the whole band. Built on demand (unlike
                           the other two, this tap doesn't pre-exist) off
                           ``self.src``.
        Returns the capture metadata for the sidecar / replay."""
        if scope == "subband":
            offset = int(center_hz) - self.band_center_hz
            if abs(offset) + bw_hz / 2 > self.samp_rate / 2:
                raise ValueError("selection is outside the captured band")
            decim = max(1, int(self.samp_rate / (bw_hz * 1.4)))
            taps = firdes.low_pass(1.0, self.samp_rate, bw_hz / 2,
                                    max(5_000, bw_hz * 0.15))
            filt = gr_filter.freq_xlating_fir_filter_ccf(
                decim, taps, offset, self.samp_rate)
            conv = blocks.complex_to_interleaved_short(False, 32767.0)
            sink = blocks.file_sink(gr.sizeof_short, str(path), False)
            sink.set_unbuffered(False)
            with self._reconfig():
                self.connect(self.src, filt, conv, sink)
            self._subband_blocks = (self.src, filt, conv, sink)    # with the source it hangs off
            self._subband_meta = {
                "center_hz": int(center_hz), "bw_hz": int(bw_hz),
                "samp_rate": self.samp_rate / decim,
            }
            self._rec_scope = "subband"
        else:
            self._rec_scope = "demod" if scope == "demod" else "band"
            sink = self.chan_iq_sink if self._rec_scope == "demod" else self.iq_sink
            sink.open(str(path))
        self._rec_start_time = time.time()
        self._recording = True
        return self.record_meta()

    def stop_record(self):
        if not self._recording:
            return
        if self._rec_scope == "subband" and self._subband_blocks is not None:
            self._detach_subband()
        else:
            sink = self.chan_iq_sink if self._rec_scope == "demod" else self.iq_sink
            sink.close()
        self._recording = False

    def _detach_subband(self):
        """Close the sub-band recorder and take its chain out of the flowgraph, from the source it was
        attached to (which may no longer be self.src) -- under lock() only while the graph runs."""
        src, filt, conv, sink = self._subband_blocks
        self._subband_blocks = None
        sink.close()
        try:
            if self.running:
                with self._reconfig():
                    self.disconnect(src, filt, conv, sink)
            else:
                self.disconnect(src, filt, conv, sink)
        except Exception:
            pass                                # already gone with its source: nothing left to remove

    def is_recording(self):
        return self._recording

    # -- extra channels (api.radio.open_channel): several narrow channels at once
    def snapshot_iq(self, n, timeout=5.0):
        """The next ``n`` raw band samples (api.radio.snapshot), via the snapshot tap: attached for the
        call if it isn't already (a module that snapshots every cycle keeps it attached)."""
        if not self.running:
            return None
        temp = self.snapshot_tap is None
        if temp:
            self.enable_snapshot_tap(True)
        try:
            return self.snapshot_tap.capture(int(n), timeout=timeout)
        finally:
            if temp:
                self.enable_snapshot_tap(False)

    def open_channel(self, path, center_hz, rate=None):
        """Record one channel at ``center_hz`` (anywhere in the captured band) to ``path`` as cs16
        at CHANNEL_RATE (48 kHz) by default, independent of the demod VFO and of start_record. Several can run
        at once. Two stages like the demod path -- the stage-1 channelizer filter to QUAD_RATE, then a
        5x low-pass -- because start_record's single-stage sub-band filter is ~9000 taps at 19.2 Msps,
        too heavy to run several of.

        Always on the CPU, even when the GPU module is in use for the main channelizer: a
        multichannel_ddc_cuda per recorder ran at only 0.5x real time at 19.2 Msps (a GPU round trip
        per small work() call), so two of them dragged the whole flowgraph down to ~56 % and the
        decoders on the tuned channel starved while several ran. The CPU filter runs ~12x
        real time and costs ~16 % of one core per recorder. Returns a handle for close_channel;
        raises ValueError outside the band."""
        rate = int(rate or CHANNEL_RATE)
        if QUAD_RATE % rate:
            raise ValueError(f"channel rate must divide {QUAD_RATE} Hz")
        offset = int(center_hz) - self.band_center_hz
        if abs(offset) + rate / 2 > self.samp_rate / 2 or self.src is None:
            raise ValueError("channel is outside the captured band")
        taps = firdes.low_pass(1.0, self.samp_rate, 100_000, 50_000)
        stage1 = gr_filter.freq_xlating_fir_filter_ccf(self.stage1_decim, taps, offset, self.samp_rate)
        stage2 = gr_filter.fir_filter_ccf(
            QUAD_RATE // rate,
            firdes.low_pass(1.0, QUAD_RATE, rate * 0.42, rate * 0.08))
        conv = blocks.complex_to_interleaved_short(False, 32767.0)
        sink = blocks.file_sink(gr.sizeof_short, str(path), False)
        sink.set_unbuffered(True)          # the caller may read the file back while it grows
        chain = (self.src, stage1, stage2, conv, sink)
        self.lock()
        try:
            self.connect(*chain)
        finally:
            self.unlock()
        handle = {"chain": chain, "path": str(path), "center_hz": int(center_hz), "rate": rate,
                  "band_center_hz": self.band_center_hz, "t0": time.monotonic()}
        self._channels.append(handle)
        return handle

    def close_channel(self, handle):
        """Stop one channel (idempotent). The file stays on disk."""
        if handle not in self._channels:
            return
        self._channels.remove(handle)
        src, stage1, stage2, conv, sink = handle["chain"]
        sink.close()
        if self.running and src is self.src:
            self.lock()
            try:
                self.disconnect(src, stage1, stage2, conv, sink)
            finally:
                self.unlock()

    def channels(self):
        return list(self._channels)

    def _close_all_channels(self):
        """The band moved or the source is going away: every channel's offset is stale."""
        stopped = list(self._channels)
        for h in stopped:
            self.close_channel(h)
        if stopped and self.on_channels_closed is not None:
            self.on_channels_closed(stopped)

    # -- retroactive IQ snapshot (iqring.py) -----------------------------------
    def set_snapshot_seconds(self, seconds):
        """Resize the snapshot buffer (0 = off). Applies at once if running."""
        self.snapshot_seconds = max(0.0, float(seconds))
        if self.running:
            self._configure_ring()

    def _configure_ring(self):
        # snapshot_seconds before the click, plus the SNAPSHOT_POST_S after it
        sec = self.snapshot_seconds + SNAPSHOT_POST_S if self.snapshot_seconds > 0 else 0
        self.iq_ring.configure(self.samp_rate, sec, self.band_center_hz)

    def snapshot_buffered_s(self):
        return self.iq_ring.buffered_seconds() if self.running else 0.0

    def take_snapshot(self, post_s=SNAPSHOT_POST_S, pre_s=None):
        """Freeze the snapshot buffer: blocks for up to ``post_s`` (call it off
        the GUI thread) and returns (int16 IQ, meta) or (None, None). meta is a
        band-scope sidecar (as record_meta) plus trigger_offset_s, the time in
        the file at which the user asked for the snapshot. ``pre_s`` keeps only
        that many seconds before the call (the recording trigger's pre-roll)."""
        iq, m = self.iq_ring.snapshot(post_s, pre_s)
        if iq is None:
            return None, None
        return iq, {
            "center_hz": int(m["center_hz"]),
            "chan_hz": int(self.demod_hz),
            "samp_rate": m["samp_rate"],
            "format": "cs16", "iq_size": 2, "iq_signed": True,
            "mode": self.mode.name,
            "scope": "band",
            # the ring's last sample is "now" (the post-trigger time has already passed)
            "start_time": round(time.time() - m["duration_s"], 3),
            "snapshot": {"trigger_offset_s": round(m["trigger_offset_s"], 3),
                         "duration_s": round(m["duration_s"], 3)},
        }

    def subband_recording_range(self):
        """(center_hz, bw_hz) of an active sub-band recording, or None — lets
        the waterfall keep showing the selection while it's being captured."""
        if self._recording and self._rec_scope == "subband" and self._subband_meta:
            m = self._subband_meta
            return m["center_hz"], m["bw_hz"]
        return None

    def record_meta(self):
        """Params describing the recording, for the sidecar + offline replay."""
        if self._rec_scope == "subband":
            m = self._subband_meta
            return {
                "center_hz": m["center_hz"],
                "chan_hz": int(self.demod_hz),
                "samp_rate": m["samp_rate"],
                "rf_bw_hz": m["bw_hz"],       # requested width (samp_rate is oversampled)
                "format": "cs16", "iq_size": 2, "iq_signed": True,
                "mode": self.mode.name,
                "scope": "subband",
                "start_time": round(getattr(self, "_rec_start_time", time.time()), 3),
            }
        demod = self._rec_scope == "demod"
        return {
            # for a band capture the file is centered at the hardware center; for
            # a demod capture it's centered on the tuned channel (baseband).
            "center_hz": int(self.demod_hz if demod else self.band_center_hz),
            "chan_hz": int(self.demod_hz),           # tuned/demod frequency
            "samp_rate": QUAD_RATE if demod else self.samp_rate,
            "format": "cs16",                        # interleaved int16
            "iq_size": 2, "iq_signed": True,
            "mode": self.mode.name,
            "scope": "demod" if demod else "band",
            "start_time": round(getattr(self, "_rec_start_time", time.time()), 3),
        }

    # -- power measurement for scanning --------------------------------------
    def channel_power(self, freq, bw):
        """Return (signal_dB, noise_floor_dB) for the channel at absolute
        ``freq``, located within the CURRENT probe FFT window (source at
        band_center_hz). Works whether ``freq`` is the demod freq or any other
        channel inside the captured band — which is what lets the scanner read
        many channels from a single hardware tune.
        """
        level = np.asarray(self.probe_meas.level(), dtype=np.float64)
        if level.size != NFFT:
            return -200.0, -200.0
        center = self.band_center_hz
        bins = center + (np.arange(NFFT) / NFFT - 0.5) * self.samp_rate
        bw = bw or self.mode.default_bw
        sig = np.abs(bins - freq) < bw / 2.0
        if not sig.any():                   # narrower than one FFT bin (CW, wide capture rates):
            sig[int(np.argmin(np.abs(bins - freq)))] = True     # the bin holding the frequency
        dc = np.abs(bins - center) < DC_GUARD
        ref = (~sig) & (~dc) & (np.abs(bins - freq) < 700_000)
        p = float(level[sig].mean()) if sig.any() else -200.0
        floor = float(np.median(level[ref])) if ref.sum() >= 3 else -200.0
        return p, floor

    def window_level(self, center_hz, bw_hz):
        """Peak signal level (dB over the noise floor) inside a band window, for the recording
        trigger, or None if the window isn't in the captured band / no FFT yet. Unlike
        channel_power's mean, the peak (of a 3-bin running mean, to keep single-bin noise
        spikes down) catches a narrow signal anywhere in a wide window. The floor is the median
        of the bins around the window, which a signal filling the window doesn't raise."""
        level = np.asarray(self.probe_meas.level(), dtype=np.float64)
        if level.size != NFFT or not np.isfinite(level).any():
            return None
        center = self.band_center_hz
        half_band = self.samp_rate / 2
        if abs(center_hz - center) + bw_hz / 2 > half_band:
            return None
        bins = center + (np.arange(NFFT) / NFFT - 0.5) * self.samp_rate
        dc = np.abs(bins - center) < DC_GUARD
        sig = (np.abs(bins - center_hz) <= max(bw_hz / 2, self.samp_rate / NFFT)) & ~dc
        around = np.abs(bins - center_hz) < bw_hz / 2 + max(200_000.0, bw_hz)
        ref = around & ~sig & ~dc & (np.abs(bins - center) < 0.9 * half_band)
        if not sig.any() or ref.sum() < 8:
            return None
        sm = np.convolve(level, np.ones(3) / 3, "same")
        return float(sm[sig].max() - np.median(level[ref]))


class TxChain(gr.top_block):
    """One-shot transmit flowgraph: audio (mic or wav file) -> mode-specific
    modulator -> osmosdr.sink (see TX_MODES for the supported modes). NFM
    uses analog.nbfm_tx, WFM uses analog.wfm_tx; AM builds a real-valued
    complex baseband (carrier + scaled audio) that's a DSB AM signal once
    transmitted; USB/LSB use a phasing-method SSB modulator (Hilbert
    transform, conjugated for the lower sideband); a module's transmit mode
    (api.TxModeSpec, ``tx_spec``) brings its own modulator. All of these produce a complex baseband
    that IS the final RF signal once the
    sink transmits it centered at freq_hz — no separate BFO/mixer needed,
    mirroring how DemodChain never needs one on RX either. Built fresh per
    PTT press, torn down on release (or file EOF) — HackRF is half-duplex, so
    a TxChain only ever runs while the paired ScannerRadio's source is
    stopped (see app.py's PTT handling)."""

    def __init__(self, freq_hz, source="mic", tx_gains=(0, 20), device="HackRF",
                 file_path=None, loop=False, ctcss_hz=0.0, mode="NFM", tx_spec=None):
        gr.top_block.__init__(self, "sdrexplorer_tx")
        if source not in ("mic", "file"):
            raise ValueError(f"unknown TX source {source!r}")
        if source == "file" and not file_path:
            raise ValueError("file TX source needs a file_path")
        if tx_spec is not None:              # a module's mode (api.TxModeSpec)
            mode = tx_spec.name
        elif mode not in TX_MODES:
            raise ValueError(f"unknown TX mode {mode!r}")
        self.tx_spec = tx_spec
        self.source = source
        self.mode = mode
        self.freq_hz = int(freq_hz)
        self.device = device if device != IQ_FILE_DEVICE else "HackRF"
        self.running = False
        rf_amp, vga_gain = (tx_gains if not isinstance(tx_gains, dict) else (0, 0))

        if source == "mic":
            tail = audio.source(AUDIO_RATE, "", True)
        else:
            tail = blocks.wavfile_source(str(file_path), bool(loop))
            frate = tail.sample_rate()
            if frate != AUDIO_RATE:            # resample to AUDIO_RATE, same
                g = math.gcd(frate, AUDIO_RATE)  # gcd-ratio approach as the
                rs = gr_filter.rational_resampler_fff(   # IQ-file replay path
                    interpolation=AUDIO_RATE // g, decimation=frate // g)
                self.connect(tail, rs)
                tail = rs

        if ctcss_hz and mode == "NFM":
            # Mix the sub-audible tone into the audio before it hits nbfm_tx —
            # the same point a repeater's own transmit audio chain sums it at.
            # nbfm_tx's internal preemphasis is a treble shelf; 67-254 Hz passes
            # through essentially flat, so this is safe even though the tone
            # rides through that stage along with the voice/file audio.
            # CTCSS has no meaning on SSB, so this only ever applies to NFM.
            tone = analog.ctcss_gen_f(AUDIO_RATE, float(ctcss_hz))
            adder = blocks.add_ff()
            self.connect(tail, (adder, 0))
            self.connect(tone, (adder, 1))
            tail = adder

        if tx_spec is not None:
            # the module's modulator: float audio @ AUDIO_RATE in, complex @ QUAD_RATE (api.TX_RATE) out
            self.modulator = tx_spec.build(self, tail, dict(tx_spec.settings))
            if self.modulator is None:
                raise RuntimeError(f"{mode}: the modulator wasn't built")
        elif mode in ("USB", "LSB"):
            # Hilbert transform turns the real audio into its analytic signal
            # (energy at positive frequencies only); at demod_hz that IS the
            # upper sideband once transmitted, and conjugating it mirrors the
            # spectrum to the lower sideband instead. ntaps=129 gives a clean
            # transition around SSB_LOW_CUT_HZ at AUDIO_RATE.
            hilbert = gr_filter.hilbert_fc(129)
            self.connect(tail, hilbert)
            tail = hilbert
            if mode == "LSB":
                conj = blocks.conjugate_cc()
                self.connect(tail, conj)
                tail = conj
            # AUDIO_RATE -> QUAD_RATE (x5), same staged interpolation nbfm_tx
            # uses below (see TX_SAMP_RATE comment for why not one big jump).
            self.modulator = gr_filter.rational_resampler_ccc(
                interpolation=QUAD_RATE // AUDIO_RATE, decimation=1)
            self.connect(tail, self.modulator)
        elif mode == "AM":
            # AM: a REAL-valued complex baseband (imaginary left at 0) —
            # once the sink transmits it at freq_hz, that's exactly
            # AM_CARRIER_AMPLITUDE * (1 + m*audio(t)) * cos(2*pi*freq_hz*t),
            # a double-sideband AM signal with a carrier, which is what
            # DemodChain's AM path (envelope detect) expects to receive.
            # AM_MOD_INDEX < 1 keeps the envelope from going negative
            # (overmodulation); AM_CARRIER_AMPLITUDE < 1 keeps its PEAK
            # (carrier + full modulation) under the sink's unity full scale
            # — carrying the carrier at full scale (1.0) and adding
            # modulation on top of that clips the TX DAC, which is exactly
            # what was producing "static": harmonic splatter from clipping,
            # not receiver noise.
            scale = blocks.multiply_const_ff(AM_CARRIER_AMPLITUDE * AM_MOD_INDEX)
            carrier = blocks.add_const_ff(AM_CARRIER_AMPLITUDE)
            f2c = blocks.float_to_complex(1)   # port 1 (imag) left unconnected -> 0
            self.connect(tail, scale, carrier)
            self.connect(carrier, (f2c, 0))
            self.modulator = gr_filter.rational_resampler_ccc(
                interpolation=QUAD_RATE // AUDIO_RATE, decimation=1)
            self.connect(f2c, self.modulator)
        elif mode == "WFM":
            # WFM mod params match MODES["WFM"] (75 kHz deviation, 75us
            # preemphasis) so TX and RX agree, same idea as the NFM branch
            # below just with analog.wfm_tx's broadcast-FM parameters.
            wfm = MODES["WFM"]
            self.modulator = analog.wfm_tx(AUDIO_RATE, QUAD_RATE, wfm.deemph, wfm.max_dev)
            self.connect(tail, self.modulator)
        else:
            # NFM mod params match MODES["NFM"] so TX and RX agree on
            # deviation/de-emphasis. nbfm_tx modulates to QUAD_RATE (240k,
            # interp x5 — see TX_SAMP_RATE comment above for why not straight
            # to TX_SAMP_RATE).
            nfm = MODES["NFM"]
            self.modulator = analog.nbfm_tx(AUDIO_RATE, QUAD_RATE, nfm.deemph, nfm.max_dev)
            self.connect(tail, self.modulator)

        # second interpolation stage brings QUAD_RATE up to TX_SAMP_RATE for
        # the sink, same for every mode.
        self.interp = gr_filter.rational_resampler_ccc(
            interpolation=TX_SAMP_RATE // QUAD_RATE, decimation=1)
        self.connect(self.modulator, self.interp)

        # TX display probe: log-power FFT off the final complex baseband
        # (post-modulation, pre-sink) — this is exactly what's being radiated,
        # so the spectrum/waterfall can show the live modulated signal while
        # transmitting instead of a frozen last-RX frame (the paired
        # ScannerRadio's source is stopped for the duration — see app.py's PTT
        # handling). Same shape as ScannerRadio's display probe, minus the
        # GPU option — a TX preview at this sample rate doesn't need it.
        self.samp_rate = TX_SAMP_RATE
        self.lpf = logpwrfft.logpwrfft_c(TX_SAMP_RATE, NFFT, 2, 30, 0.7, True, None, True)
        self.probe = blocks.probe_signal_vf(NFFT)
        self.connect(self.interp, self.lpf, self.probe)

        import osmosdr              # lazy: --help / tests work without the lib
        from . import api, sources as _sources
        dev = _sources.find(self.device)
        drv = api.sources.get(dev.driver) if dev is not None else None
        args = drv.sink_args(dev) if drv is not None else SDR_DEVICES.get(self.device)
        if not args:
            raise RuntimeError(f"{self.device}: no transmitter")
        sink = osmosdr.sink(args=args)
        sink.set_sample_rate(TX_SAMP_RATE)
        sink.set_center_freq(self.freq_hz, 0)
        if isinstance(tx_gains, dict):       # by the stage names the sink reports
            for stage, db in tx_gains.items():
                try:
                    sink.set_gain(float(db), str(stage), 0)
                except Exception:
                    pass
        else:
            for setter, val in ((sink.set_gain, rf_amp), (sink.set_if_gain, vga_gain)):
                try:
                    setter(val, 0)      # not every device exposes every gain stage
                except Exception:
                    pass
        try:
            sink.set_bandwidth(TX_SAMP_RATE, 0)
        except Exception:
            pass
        self.sink = sink
        self.connect(self.interp, self.sink)

    def start(self):
        if self.running:
            return
        gr.top_block.start(self)
        self.running = True

    def stop(self):
        if not self.running:
            return
        gr.top_block.stop(self)
        gr.top_block.wait(self)
        self.running = False
        if self.tx_spec is not None and self.tx_spec.cleanup is not None:
            try:
                self.tx_spec.cleanup(self)
            except Exception:
                import traceback
                traceback.print_exc()

    def wait(self):
        """Block until the flowgraph finishes on its own (file source hit EOF
        without ``loop``). Never returns for a mic source or a looping file —
        the caller (app.py) is expected to run this on a background thread and
        treat its return as "auto-release PTT."""
        if self.running:
            gr.top_block.wait(self)
