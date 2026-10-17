# Installing & running sdrexplorer

There are two ways in:

- **Debian / Ubuntu: the install script.** It installs the core and the modules you pick, builds what they
  need (op25, dsd-fme, gr-pager, gr-rds…) at tested versions with sdrexplorer's patches, and sets everything
  up. Tested on Ubuntu 24.04 and Debian 12.
- **Everything else (other Linux distributions, macOS, Windows): conda.** You get the core (NFM / AM / WFM /
  SSB / CW, scanning, recording, transmit) and the paging module's POCSAG. The digital and classifier modules
  are Linux-only.

---

## Debian / Ubuntu: the install script

```bash
mkdir -p ~/sdrexplorer/src && cd ~/sdrexplorer/src
git clone https://github.com/thebadlarry/sdrexplorer
./sdrexplorer/install.sh
```

The first run checks your system packages. If any are missing it prints **one** `sudo apt-get install …`
command and stops (the script never runs sudo itself). Run that command, then run `./sdrexplorer/install.sh`
again. The second run fetches the modules, builds their external parts (a few minutes the first time; longer on a small machine) and
finishes with a check of every module.

Start sdrexplorer with:

```bash
~/sdrexplorer/bin/sdrexplorer
```

### Choosing what to install

| Command | Installs |
|---|---|
| `./sdrexplorer/install.sh` | the core, plus paging, digital and classifier (the default set) |
| `./sdrexplorer/install.sh paging` | the core and the paging module only |
| `./sdrexplorer/install.sh none` | the core only |
| `./sdrexplorer/install.sh all` | everything, including **train** (builds classifier models; needs PyTorch, a 2–3 GB download) |
| `--without rds,adsb,osmosdr` | leave out the core's optional RDS (WFM station names) or ADS-B (aircraft) parts, or the patched gr-osmosdr build (the system's is used; releasing PTT on a HackRF can then hang) |
| `--no-venvs` | don't create the modules' Python environments (the classifier's ONNX Runtime, train's PyTorch) |
| `--no-models` | don't install the classifier's trained models |
| `--update` | pull the newest version of each repo; builds are redone only where something changed |
| `--portable` | keep settings, memories and recordings under `~/sdrexplorer/data` instead of your home folders |
| `--prefix DIR` | install somewhere other than `~/sdrexplorer` |
| `--dry-run` | show what would be done |

Running the script again is safe: it keeps what is there and only does what's missing. To add a module
later, run it with that module's name.

### What ends up where

```
~/sdrexplorer/
  bin/sdrexplorer        the launcher
  src/                   sdrexplorer and the module repos (git checkouts: --update pulls them)
  ext/                   the external builds (gr-osmosdr, op25, dsd-fme, gr-pager, gr-rds, gr-adsb)
  venv-onnx/             the classifier's ONNX Runtime
  paths.json             where things are that sdrexplorer wouldn't find by itself
  logs/                  one log per build; the script points at it if a build fails
```

With the classifier, the script also installs its trained models: the current release of the
**sdrexplorer-models** repo (checked out in `src/`), whose model files are downloaded from that release and
checked against their sha256. They go into the classifier's models folder,
`~/.local/share/sdrexplorer/models`. Models you trained and deployed yourself with sdrexplorer-train are
never replaced. The models are licensed CC BY 4.0 (the code is GPL-3.0-or-later).

Your data (memories, recordings, settings) is in `~/.local/share/sdrexplorer` and `~/.config/sdrexplorer`,
not in the install folder, so removing or reinstalling `~/sdrexplorer` keeps it.

`~/sdrexplorer/bin/sdrexplorer --paths` lists every external part and whether it was found.

### External builds and their patches

The script builds each external part at a pinned commit. Where sdrexplorer needs a fix that isn't upstream
yet, it applies a patch from the repository that needs it (`patches/`). Each fix is offered upstream, and
a patch shrinks or goes away as the project merges it. The status below is as of sdrexplorer 1.0.0; each
patch file's `Upstream-Status:` header has the current one.

