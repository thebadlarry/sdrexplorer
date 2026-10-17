# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
add_subdirectory(bindings)

install(FILES
    __init__.py
    DESTINATION lib/python3/dist-packages/sdrexplorer_gpu
)
