#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
# Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
"""Correctness + benchmark test for gr-sdrexplorer_gpu CUDA logpwrfft_cuda:
compares its output bin-for-bin against GNU Radio's stock logpwrfft_c for the
same synthetic input, then benchmarks throughput at a large (64k) FFT size."""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                 "build/python/sdrexplorer_gpu/bindings")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "python")))
os.environ["LD_LIBRARY_PATH"] = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "build/lib")) + ":" + os.environ.get(
        "LD_LIBRARY_PATH", "")

from gnuradio import gr, blocks
from gnuradio.fft import logpwrfft
import sdrexplorer_gpu_python as sdrexplorer_gpu


def run_chain(block, src_data, nsamples, fft_size):
    tb = gr.top_block()
    src = blocks.vector_source_c(src_data, False)
    head = blocks.head(gr.sizeof_gr_complex, nsamples)
    sink = blocks.vector_sink_f(fft_size)
    tb.connect(src, head, block)
    tb.connect(block, sink)
    t0 = time.perf_counter()
    tb.run()
    t1 = time.perf_counter()
    return np.array(sink.data(), dtype=np.float64), (t1 - t0)


def test_correctness():
    fft_size = 2048
    sample_rate = 2_400_000.0
    frame_rate = 30.0
    avg_alpha = 1.0
    average = False
    decim = max(1, round(sample_rate / fft_size / frame_rate))
    nsamples = decim * fft_size * 8  # 8 decimated frames

    rng = np.random.RandomState(42)
    data = (rng.randn(nsamples) + 1j * rng.randn(nsamples)).astype(np.complex64)

    cpu = logpwrfft.logpwrfft_c(sample_rate, fft_size, 2, frame_rate, avg_alpha,
                                average, None, True)
    gpu = sdrexplorer_gpu.logpwrfft_cuda(sample_rate, fft_size, 2, frame_rate,
                                         avg_alpha, average, True)

    cpu_out, _ = run_chain(cpu, data, nsamples, fft_size)
    gpu_out, _ = run_chain(gpu, data, nsamples, fft_size)

    n = min(cpu_out.size, gpu_out.size)
    n -= n % fft_size
    print(f"CPU frames: {cpu_out.size // fft_size}  GPU frames: {gpu_out.size // fft_size}")
    assert n > 0, "no frames produced"
    cpu_out = cpu_out[:n].reshape(-1, fft_size)
    gpu_out = gpu_out[:n].reshape(-1, fft_size)

    diff = np.abs(cpu_out - gpu_out)
    print(f"max |diff| dB: {diff.max():.4f}   mean |diff| dB: {diff.mean():.4f}")
    print("CPU frame0[:8] :", np.round(cpu_out[0, :8], 3))
    print("GPU frame0[:8] :", np.round(gpu_out[0, :8], 3))
    assert diff.max() < 0.5, f"CUDA logpwrfft diverges from CPU logpwrfft_c by {diff.max()} dB"
    print("PASS: logpwrfft_cuda matches logpwrfft_c within 0.5 dB")


def test_benchmark():
    fft_size = 65536
    sample_rate = 20_000_000.0
    frame_rate = 30.0
    decim = max(1, round(sample_rate / fft_size / frame_rate))
    nsamples = decim * fft_size * 50

    rng = np.random.RandomState(1)
    data = (rng.randn(nsamples) + 1j * rng.randn(nsamples)).astype(np.complex64)

    gpu = sdrexplorer_gpu.logpwrfft_cuda(sample_rate, fft_size, 2, frame_rate,
                                         0.7, True, True)
    out, elapsed = run_chain(gpu, data, nsamples, fft_size)
    frames = out.size // fft_size
    print(f"=== 64k-bin FFT benchmark ===")
    print(f"fft_size={fft_size}  input_rate={sample_rate/1e6:.1f} Msps  "
         f"samples={nsamples:,}  frames_out={frames}")
    print(f"elapsed={elapsed*1000:.2f} ms  "
         f"throughput={nsamples/1e6/elapsed:.2f} Msps "
         f"({nsamples/1e6/elapsed/(sample_rate/1e6):.2f}x real-time)")


if __name__ == "__main__":
    test_correctness()
    test_benchmark()
