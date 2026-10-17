#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Install sdrexplorer and its modules on Debian / Ubuntu. ./install.sh --help for the options;
# the work is in install.py (standard-library Python 3.11+).
set -e
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
    echo "python3 is missing: sudo apt-get install python3   (then run this again)" >&2
    exit 1
fi
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "sdrexplorer needs Python 3.11 or later; $PY is $("$PY" -V 2>&1)." >&2
    exit 1
fi
exec "$PY" "$(dirname "$0")/install.py" "$@"
