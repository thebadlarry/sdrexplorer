#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Portable launcher for sdrexplorer (Linux / macOS). Runs `python -m sdrexplorer.app`.
#
# Prerequisites: GNU Radio 3.10 + gr-osmosdr + PyQt5 + numpy importable by the
# python it launches (a distro install, or an activated radioconda env). See
# README for per-OS setup.
#
# Optional external builds (gr-rds, gr-adsb, the CUDA module, and modules' own
# builds) are found by sdrexplorer itself -- no environment needed here. Each is
# looked up in this order: an SDREXPLORER_<KEY> variable, ~/.config/sdrexplorer/
# paths.json, <install>/paths.json (see paths.example.json), <install>/ext/<name>.
# To see what was found:
#
#   ./run_sdrexplorer.sh --paths
#
# The old ADSB_PATH variable (and modules' old names) still work.
#
#   ./run_sdrexplorer.sh --no-gpu     # force CPU, even with the CUDA module built
#   ./run_sdrexplorer.sh --portable   # keep data under <install>/data, not ~/.local/share
#
# Machine-specific environment fixes (e.g. scrubbing a second GNU Radio off the
# path) go in run_sdrexplorer.local.sh next to this script: it's sourced if
# present and git-ignored.
#
# On Windows use run_sdrexplorer.bat (or just `python -m sdrexplorer.app`).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$HERE"                              # repo root; holds the sdrexplorer/ package
PYTHON="${PYTHON:-python3}"

# shellcheck source=/dev/null
[ -f "$HERE/run_sdrexplorer.local.sh" ] && . "$HERE/run_sdrexplorer.local.sh"

cd "$REPO"                                 # run as a package so relative imports resolve
exec "$PYTHON" -m sdrexplorer.app "$@"
