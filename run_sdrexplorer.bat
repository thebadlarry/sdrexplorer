@echo off
REM SPDX-License-Identifier: GPL-3.0-or-later
REM Copyright (C) 2026 Bad Larry
REM Windows launcher for sdrexplorer (the core and the paging module; some modules are Linux-only).
REM Requires GNU Radio 3.10 + gr-osmosdr + PyQt5 + numpy on PATH -- easiest via
REM an activated radioconda environment (see README). For RTL-SDR, install the
REM WinUSB driver with Zadig first.
setlocal
cd /d "%~dp0"
python -m sdrexplorer.app %*
