# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The Module base class, and what a module registers with the core."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class DemodSpec:
    """The audio path of a module's mode, built by the core like its own modes.

    kind      "fm", "wfm", "am", "ssb", "cw", or "silent": no demod, the speaker path is zeros (a mode
              that plays its own audio through api.AudioInput, or none); no squelch
    bw        channel / filter bandwidth, Hz
    max_dev   FM peak deviation, Hz (sets the discriminator gain)
    deemph    FM de-emphasis time constant, s (0: none -- data modes)
    sideband  "usb" / "lsb" for kind "ssb"
    pitch     kind "cw": the beat note, Hz
    bw_step   a dragged marker edge snaps to multiples of this (None: the standard channel widths)
    squelch   whether the power squelch applies
    tones     FM with the core's CTCSS squelch / tone decode taps
    """
    kind: str = "fm"
    bw: int = 12_500
    max_dev: float = 5_000.0
    deemph: float = 0.0
    sideband: Optional[str] = None
    pitch: float = 0.0
    bw_step: Optional[int] = None
    squelch: bool = True
    tones: bool = False


@dataclass
class ModeSpec:
    """An entry in the Mode picker.

    enter / leave  called with the app when the mode is picked / when another one is picked. The core has
                   already switched the audio to ``demod`` when enter() runs.
    demod          a DemodSpec (the audio you hear in this mode), a core mode name ("NFM") to listen
                   through, or None to keep the current one (a module that manages the radio itself)
    available      () -> (ok, reason); an unavailable mode is listed grayed with the reason as its tooltip
    picker         "mode" (the only picker)
    """
    name: str
    picker: str = "mode"
    enter: Optional[Callable] = None
    leave: Optional[Callable] = None
    demod: object = None
    available: Optional[Callable] = None
    tooltip: str = ""


@dataclass
class TxModeSpec:
    """A transmit mode a module adds: an entry in the main window's TX: picker (beside "Analog", which
    follows the receive mode). PTT then builds the core's transmit flowgraph (microphone or file audio, the
    device sink, the TX spectrum) around this mode's modulator.

    build          (tb, audio, settings) -> block: the modulator. ``tb`` is the transmit gr.top_block (connect
                   inside it), ``audio`` the float audio block at 48 kHz (unit full scale), ``settings`` this
                   mode's dict (from settings_factory / vfo_state). It returns the complex baseband block at
                   240 kHz (api TX_RATE) that the core interpolates to the device rate. Raise to refuse (the
                   message is shown).
    cleanup        (tb) -> None, after the transmitter stops (temp files ...), or None
    available      () -> (ok, reason)
    settings       a dict of this mode's settings (saved in the module's state by the module itself); the
                   core passes it to build()
    settings_widget (parent) -> QWidget, or None: the mode's fields in Settings > Transmit
    quick_widget   (parent) -> QWidget, or None: a small widget shown in the main window's SDR
                   strip, by the TX: picker, ONLY while this TX mode is selected — for the mode's
                   most-changed fields, so they can be edited without opening Settings. The core only
                   hosts and shows/hides it per selected mode; it carries no knowledge of the fields.
    channel_tone   () -> str: the tone/ID text a channel saved while this mode is picked gets ("" for none)
    apply_tone     (tone) -> None: a recalled channel's tone text (the mode takes what it understands)
    tooltip        for the TX: picker entry
    """
    name: str
    build: Callable
    cleanup: Optional[Callable] = None
    available: Optional[Callable] = None
    settings: dict = field(default_factory=dict)
    settings_widget: Optional[Callable] = None
    quick_widget: Optional[Callable] = None
    channel_tone: Optional[Callable] = None
    apply_tone: Optional[Callable] = None
    tooltip: str = ""


@dataclass
class MenuEntry:
    """A menu action. ``menu`` is "View", "Scan" or "Settings"; ``checkable`` actions get ``toggled``
    (bool) instead of ``triggered``. ``action`` is filled in by the core once the QAction exists."""
    menu: str
    text: str
    triggered: Optional[Callable] = None
    toggled: Optional[Callable] = None
    checkable: bool = False
    checked: bool = False
    enabled: bool = True
    tooltip: str = ""
    submenu: list = field(default_factory=list)     # context_entries only: a submenu of MenuEntry
    action: object = field(default=None, repr=False)


@dataclass
class Overlay:
    """A waterfall painter: paint(qp, rect, waterfall_rect, f_lo_hz, f_hi_hz), drawn after the
    core's own overlays in ``order``. ``visible`` () -> bool decides per frame (None: always)."""
    name: str
    paint: Callable
    order: int = 100
    visible: Optional[Callable] = None


