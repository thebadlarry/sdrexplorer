# sdrexplorer-activity (example module)

A small, complete sdrexplorer module, written as the worked example for
[docs/module-tutorial.md](../../docs/module-tutorial.md). It finds busy channels in the captured band and:

- tags them on the waterfall (`ui.annotate`);
- lists them in an "Activity" tab under the waterfall, where a double-click tunes (`Panel`);
- adds View ▸ Band activity markers (`MenuEntry`), a right-click "Log this signal" entry
  (`context_entries` → `Decode`), and an "NFM-25" entry in the Mode picker (`ModeSpec` + `DemodSpec`);
- answers the bus request `activity.peaks` for other modules;
- stores the measured level on memory channels saved while tuned to one (`channel_fields`);
- keeps its on/off state and threshold (`save_state` / `load_state`).

It needs only numpy. It reads the core's display FFT (`api.spectrum`) and adds nothing to the flowgraph.

## Try it

It sits in the core's `examples/` folder, where it isn't loaded automatically. From the core checkout:

```bash
SDREXPLORER_MODULE_DIRS=$PWD/examples/sdrexplorer-activity ./run_sdrexplorer.sh
```

Or copy it next to the sdrexplorer checkout (`…/src/sdrexplorer-activity`), or add its folder in
Settings ▸ Modules ▸ Add module….

## Tests

```bash
QT_QPA_PLATFORM=offscreen python3 tests/test_activity.py
```

The first test checks the peak finder; the second loads the module into the real main window offscreen
(no SDR needed) and drives it with a synthetic spectrum frame.

## Files

| File | |
|---|---|
| `pyproject.toml` | the `sdrexplorer.modules` entry point |
| `module.json` | metadata for the install script |
| `src/sdrexplorer_activity/detect.py` | the peak finder (numpy only) |
| `src/sdrexplorer_activity/module.py` | the module |
| `tests/test_activity.py` | tests |

## License

GPL-3.0-or-later, like sdrexplorer.
