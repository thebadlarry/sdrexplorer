// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#ifndef INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_KERNEL_H
#define INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_KERNEL_H

#include <complex>
#include <cstddef>

namespace gr {
namespace sdrexplorer_gpu {

// cuFFT-backed engine behind logpwrfft_cuda. Mirrors gr-fft's logpwrfft_c
// pipeline (stream_to_vector_decimator -> windowed FFT -> |.|^2 -> single-pole
// IIR average -> nlog10) entirely on GPU.
class LogPwrFftCudaEngine {
public:
    explicit LogPwrFftCudaEngine(int device_id = 0);
    ~LogPwrFftCudaEngine();

    // window_power/offset math matches gr::fft::window::blackman_harris(N, 92)
    // and gr-fft's logpwrfft nlog10 scale/offset exactly.
    bool init(int fft_size, float ref_scale, float avg_alpha, bool average, bool shift);

    // `in` holds noutput_items * block_stride complex samples. Each output
    // vector's FFT window is the LAST fft_size samples of its block_stride
    // slice; the leading (block_stride - fft_size) samples of each slice are
    // the "keep 1 in N" decimation gap and are not used (matches GR's
    // stream_to_vector_decimator, which drops whole skipped vectors rather
    // than folding them into the average).
    bool execute(const std::complex<float>* in,
                size_t block_stride,
                size_t noutput_items,
                float* out);

private:
    int d_device_id;
    int d_fft_size;
    float d_alpha;
    float d_offset_db;
    bool d_shift;

    struct Impl;
    Impl* d_impl;
};

} // namespace sdrexplorer_gpu
} // namespace gr

#endif /* INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_KERNEL_H */
