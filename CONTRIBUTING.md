# Contributing to sdrexplorer

Thanks for helping. sdrexplorer is developed in the open on GitHub: bugs and ideas go in the issue tracker,
changes come in as pull requests. This file covers the core repository; the module repositories
(`sdrexplorer-paging`, `-digital`, `-classifier`, `-train`) work the same way.

## Reporting a bug

Open an issue in the repository the problem is in (the core, or the module whose feature misbehaves; if
unsure, the core). Please include:

- what you did, what you expected, and what happened instead;
- your sdrexplorer version (the release, or `git log -1 --oneline` for a checkout), your OS and distribution,
  your GNU Radio version, and the SDR you use;
- the output of `sdrexplorer --paths` (or `./run_sdrexplorer.sh --paths`), which lists what was found;
- the terminal output around the problem (start sdrexplorer from a terminal to see it). For a crash, start it
  with `python3 -X faulthandler -m sdrexplorer.app` and include the stack it prints. If it hangs (the window
  stops responding), find its process id (`pgrep -af sdrexplorer.app`; with several running, the one that
  hangs) and run `kill -USR1 <pid>` in another terminal: sdrexplorer prints every thread's Python stack to
  the terminal it was started from; include that. For a hang inside GNU Radio or a device driver, also
  include the native stacks: `sudo gdb -p <pid> -batch -ex "thread apply all bt" > hang.txt 2>&1` (sudo:
  most distributions don't let a debugger attach to a running program otherwise);
- for an install problem, the `install.sh` output and the log it points to in `~/sdrexplorer/logs/`.

**Don't post real radio data in public issues** (recordings, decode logs, payload dumps, map screenshots):
they can contain other people's voice, unit ids and positions. If a bug needs a recording, say so in the
issue and we'll arrange a private way, or use `python -m sdrexplorer.contribute crop` to share a short,
stripped crop of the signal.

## Suggesting a feature

Open an issue describing what you'd like to do and why. For anything large (a new decoder, a new SDR
driver, a change to the module API), please discuss it in an issue before writing the code, so we can agree
on where it belongs: in the core, or in a module.

## Pull requests

1. Fork the repository and create a branch from `main`.
2. Keep each pull request to one change: a fix, a feature, or a cleanup. Small pull requests get reviewed
   faster.
3. Match the surrounding code: its naming, comment style and density. New source files start with the
   license header used everywhere else:
   ```python
   # SPDX-License-Identifier: GPL-3.0-or-later
   ```
4. Add or update tests for what you changed, and run the test suites (below). They must pass.
5. Update the documentation the change touches: the [user manual](docs/user-manual.md) for anything a user
   sees, the [module API reference](docs/module-api.md) for any API change (with an entry in its changelog),
   the README for install or feature changes.
6. Describe in the pull request what changed, why, and how you tested it (for radio features: with what
   hardware or recording).

By submitting a pull request you agree that your contribution is licensed under the project's license,
GPL-3.0-or-later ([LICENSE](LICENSE)).

### Running the tests

From the repository root (no SDR or display needed; the window tests run offscreen):

```bash
QT_QPA_PLATFORM=offscreen python3 -m sdrexplorer.tests.test_modules
QT_QPA_PLATFORM=offscreen python3 -m sdrexplorer.tests.test_ui_restructure
python3 -m sdrexplorer.tests.test_iqring
python3 -m sdrexplorer.tests.test_core_generic
python3 -m sdrexplorer.tests.test_contribute
python3 -m sdrexplorer.tests.test_install
```

`python3 tools/demo.py` runs the app without an SDR on a synthetic band with made-up decodes; for a
change to what the window looks like, `python3 tools/demo.py --shots DIR` remakes the screenshots in
`docs/images/` (under `xvfb-run` on a headless machine).

Each module repository has its own suite (`QT_QPA_PLATFORM=offscreen python3 tests/test_<name>.py`), run
with the core checked out beside it. A change to the installer should also be tried on a clean Debian /
Ubuntu system (a container is fine).

### Rules that keep the project working

- **The core names no module.** The core never refers to a module, protocol or decoder by name; modules find
  each other through the API. `test_core_generic` checks this.
- **Modules use only `sdrexplorer.api`.** Everything else in the core is private and may change.
- **Only the Qt main thread touches widgets.** See [Threads](docs/module-api.md#5-threads).
- **Nothing hard-coded to one machine.** Paths go through `sdrexplorer.paths` keys.
- **No real radio data in the repository**: test fixtures use made-up frequencies, ids and positions.

## Writing a module

A module is the way to add a feature with its own dependencies: a decoder, an SDR driver, an import source,
an analysis tool. It lives in its own repository, and users add it with Settings ▸ Modules ▸ Add module….
Start with the [module tutorial](docs/module-tutorial.md) and the
[example module](examples/sdrexplorer-activity/); the [module API reference](docs/module-api.md) has the
details. If you'd like your module listed in the installer, open an issue.

## Sharing recordings for training

The signal classifier gets better with real recordings of real signals. To share one, first cut it down
with the contribution tool, which keeps short crops of the signal and drops the frequency, time, position
and ids:

```bash
python -m sdrexplorer.contribute crop <recording.cs16> --label <MODE>
```

See [Sharing recordings](docs/user-manual.md#sharing-recordings) in the user manual for what is kept and
what isn't. Then open an issue saying what you have (the label, how many crops); we'll reply with where to
send the zip. Please don't attach it to a public issue.

## Questions

Open an issue, or start a discussion if the repository has Discussions enabled.
