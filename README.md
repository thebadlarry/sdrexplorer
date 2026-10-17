# sdrexplorer

A tool for exploring and analyzing digital and analog signals using software
defined radio (HackRF, RTL-SDR), built on GNU Radio 3.10 + PyQt5.

![sdrexplorer: the band waterfall with signal labels, and the decode strip with decoded positions and their raw payloads](docs/images/main-window.png)

*Screenshots are from the demo mode (`python3 tools/demo.py`): a synthetic band with made-up decodes and
positions. More in the [user manual](docs/user-manual.md).*

- **Analog:** NFM / AM / WFM / USB / LSB / CW with a live spectrum + waterfall, banks/memory,
  stop-on-active scanning, priority scan, and a draggable/snapping demod marker.
  - **Tuning to 1 Hz:** the frequency readout shows down to Hz (`0007.030 125` MHz: the last
    three digits are hundreds, tens and units of Hz).
    - Left-click a digit to raise it, right-click to lower it, or turn the mouse wheel over it.
    - Steps of 1 kHz and up recenter the band, as typing a frequency does.
    - 100 / 10 / 1 Hz steps only move the demod within the band: the waterfall stays put and a
      decoder lock isn't dropped.
  - **CW** is tuned on the carrier: the readout is the carrier frequency, and the carrier is heard
    as a 700 Hz tone.
    - A signal above the tuned frequency sounds higher, as on a rig's CW-U.
    - The filter is 500 Hz wide by default; drag the marker edges to change it in 50 Hz steps.
    - Snap goes to a 100 Hz grid.
- **RDS (optional):** decodes WFM broadcast station metadata (station name,
  RadioText, program type) via gr-rds, shown live in the tuning display.
