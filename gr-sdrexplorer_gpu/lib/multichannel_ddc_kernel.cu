// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#include "multichannel_ddc_kernel.h"
#include <cuda_runtime.h>
#include <device_launch_parameters.h>
#include <iostream>
#include <cmath>

#ifndef M_PI
#define M_PI 3.14159265358979323846f
#endif

namespace gr {
namespace sdrexplorer_gpu {

// Maximum constant memory taps for fast warp broadcast
#define MAX_CONST_TAPS 1024
__constant__ float c_taps[MAX_CONST_TAPS];

// CUDA Kernel: Polyphase Multi-Channel Downconversion (Heterodyne + FIR Filter + Decimate)
// Each thread computes one output sample 'm' for a specific channel 'chan'.
__global__ void ddc_multichannel_kernel(
    const float2* __restrict__ d_in,         // Input wideband complex samples
    float2** __restrict__ d_out_ptrs,        // Output buffer pointers per channel
    const float* __restrict__ d_norm_freqs,  // Normalized freq offsets (freq / sample_rate)
    float* __restrict__ d_phases,            // Phase accumulators per channel
    const float* __restrict__ d_taps_fallback, // Fallback taps if > MAX_CONST_TAPS
    int decim,
    int num_taps,
    size_t noutput_items,
    bool use_const_taps)
{
    size_t out_idx = blockIdx.x * blockDim.x + threadIdx.x;
    int chan = blockIdx.y;

    if (out_idx >= noutput_items) return;

    float norm_f = d_norm_freqs[chan];
    float phase_start = d_phases[chan];
    float2* channel_out = d_out_ptrs[chan];

    // Compute convolution over FIR taps window
    size_t base_in_idx = out_idx * decim;

    float acc_re = 0.0f;
    float acc_im = 0.0f;

    for (int t = 0; t < num_taps; ++t) {
        size_t sample_idx = base_in_idx + t;
        float2 in_sample = d_in[sample_idx];

        // Phase angle for sample: phase_start + 2*pi * norm_f * sample_idx
        float angle = phase_start + 2.0f * M_PI * norm_f * (float)sample_idx;
        float cos_a = __cosf(angle);
        float sin_a = __sinf(angle);

        // Heterodyne shift: in_sample * e^(-j * angle)
        float rot_re = in_sample.x * cos_a + in_sample.y * sin_a;
        float rot_im = in_sample.y * cos_a - in_sample.x * sin_a;

        float tap_val = use_const_taps ? c_taps[num_taps - 1 - t] : d_taps_fallback[num_taps - 1 - t];

        acc_re += rot_re * tap_val;
        acc_im += rot_im * tap_val;
    }

    // Write complex output
    channel_out[out_idx] = make_float2(acc_re, acc_im);
}

// Kernel to advance phase accumulators after each block execution
__global__ void advance_phase_kernel(
    float* __restrict__ d_phases,
    const float* __restrict__ d_norm_freqs,
    size_t noutput_items,
    int decim,
    int num_channels)
{
    int chan = blockIdx.x * blockDim.x + threadIdx.x;
    if (chan >= num_channels) return;

    // Advance phase strictly by the step of consumed input items per block output stride
    float delta = 2.0f * M_PI * d_norm_freqs[chan] * (float)(noutput_items * decim);
    float new_phase = fmodf(d_phases[chan] + delta, 2.0f * M_PI);
    if (new_phase < 0.0f) new_phase += 2.0f * M_PI;
    d_phases[chan] = new_phase;
}

struct DdcCudaEngine::Impl {
    cudaStream_t stream;
    float2* d_in;
    float2* d_out_bufs;       // Contiguous output buffers for all channels on GPU
    float2** d_out_ptrs;      // Device array of pointers to each channel buffer
    float2** h_out_ptrs;      // Host array of pointers to GPU channel buffers
    float* d_norm_freqs;
    float* d_phases;
    float* d_taps;
    size_t in_capacity;
    size_t out_capacity;
    size_t num_channels_alloc;

