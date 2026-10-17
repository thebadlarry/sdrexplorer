# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
cmake_minimum_required(VERSION 3.18)
project(gr-sdrexplorer_gpu CXX CUDA)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CUDA_STANDARD 17)
set(CMAKE_CUDA_STANDARD_REQUIRED ON)

if(NOT CMAKE_CUDA_ARCHITECTURES)
  set(CMAKE_CUDA_ARCHITECTURES "native")
endif()

# Find CUDA Toolkit & GNU Radio 3.10
find_package(CUDAToolkit REQUIRED)
find_package(Gnuradio "3.10" REQUIRED COMPONENTS runtime filter fft)

include_directories(
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${GNURADIO_TARGET_INCLUDE_DIRS}
    ${CMAKE_CUDA_TOOLKIT_INCLUDE_DIRECTORIES}
)

add_subdirectory(include/gnuradio/sdrexplorer_gpu)
add_subdirectory(lib)
add_subdirectory(python/sdrexplorer_gpu)
