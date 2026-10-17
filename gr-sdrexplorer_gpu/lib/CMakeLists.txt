# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
add_library(gnuradio-sdrexplorer_gpu SHARED
    multichannel_ddc_cuda_impl.cc
    multichannel_ddc_kernel.cu
    logpwrfft_cuda_impl.cc
    logpwrfft_kernel.cu
)

target_link_libraries(gnuradio-sdrexplorer_gpu
    PRIVATE
        gnuradio::gnuradio-runtime
        gnuradio::gnuradio-filter
        CUDA::cudart
        CUDA::cufft
)

target_include_directories(gnuradio-sdrexplorer_gpu
    PUBLIC
        $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/../include>
        $<INSTALL_INTERFACE:include>
)

set_target_properties(gnuradio-sdrexplorer_gpu PROPERTIES
    DEFINE_SYMBOL "__DLL_EXPORT"
)

install(TARGETS gnuradio-sdrexplorer_gpu
    LIBRARY DESTINATION lib
    ARCHIVE DESTINATION lib
    RUNTIME DESTINATION bin
)
