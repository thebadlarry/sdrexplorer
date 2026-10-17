// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#ifndef INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_CUDA_H
#define INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_CUDA_H

#include <gnuradio/sync_decimator.h>
#include <gnuradio/sdrexplorer_gpu/api.h>

namespace gr {
namespace sdrexplorer_gpu {

/*!
 * \brief CUDA-accelerated log-power FFT probe (cuFFT).
 * \ingroup sdrexplorer_gpu
 *
 * complex float @ sample_rate -> vector<float> (fft_size log-power bins) @
 * ~frame_rate. A drop-in GPU replacement for gr-fft's logpwrfft_c: same
 * math (blackman-harris window, single-pole IIR average over linear power,
 * nlog10 scaling, optional DC-centering shift) computed via cuFFT, so large
 * FFT sizes (16k-64k bins) don't spike CPU the way the stock FFTW block does.
 */
class SDREXPLORER_GPU_API logpwrfft_cuda : virtual public gr::sync_decimator
{
public:
    typedef std::shared_ptr<logpwrfft_cuda> sptr;

    /*!
     * \param sample_rate Incoming stream sample rate (Hz)
     * \param fft_size Number of FFT bins
     * \param ref_scale Sets 0 dB value input amplitude
     * \param frame_rate Output vector rate (Hz); input is decimated ("keep 1
     *        in N" blocks of fft_size samples) to approximate this rate
     * \param avg_alpha Single-pole IIR averaging constant [0.0-1.0]
     * \param average Whether to average (false = alpha forced to 1.0)
     * \param shift fftshift the output so DC sits at the center bin
     * \param device_id CUDA GPU device ID
     */
    static sptr make(float sample_rate,
                     int fft_size,
                     float ref_scale,
                     float frame_rate,
                     float avg_alpha,
                     bool average,
                     bool shift = true,
                     int device_id = 0);
};

} // namespace sdrexplorer_gpu
} // namespace gr

#endif /* INCLUDED_SDREXPLORER_GPU_LOGPWRFFT_CUDA_H */
