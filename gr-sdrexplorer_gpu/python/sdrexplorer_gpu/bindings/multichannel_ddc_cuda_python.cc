// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <gnuradio/sdrexplorer_gpu/multichannel_ddc_cuda.h>

namespace py = pybind11;

void bind_multichannel_ddc_cuda(py::module& m)
{
    using multichannel_ddc_cuda = gr::sdrexplorer_gpu::multichannel_ddc_cuda;

    py::class_<multichannel_ddc_cuda, gr::sync_decimator, gr::basic_block,
               std::shared_ptr<multichannel_ddc_cuda>>(m, "multichannel_ddc_cuda", "CUDA-accelerated Multi-Channel Digital Downconverter")
        .def(py::init(&multichannel_ddc_cuda::make),
             py::arg("decim_factor"),
             py::arg("sample_rate"),
             py::arg("channel_freqs"),
             py::arg("taps"),
             py::arg("device_id") = 0)
        .def("set_channel_freqs", &multichannel_ddc_cuda::set_channel_freqs, py::arg("channel_freqs"))
        .def("set_center_freq", [](multichannel_ddc_cuda& self, float freq) {
            self.set_channel_freqs({freq});
        }, py::arg("freq"))
        .def("set_taps", &multichannel_ddc_cuda::set_taps, py::arg("taps"));
}