    Impl() : stream(nullptr), d_in(nullptr), d_out_bufs(nullptr),
             d_out_ptrs(nullptr), h_out_ptrs(nullptr),
             d_norm_freqs(nullptr), d_phases(nullptr), d_taps(nullptr),
             in_capacity(0), out_capacity(0), num_channels_alloc(0) {}
};

DdcCudaEngine::DdcCudaEngine(int device_id)
    : d_device_id(device_id), d_decim(1), d_sample_rate(1.0f),
      d_num_channels(0), d_num_taps(0), d_impl(new Impl()) {}

DdcCudaEngine::~DdcCudaEngine() {
    if (d_impl) {
        cudaSetDevice(d_device_id);
        if (d_impl->stream) cudaStreamDestroy(d_impl->stream);
        if (d_impl->d_in) cudaFree(d_impl->d_in);
        if (d_impl->d_out_bufs) cudaFree(d_impl->d_out_bufs);
        if (d_impl->d_out_ptrs) cudaFree(d_impl->d_out_ptrs);
        if (d_impl->h_out_ptrs) free(d_impl->h_out_ptrs);
        if (d_impl->d_norm_freqs) cudaFree(d_impl->d_norm_freqs);
        if (d_impl->d_phases) cudaFree(d_impl->d_phases);
        if (d_impl->d_taps) cudaFree(d_impl->d_taps);
        delete d_impl;
    }
}

bool DdcCudaEngine::init(int decim_factor,
                         float sample_rate,
                         const std::vector<float>& channel_freqs,
                         const std::vector<float>& taps)
{
    cudaError_t err = cudaSetDevice(d_device_id);
    if (err != cudaSuccess) return false;

    if (d_impl->stream == nullptr) {
        cudaStreamCreate(&d_impl->stream);
    }

    d_decim = decim_factor;
    d_sample_rate = sample_rate;
    d_num_channels = channel_freqs.size();
    d_num_taps = taps.size();

    // Allocate & copy frequency offsets
    update_channels(channel_freqs);

    // Allocate & copy FIR taps
    update_taps(taps);

    // Initialize phases to zero
    if (d_impl->num_channels_alloc < d_num_channels) {
        if (d_impl->d_phases) cudaFree(d_impl->d_phases);
        cudaMalloc(&d_impl->d_phases, d_num_channels * sizeof(float));
        cudaMemsetAsync(d_impl->d_phases, 0, d_num_channels * sizeof(float), d_impl->stream);
        d_impl->num_channels_alloc = d_num_channels;
    }

    return true;
}

bool DdcCudaEngine::update_channels(const std::vector<float>& channel_freqs) {
    cudaSetDevice(d_device_id);
    d_num_channels = channel_freqs.size();
    std::vector<float> norm_freqs(d_num_channels);
    for (size_t i = 0; i < d_num_channels; ++i) {
        norm_freqs[i] = channel_freqs[i] / d_sample_rate;
    }

    if (d_impl->d_norm_freqs) cudaFree(d_impl->d_norm_freqs);
    cudaMalloc(&d_impl->d_norm_freqs, d_num_channels * sizeof(float));
    cudaMemcpyAsync(d_impl->d_norm_freqs, norm_freqs.data(), d_num_channels * sizeof(float),
                    cudaMemcpyHostToDevice, d_impl->stream);
    return true;
}

bool DdcCudaEngine::update_taps(const std::vector<float>& taps) {
    cudaSetDevice(d_device_id);
    d_num_taps = taps.size();
    if (d_num_taps <= MAX_CONST_TAPS) {
        cudaMemcpyToSymbolAsync(c_taps, taps.data(), d_num_taps * sizeof(float), 0,
                                cudaMemcpyHostToDevice, d_impl->stream);
    } else {
        if (d_impl->d_taps) cudaFree(d_impl->d_taps);
        cudaMalloc(&d_impl->d_taps, d_num_taps * sizeof(float));
        cudaMemcpyAsync(d_impl->d_taps, taps.data(), d_num_taps * sizeof(float),
                        cudaMemcpyHostToDevice, d_impl->stream);
    }
    return true;
}

bool DdcCudaEngine::execute(const std::complex<float>* in_samples,
                            size_t total_in_samples,
                            const std::vector<std::complex<float>*>& out_channels,
                            size_t noutput_items)
{
    if (d_num_channels == 0 || noutput_items == 0) return true;

    cudaSetDevice(d_device_id);

    // Reallocate GPU input buffer if capacity exceeded
    if (total_in_samples > d_impl->in_capacity) {
        if (d_impl->d_in) cudaFree(d_impl->d_in);
        d_impl->in_capacity = total_in_samples * 2;
        cudaMalloc(&d_impl->d_in, d_impl->in_capacity * sizeof(float2));
    }

    // Reallocate GPU output channel buffers if capacity exceeded
    if (noutput_items > d_impl->out_capacity || d_num_channels > d_impl->num_channels_alloc) {
        if (d_impl->d_out_bufs) cudaFree(d_impl->d_out_bufs);
        if (d_impl->d_out_ptrs) cudaFree(d_impl->d_out_ptrs);
        if (d_impl->h_out_ptrs) free(d_impl->h_out_ptrs);

        d_impl->out_capacity = noutput_items * 2;
        d_impl->num_channels_alloc = d_num_channels;

        cudaMalloc(&d_impl->d_out_bufs, d_num_channels * d_impl->out_capacity * sizeof(float2));
        cudaMalloc(&d_impl->d_out_ptrs, d_num_channels * sizeof(float2*));
        d_impl->h_out_ptrs = (float2**)malloc(d_num_channels * sizeof(float2*));

        for (size_t c = 0; c < d_num_channels; ++c) {
            d_impl->h_out_ptrs[c] = d_impl->d_out_bufs + c * d_impl->out_capacity;
        }

        cudaMemcpyAsync(d_impl->d_out_ptrs, d_impl->h_out_ptrs, d_num_channels * sizeof(float2*),
                        cudaMemcpyHostToDevice, d_impl->stream);
    }

    // Transfer input stream Host -> GPU
    cudaMemcpyAsync(d_impl->d_in, in_samples, total_in_samples * sizeof(float2),
                    cudaMemcpyHostToDevice, d_impl->stream);

    // Launch CUDA DDC Kernel
    dim3 block(128);
    dim3 grid((noutput_items + block.x - 1) / block.x, d_num_channels);
    bool use_const_taps = (d_num_taps <= MAX_CONST_TAPS);

    ddc_multichannel_kernel<<<grid, block, 0, d_impl->stream>>>(
        d_impl->d_in,
        d_impl->d_out_ptrs,
        d_impl->d_norm_freqs,
        d_impl->d_phases,
        d_impl->d_taps,
        d_decim,
        d_num_taps,
        noutput_items,
        use_const_taps
    );

    // Advance phases correctly using output stride
    int phase_blocks = (d_num_channels + 63) / 64;
    advance_phase_kernel<<<phase_blocks, 64, 0, d_impl->stream>>>(
        d_impl->d_phases,
        d_impl->d_norm_freqs,
        noutput_items,
        d_decim,
        d_num_channels
    );

    // Transfer output channels GPU -> Host
    for (size_t c = 0; c < d_num_channels; ++c) {
        cudaMemcpyAsync(out_channels[c], d_impl->h_out_ptrs[c],
                        noutput_items * sizeof(float2),
                        cudaMemcpyDeviceToHost, d_impl->stream);
    }

    // Synchronize stream
    cudaStreamSynchronize(d_impl->stream);

    return true;
}

} // namespace sdrexplorer_gpu
} // namespace gr
