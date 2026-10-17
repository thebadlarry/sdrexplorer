// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#ifndef INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_KERNEL_H
#define INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_KERNEL_H

#include <vector>
#include <complex>

namespace gr {
namespace sdrexplorer_gpu {

class DdcCudaEngine {
public:
    DdcCudaEngine(int device_id = 0);
    ~DdcCudaEngine();

    bool init(int decim_factor,
              float sample_rate,
              const std::vector<float>& channel_freqs,
              const std::vector<float>& taps);

    bool update_channels(const std::vector<float>& channel_freqs);
    bool update_taps(const std::vector<float>& taps);

    // Process a block of samples:
    // input: pointer to host input array of complex float (size: total_in_samples)
    // output: vector of pointers to host output arrays of complex float (size: num_channels, each of size noutput_items)
    bool execute(const std::complex<float>* in_samples,
                 size_t total_in_samples,
                 const std::vector<std::complex<float>*>& out_channels,
                 size_t noutput_items);

private:
    int d_device_id;
    int d_decim;
    float d_sample_rate;
    size_t d_num_channels;
    size_t d_num_taps;

    // CUDA internal state pointers
    struct Impl;
    Impl* d_impl;
};

} // namespace sdrexplorer_gpu
} // namespace gr

#endif /* INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_KERNEL_H */