| Project | Pinned at | Patch | What it fixes | Upstream (1.0.0) |
|---|---|---|---|---|
| [gr-osmosdr](https://gitea.osmocom.org/sdr/gr-osmosdr) | `73d8982` (v0.2.5) | `patches/gr-osmosdr-hackrf-sink-stop.patch` | Releasing PTT on a HackRF could hang the app: `hackrf_sink_c::stop()` waited for a wakeup that could be missed. It now re-checks every 100 ms. | not yet offered |
| [gr-rds](https://github.com/bastibl/gr-rds) | `58facb6` (maint-3.10) | `patches/gr-rds-include-thread.patch` | Adds `#include <thread>`, without which it doesn't build with GCC 12 (Debian 12). | [pull request open](https://github.com/bastibl/gr-rds/pulls) |
| [gr-adsb](https://github.com/mhostetter/gr-adsb) | `b1b5b13` | none | | |
| [op25](https://github.com/boatbod/op25) | `da5374d` | sdrexplorer-digital's `patches/op25-sdrexplorer.patch` | DMR color codes in the decoder's messages, the DMR transmitter's embedded LC and idle slot, P25 TX NAC and Link Control, NXDN TX filter taps, a DMR mobile-station transmitter | per change: see the [sdrexplorer-digital README](https://github.com/thebadlarry/sdrexplorer-digital#the-op25-patch) |
| [mbelib](https://github.com/lwvmobile/mbelib), [dsd-fme](https://github.com/lwvmobile/dsd-fme) | `30dc790`, `69d3115` | none | | |
| [gr-pager](https://github.com/troycurtisjr/gr-pager) | `bca8638` | none | Its GNU Radio 3.10 port was merged upstream ([#9](https://github.com/troycurtisjr/gr-pager/pull/9)), so the patch sdrexplorer used to carry is gone. | merged |

`--without osmosdr` skips the patched gr-osmosdr and uses the system's; everything works, except that
releasing PTT on a HackRF can then hang.

### Another distribution

The script uses Debian package names. On another distribution, install the equivalents of the packages it
lists (GNU Radio 3.10 with its development files, gr-osmosdr, PyQt5, numpy, scipy, cmake, and the libraries
named), then run it with `--skip-apt-check`.

---

## Everything else: conda

The only tricky dependency is **GNU Radio** (not a pip package). **conda** (via Miniforge) installs GNU Radio
and the HackRF / RTL-SDR support on Linux, macOS and Windows the same way.

### 1. Install Miniforge (gives you `conda`)

Skip this if you already have conda, mamba or radioconda.

- **Linux / macOS**, in a terminal:
  ```bash
  curl -L -o miniforge.sh https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-$(uname)-$(uname -m).sh
  bash miniforge.sh -b        # installs to ~/miniforge3
  ~/miniforge3/bin/conda init "$(basename "$SHELL")"
  ```
  Close and reopen the terminal so `conda` is on your PATH.

- **Windows**: download and run the **Miniforge3 Windows x86_64** installer from
  <https://conda-forge.org/download/>, then open **"Miniforge Prompt"** from the Start menu for the commands
  below.

### 2. Get the code

```bash
git clone https://github.com/thebadlarry/sdrexplorer
cd sdrexplorer
```
(No git? Download the repo ZIP from its web page, unzip it, and `cd` in.)

### 3. Create the environment (installs GNU Radio and everything else)

```bash
conda env create -f environment.yml
conda activate sdrexplorer
```
This is the slow step (a few minutes). You only do it once.

### 4. Run it

- **Linux / macOS** (from the repo folder): `./run_sdrexplorer.sh`
- **Windows** (in the Miniforge Prompt, environment activated): `run_sdrexplorer.bat`
- Any system: `python -m sdrexplorer.app`

Next time, `conda activate sdrexplorer` and run step 4 again.

### Modules with conda

Clone a module's repo next to this one (`sdrexplorer-paging` beside `sdrexplorer`), or add its folder in
Settings ▸ Modules ▸ Add module…. External builds a module needs are described in its README; their
locations go in `paths.json` (see `paths.example.json`).

---

## Device drivers / permissions

- **Linux:** add yourself to the device group so the radio works without root:
  ```bash
  sudo usermod -aG plugdev "$USER"     # then log out and in
  ```
  The hackrf and rtl-sdr packages install the udev rules. Check with `hackrf_info` or `rtl_test`.
- **macOS:** no driver install needed for HackRF / RTL-SDR.
- **Windows:** for an **RTL-SDR**, replace its driver with **WinUSB** using
  [Zadig](https://zadig.akeo.ie/) (select the RTL device, install WinUSB). A HackRF works with its stock
  driver.

If the app opens but shows "not connected", the device isn't visible: check the cable, the **SDR** picker on
the Radio page, and the driver / permission steps above, then press **Start**.

---

## Optional: GPU acceleration (CUDA)

With an **NVIDIA GPU and the CUDA Toolkit**, building the in-repo `gr-sdrexplorer_gpu` module moves the
channel filter and the spectrum / waterfall FFT to the GPU (useful at wide capture bandwidths). Without it the
app runs the equivalent CPU blocks.

```bash
cd gr-sdrexplorer_gpu          # in ~/sdrexplorer/src/sdrexplorer with the install script
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"
```

sdrexplorer finds the `build/` folder by itself. `--no-gpu` / `--gpu` on the launch command force the CPU or
require the GPU. See [`gr-sdrexplorer_gpu/README.md`](gr-sdrexplorer_gpu/README.md).
