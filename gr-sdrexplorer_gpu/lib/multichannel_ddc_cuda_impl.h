// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#ifndef INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_CUDA_IMPL_H
#define INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_CUDA_IMPL_H

#include <gnuradio/sdrexplorer_gpu/multichannel_ddc_cuda.h>
#include "multichannel_ddc_kernel.h"

namespace gr {
namespace sdrexplorer_gpu {

class multichannel_ddc_cuda_impl : public multichannel_ddc_cuda
{
private:
    int d_decim;
    float d_sample_rate;
    std::vector<float> d_channel_freqs;
    std::vector<float> d_taps;
    int d_device_id;

    DdcCudaEngine d_engine;

public:
    multichannel_ddc_cuda_impl(int decim_factor,
                               float sample_rate,
                               const std::vector<float>& channel_freqs,
                               const std::vector<float>& taps,
                               int device_id);
    ~multichannel_ddc_cuda_impl();

    void set_channel_freqs(const std::vector<float>& channel_freqs) override;
    void set_taps(const std::vector<float>& taps) override;

    int work(int noutput_items,
             gr_vector_const_void_star& input_items,
             gr_vector_void_star& output_items) override;
};

} // namespace sdrexplorer_gpu
} // namespace gr

#endif /* INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_CUDA_IMPL_H */
