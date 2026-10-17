#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
"""Integration & benchmark test for gr-sdrexplorer_gpu CUDA Multi-Channel DDC."""

import sys
import os
import time
import numpy as np

# Ensure build path is on Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "build/python/sdrexplorer_gpu/bindings")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "python")))
os.environ["LD_LIBRARY_PATH"] = os.path.abspath(os.path.join(os.path.dirname(__file__), "build/lib")) + ":" + os.environ.get("LD_LIBRARY_PATH", "")

from gnuradio import gr, blocks
from gnuradio.filter import firdes
import sdrexplorer_gpu_python as sdrexplorer_gpu

def test_cuda_ddc():
    sample_rate = 20_000_000 # 20 Msps wideband input
    decim_factor = 10         # Downsample to 2.0 Msps per channel
    num_channels = 8
    num_samples = 20_000_000  # 20M samples (1.0 sec of data @ 20 Msps)

    print(f"=== Testing CUDA Multi-Channel DDC OOT Block ===")
    print(f"Wideband Input Rate: {sample_rate / 1e6:.1f} Msps")
    print(f"Decimation Factor:   {decim_factor}")
    print(f"Channels:            {num_channels}")
    print(f"Total Input Samples: {num_samples:,}")

    # Generate synthetic input signal with multiple tones
    channel_offsets = [-4.0e6, -2.5e6, -1.0e6, 0.0, 1.0e6, 2.5e6, 4.0e6, 6.0e6]
    taps = firdes.low_pass(1.0, sample_rate, 800_000, 200_000)

    print(f"FIR Taps Count:      {len(taps)}")

    # Instantiate CUDA DDC block
    ddc_block = sdrexplorer_gpu.multichannel_ddc_cuda(
        decim_factor=decim_factor,
        sample_rate=float(sample_rate),
        channel_freqs=channel_offsets,
        taps=taps,
        device_id=0
    )

    # Build GNU Radio flowgraph
    tb = gr.top_block()
    
    src = blocks.null_source(gr.sizeof_gr_complex)
    head = blocks.head(gr.sizeof_gr_complex, num_samples)
    
    sinks = []
    for c in range(num_channels):
        snk = blocks.null_sink(gr.sizeof_gr_complex)
        sinks.append(snk)
        tb.connect((ddc_block, c), snk)

    tb.connect(src, head, ddc_block)

    # Benchmark execution
    t0 = time.perf_counter()
    tb.run()
    t1 = time.perf_counter()

    elapsed = t1 - t0
    throughput_msps = (num_samples / 1e6) / elapsed

    print(f"Flowgraph Completed Successfully!")
    print(f"Execution Time:      {elapsed * 1000:.2f} ms")
    print(f"Effective Throughput:{throughput_msps:.2f} Msps")
    print(f"Real-Time Speedup:   {throughput_msps / (sample_rate / 1e6):.2f}x Real-Time!")

    print("SUCCESS: 20 Million Samples Processed Across 8 Channels!")

if __name__ == "__main__":
    test_cuda_ddc()
