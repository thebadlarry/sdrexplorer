// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#ifndef INCLUDED_SDREXPLORER_GPU_API_H
#define INCLUDED_SDREXPLORER_GPU_API_H

#include <gnuradio/attributes.h>

#ifdef __DLL_EXPORT
#define SDREXPLORER_GPU_API __GR_ATTR_EXPORT
#else
#define SDREXPLORER_GPU_API __GR_ATTR_IMPORT
#endif

#endif /* INCLUDED_SDREXPLORER_GPU_API_H */
