# Real-data store format (format 1)

The store holds labeled I/Q crops of real signals. sdrexplorer's "Label as…" and the auto-labelers
write it; sdrexplorer-train reads it for training and evaluation. Code: `sdrexplorer.api.store`.
Location: paths key `store_dir` (default `~/.local/share/sdrexplorer/realdata`).

```
<store_dir>/
  held_out.json                       {"<profile>": ["<recording>", ...],
                                       "keep_out": {"<profile>": [{"recording" | "id": ..., "why": ...}]}}
  <profile>/<label>/<id>/iq.npy       complex64, 1-D, at the profile's rate, ~2 s (store.CROP_S)
  <profile>/<label>/<id>/meta.json
```

- `<profile>`: the model input it was cut for, e.g. `vu` (24 kHz) or `vu48` (48 kHz). One crop is
  often saved to two profiles, at each rate.
- `<label>`: the class name, e.g. `NBFM`, `DMR`, `NXDN96`, `FLEX`. Characters outside
  `[A-Za-z0-9_.+-]` become `_` in the directory name; `meta.json` keeps the real label.
- `<id>`: `YYYYMMDD-HHMMSS-<6 hex>`, unique per example.
- The signal is centered at 0 Hz.

## meta.json

| Key | Type | Required | |
|---|---|---|---|
| `format` | int | written since format 1 was named (missing = 1) | this spec's version |
| `label` | str | yes | class name |
| `profile` | str | yes | |
| `fs` | float | yes | sample rate of `iq.npy`, Hz |
| `n` | int | yes | number of samples |
| `freq_hz` | float | yes | RF center frequency; `0` when withheld (a shared contribution) |
| `recording` | str | yes | the held-out split key: the capture file name (without `.cs16`), `live-YYYYMMDD-<MHz>`, or `contrib-<random>` for a shared contribution (one per package) |
| `source` | str | yes | `"manual"` (a person labeled it), `"sync"` (sync-word / decoder auto-label), `"submitted"` (someone else's contribution, `python -m sdrexplorer.contribute`) |
| `band` | str | no | a coarse band name (`"UHF"`, `"800 / 900"`) instead of a frequency, in contributions |
| `time` | str | yes | `YYYY-MM-DD HH:MM:SS`, local |
| `bw_hz` | float | no | occupied bandwidth estimate |
| `snr_db` | float | no | |
| `classifier_said` | str | no | the classifier's label when it was saved |
| `verify` | dict | no | a decoder proof's result (`ok`, `decoder`, `frames`, `ids`, …) |
| `real_hz` | float | no | recorded bandwidth when narrower than the profile; the rest is noise fill |
| others | | no | readers ignore keys they don't know |

## Rules for readers

- **Split by `recording`, never by window.** A recording in `held_out.json` is evaluation-only, for all
  of its examples. Near-duplicate windows of one transmission must not land on both sides.
- **Kept out** (`keep_out`, `store.keep_out` / `set_keep_out`): a recording (`"recording"`) or a single
  example (`"id"`, its directory name) on neither side: it would leak into the held-out split (same
  channel or system as a held-out recording), or it is one bad crop of an otherwise good capture. A
  reader that predates this key ignores it (it isn't a profile name: no profile is called `keep_out`).
- **Trust** (`store.trusted`): a `"sync"` example counts only with `verify.ok` true; a `"manual"`
  example counts unless `verify.ok` is false.
- `noise` and `carrier` windows are kept whole; others drop windows where the signal is off
  (`store.NO_SIGNAL_GATE`, `store.to_windows`).

## Changes

A change that old readers would misread bumps `format` and is listed here. Adding an optional
`meta.json` key doesn't.