- **Modules:** features with their own dependencies come as separate module repos.
  - **Adding one:** clone its repo to any folder and pick that folder in Settings ▸ Modules ▸ Add module…
    (checked first, then remembered). A repo cloned next to this checkout is found by itself.
  - Settings ▸ Modules also lists each module's folder and how it was found, switches modules on or off,
    removes added folders and rescans. `--modules a,b` / `--no-modules` apply to one run.
  - Writing one: the [tutorial](docs/module-tutorial.md) and the [API reference](docs/module-api.md).
  - **Paging** (`sdrexplorer-paging`): POCSAG 512 / 1200 / 2400 bps and FLEX 1600 / 3200 / 6400 bps, following
    the transmitter's FM deviation up to ±16 kHz and either FSK polarity. They appear in the Mode picker
    when the module is there; FLEX also needs gr-pager (the module's README).
  - **Classifier** (`sdrexplorer-classifier`): the signal explorer labels every signal in the band (a neural
    network on the CPU through ONNX Runtime, or an accelerator), switches the decoder when you tune onto one,
    saves "Label as…" training examples, hunts for new signals of one mode, and has a phone remote for the
    hunt (the module's README).
  - **Digital** (`sdrexplorer-digital`): digital voice and trunking decode (op25 and dsd-fme), with its own
    tab in the decode strip, decoded voice through the speaker, positions on the map, decryption keys and
    digital transmit modes (the module's README).
- **Annotations:** a module's labeled signals are drawn on the waterfall (View ▸ Label source: memories,
  annotations or both). When a labeled signal stops, its tag stays on the row it was last seen on and scrolls
  down with it.
- **IQ recording:** ● REC (full band or demod window), ◷ SNAP (the last few seconds,
  from a RAM buffer), and right-drag a window on the waterfall to **record**,
  **snapshot**, or **record when a signal appears** in it. The trigger re-arms
  after each transmission, so each is its own `iq_trig_*` file, plus an
  `iq_pretrig_*` file with the seconds before. Threshold, hang time and max
  length: Settings ▸ Recording trigger…. ■ DISARM or Esc stops watching.
- **ADS-B (optional):** 1090 MHz aircraft reception via gr-adsb, with a live
  aircraft table and a **map** plotting aircraft and the radio positions modules decode.
- **Selectable capture bandwidth** (2.4 / 4.8 / 9.6 / 14.4 / 19.2 Msps on HackRF).
- **GPU acceleration (optional):** a CUDA OOT module offloads the channelizer
  and the spectrum/waterfall FFT probes to the GPU, so wide capture bandwidths
  and large FFT sizes don't spike the CPU. Falls back to the stock CPU blocks
  automatically if it isn't built. See [below](#gpu-acceleration-optional).
- **Any SDR gr-osmosdr or SoapySDR can open** (HackRF, RTL-SDR, LimeSDR, Airspy, bladeRF, USRP,
  PlutoSDR, ...).
  - The SDR picker lists what's attached, by name and serial; Settings ▸ Find SDR devices looks again.
  - Capture rates, one gain slider per stage the device has, frequency correction (ppm) and whether PTT
    is offered all come from what the device reports. They are kept per device.
  - A LimeSDR, for example, needs its SoapySDR module (`soapysdr-module-lms7`), nothing in sdrexplorer.
- Two independent VFOs, SDR device selector with Start/Stop and auto-run,
  data-source import (CSV / FCC ULS / RadioReference), and session persistence.
- **FCC license search** (Memories ▸ Import ▸ *Search FCC data…*, or View ▸ FCC license search…):
  search the downloaded FCC ULS weekly Land Mobile data without importing it.
  - Download a dataset (LMcomm ~80 MB, LMpriv ~420 MB, LMbcast ~8 MB zipped) with *Download FCC data*;
    the search window opens when it finishes.
  - The first search after a download builds `uls/uls_index.sqlite` (~35 s for LMpriv); searches take
    under a second after that.
  - Filter by Where + radius, frequency (one value = ±1 kHz, or a range), licensee / call sign / city,
    service, and emission type (P25 Phase 1/2, DMR, NXDN48/96, FM / NFM voice, …).
  - Emission designators are shown by name ("11K2F3E" → "NFM voice (12.5 kHz)", "8K10F1D" → "P25 Phase 1");
    the raw codes are in their own column and the tooltip. A license lists what is *allowed*, not what is
    on the air.
  - Double-click tunes a row; *Add selected → bank…* copies rows into a bank. *Import → bank* still
    copies a whole region in one go.

## Documentation

- [User manual](docs/user-manual.md): how to use every part of the program.
- [INSTALL.md](INSTALL.md): step-by-step install.
- [Architecture](docs/architecture.md): how it is built, with block diagrams.
- [Module tutorial](docs/module-tutorial.md) and [module API reference](docs/module-api.md): writing a module.
- [Everything else](docs/README.md).

## Prerequisites

| Component | Version here | Required for | How to get it |
|-----------|--------------|--------------|---------------|
| Python | 3.12 | everything | system / conda |
| GNU Radio | 3.10.9 | everything | distro pkg, or **radioconda** (recommended, cross-platform) |
| gr-osmosdr | 0.2.5 | HackRF / RTL-SDR I/O | the install script builds it with a HackRF PTT fix ([INSTALL.md](INSTALL.md#external-builds-and-their-patches)); else distro pkg / radioconda |
| PyQt5 | 5.15 | the GUI | `pip install PyQt5` or distro |
| NumPy | 1.26 | DSP/decode | `pip install numpy` |
| SciPy | 1.11 | snapshot sub-band filter | `pip install scipy` |
| HackRF / RTL-SDR host libs | — | the radios | `libhackrf`, `librtlsdr` + udev rules |
| **gr-adsb** (optional) | maint-3.10 | ADS-B (1090 MHz) reception | clone; key `gr_adsb` = its `python/` dir (no build); see below |
| **gr-rds** (optional) | maint-3.10 | WFM RDS station metadata (PS/RadioText/PTY) | conda-forge (`gnuradio-rds`), or build from source; see below |
| PyQtWebEngine (optional) | 5.15 | the live map view | `pip install PyQtWebEngine` (or `pip install -e '.[map]'`) |
| `zeep` (optional) | — | RadioReference import | `pip install zeep` |
| **CUDA Toolkit + gr-sdrexplorer_gpu** (optional) | CUDA 12 | GPU-accelerated channelizer + FFT probes | NVIDIA GPU only; build the in-repo OOT module — see below |

Every optional part switches off on its own when it is missing. Without
gr-rds, WFM demodulates normally but with no station metadata. Modules list
their own requirements in their READMEs.

## Install & run

**Debian / Ubuntu:** the install script sets up the core and the modules, including their external builds
(gr-osmosdr, op25, dsd-fme, gr-pager, gr-rds) at tested versions, with the patches listed in
[INSTALL.md](INSTALL.md#external-builds-and-their-patches):

```bash
mkdir -p ~/sdrexplorer/src && cd ~/sdrexplorer/src
git clone https://github.com/thebadlarry/sdrexplorer
./sdrexplorer/install.sh          # lists any missing apt packages first; run it again after installing them
~/sdrexplorer/bin/sdrexplorer
```

[INSTALL.md](INSTALL.md) has the details and its options. The sections below are the manual routes: conda
(any OS) and distro packages.

### Option A — conda / radioconda (Linux, macOS, Windows)

```bash
conda env create -f environment.yml     # or: mamba env create -f environment.yml
conda activate sdrexplorer
pip install -e .                         # optional: installs a `sdrexplorer` command
python -m sdrexplorer.app                    # Linux/macOS also: ./run_sdrexplorer.sh
```

On **Windows**: run in the activated env with `python -m sdrexplorer.app` or
`run_sdrexplorer.bat`; for RTL-SDR, install the WinUSB driver with
[Zadig](https://zadig.akeo.ie/) first.

### Option B — Linux distro packages

```bash
# Debian / Ubuntu
sudo apt install gnuradio gr-osmosdr python3-pyqt5 python3-numpy hackrf rtl-sdr
# Fedora:  sudo dnf install gnuradio gr-osmosdr python3-qt5 python3-numpy hackrf rtl-sdr
# Arch:    sudo pacman -S gnuradio gr-osmosdr python-pyqt5 python-numpy hackrf rtl-sdr
sudo usermod -aG plugdev "$USER"         # + install HackRF/RTL udev rules, then re-login
./run_sdrexplorer.sh                 # or: python3 -m sdrexplorer.app
```

### Where sdrexplorer finds things

Your data lives outside the program, so the program directory can be read-only:

| What | Where (Linux) |
|---|---|
| Banks, session state, keys, caches, modules' logs | `~/.local/share/sdrexplorer/memory/` |
| IQ recordings | `~/.local/share/sdrexplorer/recordings/` |
| FCC ULS downloads + search index | `~/.local/share/sdrexplorer/uls/` |
| RadioReference credentials (`config.json`), your `paths.json` | `~/.config/sdrexplorer/` |

macOS uses `~/Library/Application Support/sdrexplorer`, Windows `%LOCALAPPDATA%\sdrexplorer`.
`--portable` (or `SDREXPLORER_PORTABLE=1`) keeps all of it under `<install>/data/` instead.
Versions before this kept data inside the package (`sdrexplorer/memory/` etc.). On the first start
they are taken over: `memory/` and `config.json` are copied (the originals stay as a backup, with a
`MOVED.txt` note), and the large `recordings/` and `uls/` are used where they are, through an entry in
your `paths.json`.

Optional external builds are looked up by key, in this order:

1. an environment variable `SDREXPLORER_<KEY>` (the old `ADSB_PATH` still works);
2. `~/.config/sdrexplorer/paths.json`;
3. `paths.json` in the install directory (the repo root here; the install script will write it);
   see [`paths.example.json`](paths.example.json);
4. `<install>/ext/<name>` (`ext/gr-rds`, `ext/gr-adsb`, and modules' own builds);
5. the system: `PATH`, the Python path, distro / conda packages.

| Key | What |
|---|---|
| `gr_rds` | gr-rds install prefix |
| a module's own keys | listed in its README (e.g. `gr_pager` for the paging module, `op25` / `dsd_fme` for the digital one) |
| `gr_adsb` | gr-adsb's `python/` directory |
| `gr_sdrexplorer_gpu` | the CUDA module's build directory (default `gr-sdrexplorer_gpu/build`) |
| `recordings_dir`, `uls_dir`, `memory_dir`, `data_dir`, `config_dir` | move any data directory |

Relative paths in a `paths.json` are relative to that file. To see what was found and why:

```bash
./run_sdrexplorer.sh --paths        # or: python3 -m sdrexplorer.paths
```

Machine-specific shell fixes for the launcher go in `run_sdrexplorer.local.sh` next to it
(sourced if present, git-ignored).

### ADS-B (1090 MHz) + live map — optional

ADS-B uses **gr-adsb** (pure-Python blocks; no compile). Clone it and point
sdrexplorer at its `python/` dir (key `gr_adsb`); then pick **ADS-B** in the mode
selector (the SDR parks on 1090 MHz and the aircraft table fills):

```bash
git clone -b maint-3.10 https://github.com/mhostetter/gr-adsb ~/src/gr-adsb
SDREXPLORER_GR_ADSB=~/src/gr-adsb/python ./run_sdrexplorer.sh   # or "gr_adsb" in paths.json
```

The **View → Live map…** window plots aircraft (heading-rotated markers, with
trails) and any radio positions modules decode on an OpenStreetMap basemap (dark/light
themes). Markers are shaped by the ADS-B **emitter category** the aircraft
broadcasts — helicopter, heavy, jet, light, glider, UAV, balloon — shown in the
map legend and the aircraft table's Class column. (ADS-B only carries this
coarse category over the air, not an exact type like "C-130J".) It needs
**PyQtWebEngine** — `pip install -e '.[map]'` (or `pip install PyQtWebEngine`),
or the distro package: `python3-pyqt5.qtwebengine` (Debian/Ubuntu),
`python3-qt5-webengine` (Fedora), `python-pyqtwebengine` (Arch). Leaflet is
vendored locally (a file:// page can't load it from a CDN), so only the OSM map
**tiles** need network. Without PyQtWebEngine the menu item is disabled and the
rest of the app is unaffected.

### Broadcast RDS (optional)

WFM station metadata (station name / PS, RadioText, program type) uses
**gr-rds** for block sync, CRC error correction, and group parsing — the 57 kHz
subcarrier extraction, carrier recovery, and symbol sync happen in `radio.py`
itself using stock GNU Radio blocks. Easiest install, via conda-forge (into
the same env as `environment.yml`):

```bash
conda install -c conda-forge gnuradio-rds
```

Or build from source against this repo's GNU Radio 3.10:

```bash
git clone -b maint-3.10 https://github.com/bastibl/gr-rds ~/src/gr-rds
cd ~/src/gr-rds && mkdir -p build && cd build
cmake .. -DCMAKE_INSTALL_PREFIX=$CONDA_PREFIX   # or your GNU Radio prefix
make -j"$(nproc)" && make install
```

No extra flags to `run_sdrexplorer.sh` — once `from gnuradio import rds`
succeeds in the environment the app runs in (or key `gr_rds` names your install prefix), picking **WFM** starts decoding
RDS automatically and the station name appears in the tuning display within a
few seconds of a strong signal locking. Without gr-rds, WFM still demodulates
audio normally; the digital-decode status just notes RDS isn't available.

### GPU acceleration (optional)

**`gr-sdrexplorer_gpu`** is an in-repo GNU Radio OOT module that offloads two
stages to an NVIDIA GPU via CUDA/cuFFT:

- the stage-1 channelizer (frequency-translating decimation filter), and
- the spectrum/waterfall log-power FFT probes — this is what lets large FFT
  sizes (16k–64k bins) or wide capture bandwidths run without spiking the CPU.

It's entirely optional: without it (or without an NVIDIA GPU/driver), the app
runs the equivalent stock GNU Radio CPU blocks with no functional difference.
`radio.py` detects the module at import time and picks GPU blocks automatically
whenever it's importable.

Build it (needs the CUDA Toolkit and an NVIDIA GPU):

```bash
cd gr-sdrexplorer_gpu
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"
```

sdrexplorer finds `gr-sdrexplorer_gpu/build/` by itself (key `gr_sdrexplorer_gpu` to
use a build elsewhere) — nothing else to configure. See
[`gr-sdrexplorer_gpu/README.md`](gr-sdrexplorer_gpu/README.md) for what it
provides and how to test it standalone.

Command-line flags control which path runs, regardless of what's built:

```bash
./run_sdrexplorer.sh --no-gpu    # force CPU, even if the CUDA module is available
./run_sdrexplorer.sh --gpu       # require GPU (warns and falls back to CPU if unavailable)
                                  # default (neither flag): GPU if available, else CPU
```

The SDR status line in the Radio view shows a "· GPU" tag when the GPU path is
actually active.

## Notes

- Runtime data and RadioReference credentials live outside the repo (see
  [Where sdrexplorer finds things](#where-sdrexplorer-finds-things)); copy
  `sdrexplorer/config.example.json` to `~/.config/sdrexplorer/config.json` to enable the RR data source.
- `sdrexplorer/samples/boston_fcc_sample.csv` is a sample channel list to try Memories ▸ Import ▸ CSV file
  with: 140 licensed channels around Boston from the FCC's public license data (`_gen_boston_fcc.py` beside it
  rebuilds it, or one for another area, from your own FCC download).

## License

sdrexplorer is free software: you can redistribute it and/or modify it under the terms of the GNU General
Public License as published by the Free Software Foundation, either version 3 of the License, or (at your
option) any later version ([LICENSE](LICENSE)). Each source file says so in its `SPDX-License-Identifier`
line.

`gr-sdrexplorer_gpu` adds a GPLv3 section 7 permission to link with the NVIDIA CUDA libraries
([gr-sdrexplorer_gpu/LICENSE-EXCEPTION](gr-sdrexplorer_gpu/LICENSE-EXCEPTION)). Leaflet
(`sdrexplorer/web/leaflet/`) is BSD-2-Clause. The sample channel list comes from FCC public-domain data.
