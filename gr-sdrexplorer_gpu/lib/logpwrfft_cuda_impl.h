// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#ifndef INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_CUDA_IMPL_H
#define INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_CUDA_IMPL_H

#include <gnuradio/sdrexplorer_gpu/logpwrfft_cuda.h>
#include "logpwrfft_kernel.h"

namespace gr {
namespace sdrexplorer_gpu {

class logpwrfft_cuda_impl : public logpwrfft_cuda
{
private:
    int d_fft_size;
    int d_decim;      // "keep 1 in N" factor, as in GR's stream_to_vector_decimator
    LogPwrFftCudaEngine d_engine;

public:
    logpwrfft_cuda_impl(float sample_rate,
                        int fft_size,
                        float ref_scale,
                        float frame_rate,
                        float avg_alpha,
                        bool average,
                        bool shift,
                        int device_id);
    ~logpwrfft_cuda_impl();

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items) override;
};

} // namespace sdrexplorer_gpu
} // namespace gr

#endif /* INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_CUDA_IMPL_H */
