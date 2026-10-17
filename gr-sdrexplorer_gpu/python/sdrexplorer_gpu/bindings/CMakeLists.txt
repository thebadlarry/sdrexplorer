# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
find_package(pybind11 REQUIRED)

pybind11_add_module(sdrexplorer_gpu_python
    python_bindings.cc
    multichannel_ddc_cuda_python.cc
    logpwrfft_cuda_python.cc
)

target_link_libraries(sdrexplorer_gpu_python
    PRIVATE
        gnuradio-sdrexplorer_gpu
        gnuradio::gnuradio-runtime
)

target_include_directories(sdrexplorer_gpu_python
    PRIVATE
        ${CMAKE_CURRENT_SOURCE_DIR}/../../../include
)

install(TARGETS sdrexplorer_gpu_python
    DESTINATION lib/python3/dist-packages/sdrexplorer_gpu
)
