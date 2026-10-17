// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#include "multichannel_ddc_cuda_impl.h"
#include <gnuradio/io_signature.h>

namespace gr {
namespace sdrexplorer_gpu {

multichannel_ddc_cuda::sptr
multichannel_ddc_cuda::make(int decim_factor,
                            float sample_rate,
                            const std::vector<float>& channel_freqs,
                            const std::vector<float>& taps,
                            int device_id)
{
    return gnuradio::make_block_sptr<multichannel_ddc_cuda_impl>(
        decim_factor, sample_rate, channel_freqs, taps, device_id);
}

multichannel_ddc_cuda_impl::multichannel_ddc_cuda_impl(
    int decim_factor,
    float sample_rate,
    const std::vector<float>& channel_freqs,
    const std::vector<float>& taps,
    int device_id)
    : gr::sync_decimator(
          "multichannel_ddc_cuda",
          gr::io_signature::make(1, 1, sizeof(gr_complex)),
          gr::io_signature::make(channel_freqs.size(), channel_freqs.size(), sizeof(gr_complex)),
          decim_factor),
      d_decim(decim_factor),
      d_sample_rate(sample_rate),
      d_channel_freqs(channel_freqs),
      d_taps(taps),
      d_device_id(device_id),
      d_engine(device_id)
{
    set_history(taps.size());
    d_engine.init(decim_factor, sample_rate, channel_freqs, taps);
}

multichannel_ddc_cuda_impl::~multichannel_ddc_cuda_impl() {}

void multichannel_ddc_cuda_impl::set_channel_freqs(const std::vector<float>& channel_freqs)
{
    gr::thread::scoped_lock lock(d_setlock);
    d_channel_freqs = channel_freqs;
    set_output_signature(gr::io_signature::make(
        channel_freqs.size(), channel_freqs.size(), sizeof(gr_complex)));
    d_engine.update_channels(channel_freqs);
}

void multichannel_ddc_cuda_impl::set_taps(const std::vector<float>& taps)
{
    gr::thread::scoped_lock lock(d_setlock);
    d_taps = taps;
    set_history(taps.size());
    d_engine.update_taps(taps);
}

int multichannel_ddc_cuda_impl::work(int noutput_items,
                                     gr_vector_const_void_star& input_items,
                                     gr_vector_void_star& output_items)
{
    const gr_complex* in = (const gr_complex*)input_items[0];
    size_t total_in_samples = noutput_items * d_decim + history() - 1;

    std::vector<gr_complex*> out_channels(output_items.size());
    for (size_t i = 0; i < output_items.size(); ++i) {
        out_channels[i] = (gr_complex*)output_items[i];
    }

    d_engine.execute(
        reinterpret_cast<const std::complex<float>*>(in),
        total_in_samples,
        *reinterpret_cast<const std::vector<std::complex<float>*>*>(&out_channels),
        noutput_items);

    return noutput_items;
}

} // namespace sdrexplorer_gpu
} // namespace gr
