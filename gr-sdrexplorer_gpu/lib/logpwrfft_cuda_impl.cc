// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#include "logpwrfft_cuda_impl.h"
#include <gnuradio/io_signature.h>
#include <algorithm>
#include <cmath>

namespace gr {
namespace sdrexplorer_gpu {

namespace {

// matches gr::blocks::stream_to_vector_decimator's decimation formula
int compute_decim(float sample_rate, int fft_size, float frame_rate)
{
    double raw = double(sample_rate) / double(fft_size) / double(frame_rate);
    return std::max(1, int(std::lround(raw)));
}

} // namespace

logpwrfft_cuda::sptr logpwrfft_cuda::make(float sample_rate,
                                          int fft_size,
                                          float ref_scale,
                                          float frame_rate,
                                          float avg_alpha,
                                          bool average,
                                          bool shift,
                                          int device_id)
{
    return gnuradio::make_block_sptr<logpwrfft_cuda_impl>(
        sample_rate, fft_size, ref_scale, frame_rate, avg_alpha, average, shift,
        device_id);
}

logpwrfft_cuda_impl::logpwrfft_cuda_impl(float sample_rate,
                                         int fft_size,
                                         float ref_scale,
                                         float frame_rate,
                                         float avg_alpha,
                                         bool average,
                                         bool shift,
                                         int device_id)
    : gr::sync_decimator(
          "logpwrfft_cuda",
          gr::io_signature::make(1, 1, sizeof(gr_complex)),
          gr::io_signature::make(1, 1, sizeof(float) * fft_size),
          (size_t)compute_decim(sample_rate, fft_size, frame_rate) * fft_size),
      d_fft_size(fft_size),
      d_decim(compute_decim(sample_rate, fft_size, frame_rate)),
      d_engine(device_id)
{
    d_engine.init(fft_size, ref_scale, avg_alpha, average, shift);
}

logpwrfft_cuda_impl::~logpwrfft_cuda_impl() {}

int logpwrfft_cuda_impl::work(int noutput_items,
                              gr_vector_const_void_star& input_items,
                              gr_vector_void_star& output_items)
{
    const gr_complex* in = (const gr_complex*)input_items[0];
    float* out = (float*)output_items[0];
    size_t block_stride = (size_t)d_decim * (size_t)d_fft_size;

    d_engine.execute(reinterpret_cast<const std::complex<float>*>(in), block_stride,
                     (size_t)noutput_items, out);

    return noutput_items;
}

} // namespace sdrexplorer_gpu
} // namespace gr
