# gr-sdrexplorer_gpu

A small GNU Radio 3.10 out-of-tree (OOT) module providing CUDA-accelerated
replacements for two stages of sdrexplorer's flowgraph. It's entirely optional —
`sdrexplorer/radio.py` imports it lazily and falls back to the equivalent stock
GNU Radio CPU blocks if it isn't built or no CUDA GPU is present.

## Blocks

| Block | Replaces | What it does |
|-------|----------|---------------|
| `multichannel_ddc_cuda` | `gr-filter`'s `freq_xlating_fir_filter_ccf` | Heterodyne shift + FIR low-pass + decimate, N channels at once, on GPU. sdrexplorer uses it with a single channel as the stage-1 wideband→`QUAD_RATE` channelizer. |
| `logpwrfft_cuda` | `gr-fft`'s `logpwrfft_c` | Windowed FFT (cuFFT) → \|·\|² → single-pole IIR average → `10*log10` scaling, fftshift. Bit-for-bit-compatible math with `logpwrfft_c` (same blackman-harris window, same decimation-to-frame-rate formula, same nlog10 offset), so it's a drop-in for the spectrum/waterfall and scan-measurement probes. Exists so large FFT sizes (16k–64k bins) or wide capture bandwidths don't spike the CPU. |

Both are plain `gr::sync_decimator` blocks (no scheduler surprises): one CUDA
stream per block instance, host↔device copies per `work()` call, device buffers
grown (never shrunk) as needed.

## Build

Needs the CUDA Toolkit (built/tested against CUDA 12.0) and GNU Radio 3.10
dev headers (`runtime`, `filter`, `fft` components) already required by the
main app.

```bash
cd gr-sdrexplorer_gpu
mkdir -p build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
make -j"$(nproc)"
```

`CMAKE_CUDA_ARCHITECTURES` defaults to `native` (build for the GPU that's
present); override it if cross-compiling for a different card.

Nothing needs installing system-wide: `run_sdrexplorer.sh` (in the repo root)
auto-detects `gr-sdrexplorer_gpu/build/` and puts
`build/python/sdrexplorer_gpu/{,bindings}` on `PYTHONPATH` and `build/lib` on
`LD_LIBRARY_PATH`. If you're not using that launcher, do the same by hand, or
`cmake --install .` into a prefix already on those paths.

## Test

Each block has a standalone correctness + benchmark script (build the module
first):

```bash
python3 test_multichannel_ddc_cuda.py   # channelizer throughput benchmark
python3 test_logpwrfft_cuda.py          # logpwrfft_cuda vs CPU logpwrfft_c bin-for-bin diff, + 64k-bin benchmark
```

`test_logpwrfft_cuda.py` asserts the GPU and CPU log-power outputs agree to
within 0.5 dB (in practice they match to ~0.0002 dB) — that's the check to
re-run after touching `logpwrfft_kernel.cu` or `logpwrfft_cuda_impl.cc`.

## Runtime selection

`radio.py`'s `ScannerRadio` picks the GPU path automatically when this module
imports successfully, or use the `use_gpu` constructor kwarg (`None` = auto,
`True`/`False` force GPU/CPU) — exposed at the app level as the `--gpu` /
`--no-gpu` command-line flags (see the top-level README).

## Layout

```
include/gnuradio/sdrexplorer_gpu/   public block headers + api.h
lib/                                 block impls (*_impl.cc/h) + CUDA kernels (*_kernel.cu/h)
python/sdrexplorer_gpu/              Python package + pybind11 bindings
```

## License

GPL-3.0-or-later, like the rest of sdrexplorer, with an additional permission (GPLv3 section 7) to link
with the NVIDIA CUDA Toolkit libraries: see [LICENSE-EXCEPTION](LICENSE-EXCEPTION).
