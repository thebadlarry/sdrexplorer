// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#ifndef INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_CUDA_H
#define INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_CUDA_H

#include <gnuradio/sync_decimator.h>
#include <gnuradio/sdrexplorer_gpu/api.h>
#include <vector>

namespace gr {
namespace sdrexplorer_gpu {

/*!
 * \brief CUDA-Accelerated Multi-Channel Digital Downconverter (DDC)
 * \ingroup sdrexplorer_gpu
 *
 * Takes 1 wideband complex float input stream (e.g. 2.4 - 20 Msps) and
 * heterodyne-shifts, low-pass filters, and decimates N channels concurrently
 * on GPU using CUDA.
 */
class SDREXPLORER_GPU_API multichannel_ddc_cuda : virtual public gr::sync_decimator
{
public:
    typedef std::shared_ptr<multichannel_ddc_cuda> sptr;

    /*!
     * \brief Return a shared_ptr to a new instance of multichannel_ddc_cuda.
     * \param decim_factor Downsampling/decimation factor per channel (e.g. 10 or 50)
     * \param sample_rate Input wideband sample rate (Hz)
     * \param channel_freqs Frequency offsets (Hz) relative to center for each channel
     * \param taps Low-pass FIR filter taps for channel filtering
     * \param device_id CUDA GPU device ID (default 0)
     */
    static sptr make(int decim_factor,
                     float sample_rate,
                     const std::vector<float>& channel_freqs,
                     const std::vector<float>& taps,
                     int device_id = 0);

    virtual void set_channel_freqs(const std::vector<float>& channel_freqs) = 0;
    virtual void set_taps(const std::vector<float>& taps) = 0;
};

} // namespace sdrexplorer_gpu
} // namespace gr

#endif /* INCLUDED_SDREXPLORER_GPU_MULTICHANNEL_DDC_CUDA_H */
