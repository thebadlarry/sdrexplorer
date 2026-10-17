// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#include <pybind11/pybind11.h>

namespace py = pybind11;

void bind_multichannel_ddc_cuda(py::module& m);
void bind_logpwrfft_cuda(py::module& m);

PYBIND11_MODULE(sdrexplorer_gpu_python, m)
{
    m.doc() = "GNU Radio sdrexplorer CUDA GPU acceleration bindings";
    bind_multichannel_ddc_cuda(m);
    bind_logpwrfft_cuda(m);
}