@dataclass
class Panel:
    """A widget the core places. ``area``: "decode" (a tab in the decode strip under the waterfall),
    "status" (the status bar, right side). ``factory`` (parent) -> QWidget, called once the area exists."""
    title: str
    factory: Callable
    area: str = "decode"
    widget: object = field(default=None, repr=False)


class Module:
    """Base class of an sdrexplorer module. Subclass it, set the class attributes, override the hooks
    you need, and register the class under the "sdrexplorer.modules" entry point.

    The core calls, in order: probe() (always, even when the user has the module switched off, to show
    why it's unavailable); for a loaded module attach_radio(), attach_ui(), modes() / menu_entries() /
    overlays() / panels() / decodes() / services(), load_state(); then save_state() (also periodically)
    and, at exit, shutdown().
    """
    name = ""                 # unique short id; its state.json section and Settings > Modules key
    title = ""                # shown in Settings > Modules
    version = "0"
    api = ">=1.0,<2.0"        # the API_VERSION range this module works with
    paths = {}                # path keys it uses: {key: description}; resolved like the core's
                              # (SDREXPLORER_<KEY>, paths.json, <install>/ext/<key with - for _>)
    min_frames = 5            # decode records of one protocol before a recording is labeled with it

    def __init__(self):
        self.app = None       # the ScannerApp, from attach_ui on
        self.radio = None     # the ScannerRadio, from attach_radio on

    # -- lifecycle ------------------------------------------------------------------------------------
    def probe(self) -> tuple:
        """(ok, reason): are this module's own dependencies there? Called before anything else."""
        return True, ""

    def attach_radio(self, radio, taps):
        """Build flowgraph branches. ``taps`` (api.Taps) connects them to the core's tap points."""
        self.radio = radio

    def attach_ui(self, app, ui):
        """``ui`` is this module's handle on the core (moduleui.ModuleUI): ui.publish(Decode), ui.bus,
        ui.audio, ui.add_menu_entry / add_overlay / add_panel / add_mode, ui.request_decode, ui.status."""
        self.app = app

    def modes(self) -> list:
        """[ModeSpec] this module adds to the Mode picker."""
        return []

    def overlays(self) -> list:
        return []

    def menu_entries(self) -> list:
        return []

    def panels(self) -> list:
        """[Panel] for the decode strip / status bar."""
        return []

    def decodes(self) -> dict:
        """{label: handler}: labels of signals this module can decode. ``handler(freq_hz, label, info)
        -> bool`` (True: it took it). The core routes app.request_decode(label, ...) here without knowing
        which module answers; with none, nothing happens. A module lists every spelling it accepts."""
        return {}

    def context_entries(self, freq_hz, annotation) -> list:
        """[MenuEntry] for the waterfall's right-click menu at ``freq_hz`` (``annotation``: the labeled
        signal there, a dict, or None). ``menu`` is ignored; a non-empty ``submenu`` on the entry (a list
        of MenuEntry) makes it a submenu."""
        return []

    def tx_modes(self) -> list:
        """[TxModeSpec] this module can transmit."""
        return []

    def sources(self) -> list:
        """[api.sources.SourceDriver] this module adds: its devices appear in the SDR picker. Called at
        discovery (before the radio exists), so the app's first device search includes them."""
        return []

    # -- bank channels --------------------------------------------------------------------------------
    def channel_fields(self, channel) -> dict:
        """Fields to store on a channel the user is saving now (talkgroup, color code, a satellite
        id...): a JSON-safe dict, kept under ``channel.ext[self.name]`` and handed back on recall. None or
        {} stores nothing. ``channel`` is the new api.Channel (frequency and mode already set)."""
        return None

    def channel_recalled(self, channel, fields: dict):
        """A bank channel was tuned (a click in the table, a scan or priority hit), after its mode
        and frequency are set. ``fields`` is what channel_fields stored for it ({} if nothing)."""

    def channel_summary(self, fields: dict) -> str:
        """One line describing this module's stored fields, shown read-only in Edit memory."""
        return ", ".join(f"{k} {v}" for k, v in fields.items())

    def importers(self) -> list:
        """[api.importers.ImportProvider] this module adds to Memories ▸ Import."""
        return []

    def services(self) -> dict:
        """(legacy) {name: object} looked up with app.service(name). Prefer bus requests / topics."""
        return {}

    def load_state(self, d: dict):
        """This module's saved section of state.json ({} the first time)."""

    def save_state(self) -> dict:
        return {}

    def shutdown(self):
        """Stop threads and branches. The radio still exists when this runs."""
