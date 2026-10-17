# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
import os

try:
    from gnuradio import gr
    from .sdrexplorer_gpu_python import *
except ImportError:
    pass
