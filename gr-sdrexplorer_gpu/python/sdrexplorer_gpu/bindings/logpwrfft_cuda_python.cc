// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#include <pybind11/pybind11.h>
#include <gnuradio/sdrexplorer_gpu/logpwrfft_cuda.h>

namespace py = pybind11;

void bind_logpwrfft_cuda(py::module& m)
{
    using logpwrfft_cuda = gr::sdrexplorer_gpu::logpwrfft_cuda;

    py::class_<logpwrfft_cuda, gr::sync_decimator, gr::basic_block,
               std::shared_ptr<logpwrfft_cuda>>(
        m, "logpwrfft_cuda", "CUDA-accelerated log-power FFT (cuFFT)")
        .def(py::init(&logpwrfft_cuda::make),
             py::arg("sample_rate"),
             py::arg("fft_size"),
             py::arg("ref_scale"),
             py::arg("frame_rate"),
             py::arg("avg_alpha"),
             py::arg("average"),
             py::arg("shift") = true,
             py::arg("device_id") = 0);
}
