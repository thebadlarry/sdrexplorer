# The `sdrexplorer` package

The core program. Run it from the repository root with `./run_sdrexplorer.sh` or `python -m sdrexplorer.app`.

- What each file does, and how they fit together: [docs/architecture.md](../docs/architecture.md)
  (§15 lists the source tree).
- The public module API is `sdrexplorer.api` ([docs/module-api.md](../docs/module-api.md)); everything else
  in this package is private.
- Tests are in `tests/` ([docs/architecture.md §16](../docs/architecture.md#16-tests)).
- `verify_hw.py` is a headless smoke test against a real SDR: `python3 -m sdrexplorer.verify_hw`.
