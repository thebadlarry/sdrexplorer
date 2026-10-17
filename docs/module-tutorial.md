# Writing a module: a tutorial

This tutorial builds a complete sdrexplorer module, step by step. The finished code is in
[`examples/sdrexplorer-activity/`](../examples/sdrexplorer-activity/), and its tests load it into the real
application window. Every call is described in full in the [module API reference](module-api.md).

The module, **activity**, finds busy channels in the captured band and:

- tags each one on the waterfall,
- lists them in an "Activity" tab under the waterfall (double-click tunes),
- adds a View menu toggle, a right-click entry, and a wide-NFM entry in the Mode picker,
- answers a question other modules can ask it,
- stores the measured level on memory channels saved while tuned to one,
- remembers its settings between runs.

You need a working sdrexplorer (see [INSTALL.md](../INSTALL.md)), Python 3.11 or later, and basic PyQt5.
No GNU Radio knowledge is needed for this module; [the last section](#11-going-further) shows where
flowgraph code would go.

## 1. Create the repository

```
sdrexplorer-activity/
  pyproject.toml
  module.json
  src/sdrexplorer_activity/
    __init__.py
    detect.py
    module.py
  tests/
    test_activity.py
```

`pyproject.toml` registers the module under the `sdrexplorer.modules` entry point. This is what makes the
core find it:

```toml
[build-system]
requires = ["setuptools>=64"]
build-backend = "setuptools.build_meta"

[project]
name = "sdrexplorer-activity"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = ["numpy>=1.24"]

[project.entry-points."sdrexplorer.modules"]
activity = "sdrexplorer_activity.module:ActivityModule"

[tool.setuptools.packages.find]
where = ["src"]
```

`module.json` is for the install script and for people reading the repo. The app never reads it:

```json
{
  "name": "activity",
  "title": "Band activity (example)",
  "api": ">=1.0,<2.0",
  "pip": ["numpy>=1.24"],
  "ext": [],
  "platforms": ["linux", "darwin", "win32"]
}
```

`src/sdrexplorer_activity/__init__.py` only holds the version:

```python
__version__ = "1.0.0"
```

## 2. Keep the signal processing separate

Put the work that doesn't need Qt or the radio in its own file. It can then be tested on its own, and
reused headlessly (a [decoder proof](module-api.md#22-decoder-proofs-apiproofs), a worker, a training
script).

`detect.py` finds runs of bins above the noise floor in one spectrum frame:

```python
from dataclasses import dataclass
import numpy as np


@dataclass
class Peak:
    freq_hz: float
    bw_hz: float
    snr_db: float


def find_peaks(db, center_hz, samp_rate, threshold_db=10.0, max_peaks=12, min_bins=2):
    db = np.asarray(db, np.float64)
    n = db.size
    if n == 0:
        return []
    floor = float(np.median(db))
    busy = db > floor + threshold_db
    d = np.diff(np.concatenate([[0], busy.astype(np.int8), [0]]))
    starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    bin_hz = samp_rate / n
    out = []
    for s, e in zip(starts, ends):
        if e - s < min_bins:
            continue
        mid = (s + e - 1) / 2.0
        out.append(Peak(freq_hz=center_hz + (mid / n - 0.5) * samp_rate,
                        bw_hz=(e - s) * bin_hz,
                        snr_db=float(db[s:e].max() - floor)))
    out.sort(key=lambda p: p.snr_db, reverse=True)
    return out[:max_peaks]
```

The frame's bins are ordered lowest frequency first, and bin `i` is centered at
`center_hz + (i / n - 0.5) * samp_rate`. That is the layout `api.spectrum` delivers.

## 3. The module class

`module.py` starts with the class attributes. Import only from `sdrexplorer.api`; the rest of the core is
private and may change.

```python
from PyQt5 import QtWidgets
from sdrexplorer.api import (Annotation, Decode, DemodSpec, MenuEntry, ModeSpec, Module, Panel, spectrum)

from . import __version__
from .detect import find_peaks


class ActivityModule(Module):
    name = "activity"                 # state key, channel-field key, Settings ▸ Modules key: never change it
    title = "Band activity (example)"
    version = __version__
    api = ">=1.0,<2.0"                # the core API versions this works with

    def __init__(self):
        super().__init__()
        self.ui = None
        self.enabled = True
        self.threshold_db = 10.0
        self.peaks = []
        self._list = None
        self._menu = None
```

`probe()` says whether the module's own dependencies are present. It runs at start-up even when the user
has switched the module off, so keep it quick and free of side effects. It runs on a separate instance,
so don't set anything on `self` here:

```python
    def probe(self):
        try:
            import numpy  # noqa: F401
        except ImportError as e:
            return False, f"numpy missing: {e}"
        return True, ""
```

If `probe()` returns `False`, Settings ▸ Modules shows the module as unavailable, with the reason.

## 4. Attach to the core

`attach_ui(app, ui)` hands over `ui`, the module's handle on the core. Keep it. This is also where a module
subscribes to topics and offers requests on the bus:

```python
    def attach_ui(self, app, ui):
        super().attach_ui(app, ui)
        self.ui = ui
        ui.bus.subscribe("radio_stopped", self._on_radio_stopped)
        ui.bus.subscribe("band_changed", self._on_band_changed)
        ui.bus.provide_request("activity.peaks", self._answer_peaks, owner=self.name)
```

Core topics (`radio_stopped`, `band_changed`, `tuned` …) have no prefix. A module's own topics and
requests start with its name and a dot, `activity.peaks`, so modules never collide. Another module can now
ask:

```python
peaks = self.ui.bus.request("activity.peaks", default=[])
```

It gets `[]` when this module isn't installed. Code written this way works with or without the other
modules.

The handlers receive the topic's payload as keyword arguments:

```python
    def _on_radio_stopped(self):
        self._clear()

    def _on_band_changed(self, center_hz, samp_rate):
        self._clear()

    def _answer_peaks(self):
        return [{"freq_hz": p.freq_hz, "bw_hz": p.bw_hz, "snr_db": p.snr_db} for p in self.peaks]
```

## 5. Declare what the module adds

Right after `attach_ui`, the core asks each module what it adds. These methods are called once.

A **Mode picker entry**. The core builds the audio path from the `DemodSpec`, as it does for its own modes:

```python
    def modes(self):
        return [ModeSpec("NFM-25", demod=DemodSpec("fm", bw=25_000, max_dev=5_000, deemph=75e-6),
                         tooltip="NFM on a 25 kHz channel (example module)")]
```

A decoder's mode would also pass `enter=` and `leave=` callbacks to start and stop its decode branch.

A **menu entry**. Keep the `MenuEntry`: once the core has made the `QAction`, it is in `entry.action`.

```python
    def menu_entries(self):
        self._menu = MenuEntry("View", "Band activity markers", checkable=True, checked=self.enabled,
                               toggled=self._set_enabled,
                               tooltip="Tag busy channels on the waterfall (example module)")
        return [self._menu]
```

A **panel**: a tab in the decode strip under the waterfall. The factory is called once, with the parent
widget:

```python
    def panels(self):
        return [Panel("Activity", self._make_panel)]

    def _make_panel(self, parent):
        w = QtWidgets.QWidget(parent)
        lay = QtWidgets.QHBoxLayout(w)
        lay.setContentsMargins(4, 2, 4, 2)
        self._list = QtWidgets.QListWidget()
        self._list.itemDoubleClicked.connect(self._tune_item)
        lay.addWidget(self._list, 1)
        thr = QtWidgets.QDoubleSpinBox()
        thr.setRange(3, 40)
        thr.setSuffix(" dB")
        thr.setValue(self.threshold_db)
        thr.valueChanged.connect(lambda v: setattr(self, "threshold_db", float(v)))
        lay.addWidget(thr)
        self._show()
        return w
```

## 6. Get data, show results

The core already computes an FFT of the whole band for the waterfall. `api.spectrum` shares it, so a module
never adds its own FFT to the flowgraph. Subscribing and unsubscribing is what the menu toggle does:

```python
FRAME_RATE_HZ = 2.0

    def _set_enabled(self, on):
        self.enabled = bool(on)
        if self.enabled:
            spectrum.subscribe(self._on_frame, rate_hz=FRAME_RATE_HZ)
        else:
            spectrum.unsubscribe(self._on_frame)
            self._clear()
```

Frames arrive on the Qt main thread, so the callback can update widgets and call `ui` directly. It also
runs on the same thread as the display, so keep it cheap; that's why this module asks for 2 frames a
second.

```python
    def _on_frame(self, frame):
        self.peaks = find_peaks(frame.db, frame.center_hz, frame.samp_rate, self.threshold_db)
        self.ui.annotate([Annotation(freq_hz=p.freq_hz, bw_hz=p.bw_hz, label=f"{p.snr_db:.0f} dB",
                                     snr_db=p.snr_db, raw="busy") for p in self.peaks])
        self._show()
```

`ui.annotate()` replaces the module's previous set of tags. The core draws them in its own style, keeps a
tag on the row where a signal was last seen after it stops, and finds them for the right-click menu. Clear
them when they no longer apply; `trail=False` also removes the trailing tags:

```python
    def _clear(self):
        self.peaks = []
        if self.ui is not None:
            self.ui.annotate([], trail=False)
        self._show()
```

The panel's list, and tuning from it. `user=True` says the user asked for this in the module's own
window, so the core tunes as if the user had clicked; it is never refused:

```python
    def _show(self):
        if self._list is None:
            return
        self._list.clear()
        for p in self.peaks:
            item = QtWidgets.QListWidgetItem(f"{p.freq_hz / 1e6:10.4f} MHz  {p.bw_hz / 1e3:6.1f} kHz  "
                                             f"{p.snr_db:5.1f} dB")
            item.setData(0x0100, p.freq_hz)            # Qt.UserRole
            self._list.addItem(item)

    def _tune_item(self, item):
        self.ui.tune(int(item.data(0x0100)), recenter=False, user=True)
```

## 7. The right-click menu and the decode log

`context_entries` is called on every right-click on the waterfall, with the annotation under the cursor
(from any module) as a dict, or `None`. This module only adds an entry for its own tags:

```python
    def context_entries(self, freq_hz, annotation):
        if not annotation or annotation.get("source") != self.name:
            return []
        f, snr = annotation["freq_hz"], annotation.get("snr_db", 0.0)
        return [MenuEntry("", f"Log this signal ({snr:.0f} dB)", triggered=lambda: self._log(f, snr))]

    def _log(self, freq_hz, snr_db):
        self.ui.publish(Decode(source=self.name, protocol="activity",
                               text=f"busy channel {freq_hz / 1e6:.4f} MHz, {snr_db:.0f} dB over the floor",
                               fields={"snr_db": snr_db}, freq_hz=int(freq_hz), counts=False))
```

`ui.publish(Decode(...))` is the one path for decoded data. The core writes `text` to the decode log with
the time and station name, shows `payload` bytes if any, counts the record toward the label of an IQ
recording being made (unless `counts=False`, as here, since this isn't a decode of the signal), and
republishes it on the `decode` topic for other modules.

## 8. State and memory channels

`save_state()` returns a JSON-safe dict, saved every 15 s and at exit. `load_state(d)` gets it back at the
next start (`{}` the first time). It runs after the declarations, so the menu action exists and can be
updated:

```python
    def load_state(self, d):
        self.threshold_db = float(d.get("threshold_db", self.threshold_db))
        self._set_enabled(bool(d.get("enabled", self.enabled)))
        if self._menu is not None and self._menu.action is not None:
            self._menu.action.setChecked(self.enabled)

    def save_state(self):
        return {"enabled": self.enabled, "threshold_db": self.threshold_db}
```

Note that the spectrum subscription starts in `load_state`, through `_set_enabled`. That way it starts
once, in the saved on/off state.

When the user saves a memory channel, each module may store its own fields on it. They are kept under
`channel.ext["activity"]`, written back by every core version (also one without this module), and handed
back in `channel_recalled()` when the channel is tuned again:

```python
    def channel_fields(self, channel):
        p = self._peak_at(channel.freq)
        return {"snr_db": round(p.snr_db, 1)} if p else None

    def channel_summary(self, fields):
        return f"{fields.get('snr_db', '?')} dB over the floor when saved"

    def _peak_at(self, freq_hz):
        return next((p for p in self.peaks if abs(p.freq_hz - freq_hz) <= max(p.bw_hz / 2, 2_500)), None)
```

`channel_summary()` is the read-only line shown in the Edit memory dialog.

Finally, `shutdown()` stops what the module started. The radio still exists when it runs:

```python
    def shutdown(self):
        spectrum.unsubscribe(self._on_frame)
```

## 9. Run it

Put the folder next to your sdrexplorer checkout and the core finds it by itself:

```
src/
  sdrexplorer/
  sdrexplorer-activity/
```

Or keep it anywhere and add it in **Settings ▸ Modules ▸ Add module…**. Either way it loads at the next
start. To run sdrexplorer with only this module:

```bash
./run_sdrexplorer.sh --modules activity
```

Settings ▸ Modules shows it as *loaded*, with its folder. If it says *error* or *unavailable*, the reason
is in the Status column, and the full traceback is on the terminal.

The example lives inside the core repo, where it is **not** found automatically (it is in `examples/`,
not next to the checkout). To try it:

```bash
SDREXPLORER_MODULE_DIRS=$PWD/examples/sdrexplorer-activity ./run_sdrexplorer.sh
```

## 10. Test it

Two layers of tests, both in [`tests/test_activity.py`](../examples/sdrexplorer-activity/tests/test_activity.py):

1. **The DSP alone**: `find_peaks` on a synthetic frame. Needs only numpy.
2. **In the real window**: build the main window offscreen, with every data path in a temp folder, load the
   module through the normal discovery path, and drive it with a synthetic `SpectrumFrame`:

```python
from sdrexplorer import paths as P
P.override(tempfile.mkdtemp(), module_dirs=[REPO])        # before importing sdrexplorer.app

from sdrexplorer import app as A
from sdrexplorer.api import spectrum
from sdrexplorer.api.spectrum import SpectrumFrame

A.ScannerRadio.start = lambda self: setattr(self, "running", True)        # no SDR needed
w = A.ScannerApp()
mod = w.modules.get("activity")
spectrum.deliver(SpectrumFrame(t=0.0, center_hz=CENTER, samp_rate=RATE, db=frame_db(...)))
assert w.annotation_near(CENTER + 100_000)["source"] == "activity"
assert len(w.bus.request("activity.peaks", default=[])) == 1
w.close()
```

Run them:

```bash
cd examples/sdrexplorer-activity
QT_QPA_PLATFORM=offscreen python3 tests/test_activity.py
```

For faster unit tests that need no window at all, `sdrexplorer.api.testing` has `FakeRadio` and `FakeUI`
([reference §24](module-api.md#24-testing-a-module)).

## 11. Going further

The activity module touches only the window and the bus. Real decoders go further:

| You want to | Use | Example in the wild |
|---|---|---|
| decode the tuned channel | a GNU Radio branch on `TAP_CHANNEL`, started in `ModeSpec.enter` | sdrexplorer-paging (POCSAG / FLEX) |
| decode signals anywhere in the band | `api.radio.open_channel` or `snapshot` | sdrexplorer-classifier |
| play decoded audio | `ui.audio.claim` + `open_input` | sdrexplorer-digital (voice) |
| take over when another module labels a signal | `decodes()` | every decoder module |
| move the band on your own | the tuner lease (`ui.tuner.acquire`) | the classifier's signal hunt |
| run a heavy runtime or native library | `api.worker` | the classifier's inference backends |
| put things on the map | `ui.publish_position` | DMR LRRP, NXDN GPS |
| add a transmit mode | `tx_modes()` + `TxModeSpec` | sdrexplorer-digital (P25 / DMR) |
| support new SDR hardware | `sources()` + `SourceDriver` | (the core's own gr-osmosdr driver) |
| prove a recording really is your mode | a proof under `sdrexplorer.proofs` | paging, digital |

The [reference](module-api.md) has a section on each. Before publishing a module:

- set `api` to the range you tested;
- list external builds with pinned commits and patches in `module.json`, and explain them in the README;
- make `probe()` and `ModeSpec.available` give a reason the user can act on ("needs gr-pager: see the
  README"), not a traceback;
- check the module still loads with every other module switched off (`--modules yourname`).
