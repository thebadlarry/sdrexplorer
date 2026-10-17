// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 Bad Larry
// Additional permission under GNU GPL version 3 section 7: see LICENSE-EXCEPTION in gr-sdrexplorer_gpu/
#include "logpwrfft_kernel.h"
#include <cuda_runtime.h>
#include <cufft.h>
#include <cmath>
#include <vector>

namespace gr {
namespace sdrexplorer_gpu {

namespace {

constexpr float PI_F = 3.14159265358979323846f;

// gr::fft::window::blackman_harris(N, 92) 4-term coefficients (GNU Radio's
// default atten when no attenuation is specified) - reproduced here so the
// window matches gr-fft's logpwrfft_c bin-for-bin.
std::vector<float> blackman_harris(int n)
{
    const float c0 = 0.35875f, c1 = 0.48829f, c2 = 0.14128f, c3 = 0.01168f;
    std::vector<float> w(n);
    float M = float(n - 1);
    for (int i = 0; i < n; ++i) {
        w[i] = c0 - c1 * cosf(2.0f * PI_F * i / M) + c2 * cosf(4.0f * PI_F * i / M) -
               c3 * cosf(6.0f * PI_F * i / M);
    }
    return w;
}

} // namespace

// Gather the trailing fft_size samples of each block_stride-sample slice and
// apply the window, writing a contiguous batch buffer for cuFFT.
__global__ void logpwrfft_window_kernel(const float2* __restrict__ d_in,
                                        float2* __restrict__ d_windowed,
                                        const float* __restrict__ d_window,
                                        size_t block_stride,
                                        size_t window_offset,
                                        int fft_size,
                                        size_t batch)
{
    size_t idx = blockIdx.x * (size_t)blockDim.x + threadIdx.x;
    if (idx >= batch * (size_t)fft_size) return;
    size_t b = idx / fft_size;
    size_t n = idx % fft_size;
    float2 s = d_in[b * block_stride + window_offset + n];
    float w = d_window[n];
    d_windowed[idx] = make_float2(s.x * w, s.y * w);
}

// One thread per output bin, looping sequentially over the batch's rows: the
// single-pole IIR average is sequential in time but independent per bin, so
// this parallelizes across bins while keeping each bin's running state exact.
__global__ void logpwrfft_avg_log_kernel(const float2* __restrict__ d_fft,
                                         float* __restrict__ d_out,
                                         float* __restrict__ d_avg_state,
                                         int fft_size,
                                         size_t batch,
                                         float alpha,
                                         float one_minus_alpha,
                                         float offset_db,
                                         bool shift)
{
    int bin = blockIdx.x * blockDim.x + threadIdx.x;
    if (bin >= fft_size) return;
    int half = fft_size / 2;
    int src_bin = shift ? (bin + half) % fft_size : bin;
    float y = d_avg_state[bin];
    for (size_t r = 0; r < batch; ++r) {
        float2 c = d_fft[r * (size_t)fft_size + src_bin];
        float magsq = c.x * c.x + c.y * c.y;
        y = alpha * magsq + one_minus_alpha * y;
        float p = fmaxf(y, 1e-30f);
        d_out[r * (size_t)fft_size + bin] = 10.0f * log10f(p) + offset_db;
    }
    d_avg_state[bin] = y;
}

struct LogPwrFftCudaEngine::Impl {
    cudaStream_t stream = nullptr;
    cufftHandle plan = 0;
    bool plan_valid = false;
    size_t plan_batch = 0;

    float2* d_in = nullptr;
    float2* d_windowed = nullptr;
    float2* d_fft = nullptr;
    float* d_out = nullptr;
    float* d_window = nullptr;
    float* d_avg_state = nullptr;

    size_t in_capacity = 0;
    size_t batch_capacity = 0;
};

LogPwrFftCudaEngine::LogPwrFftCudaEngine(int device_id)
    : d_device_id(device_id), d_fft_size(0), d_alpha(1.0f), d_offset_db(0.0f),
      d_shift(true), d_impl(new Impl())
{
}

LogPwrFftCudaEngine::~LogPwrFftCudaEngine()
{
    cudaSetDevice(d_device_id);
    if (d_impl->plan_valid) cufftDestroy(d_impl->plan);
    if (d_impl->stream) cudaStreamDestroy(d_impl->stream);
    if (d_impl->d_in) cudaFree(d_impl->d_in);
    if (d_impl->d_windowed) cudaFree(d_impl->d_windowed);
    if (d_impl->d_fft) cudaFree(d_impl->d_fft);
    if (d_impl->d_out) cudaFree(d_impl->d_out);
    if (d_impl->d_window) cudaFree(d_impl->d_window);
    if (d_impl->d_avg_state) cudaFree(d_impl->d_avg_state);
    delete d_impl;
}

bool LogPwrFftCudaEngine::init(int fft_size,
                               float ref_scale,
                               float avg_alpha,
                               bool average,
                               bool shift)
{
    cudaError_t err = cudaSetDevice(d_device_id);
    if (err != cudaSuccess) return false;
    if (!d_impl->stream) cudaStreamCreate(&d_impl->stream);

    d_fft_size = fft_size;
    d_alpha = average ? avg_alpha : 1.0f;
    d_shift = shift;

    auto window = blackman_harris(fft_size);
    double window_power = 0.0;
    for (float w : window) window_power += double(w) * double(w);
    // matches logpwrfft.py: -20*log10(fft_size) - 10*log10(window_power/fft_size)
    //                        - 20*log10(ref_scale/2)
    d_offset_db = float(-20.0 * log10(double(fft_size)) -
                        10.0 * log10(window_power / fft_size) -
                        20.0 * log10(double(ref_scale) / 2.0));

    if (d_impl->d_window) cudaFree(d_impl->d_window);
    cudaMalloc(&d_impl->d_window, fft_size * sizeof(float));
    cudaMemcpyAsync(d_impl->d_window, window.data(), fft_size * sizeof(float),
                    cudaMemcpyHostToDevice, d_impl->stream);

    if (d_impl->d_avg_state) cudaFree(d_impl->d_avg_state);
    cudaMalloc(&d_impl->d_avg_state, fft_size * sizeof(float));
    cudaMemsetAsync(d_impl->d_avg_state, 0, fft_size * sizeof(float), d_impl->stream);

    if (d_impl->plan_valid) {
        cufftDestroy(d_impl->plan);
        d_impl->plan_valid = false;
        d_impl->plan_batch = 0;
    }

    cudaStreamSynchronize(d_impl->stream);
    return true;
}

bool LogPwrFftCudaEngine::execute(const std::complex<float>* in,
                                  size_t block_stride,
                                  size_t noutput_items,
                                  float* out)
{
    if (noutput_items == 0 || d_fft_size == 0) return true;
    cudaSetDevice(d_device_id);

    size_t total_in = noutput_items * block_stride;
    if (total_in > d_impl->in_capacity) {
        if (d_impl->d_in) cudaFree(d_impl->d_in);
        d_impl->in_capacity = total_in * 2;
        cudaMalloc(&d_impl->d_in, d_impl->in_capacity * sizeof(float2));
    }

    if (noutput_items > d_impl->batch_capacity) {
        if (d_impl->d_windowed) cudaFree(d_impl->d_windowed);
        if (d_impl->d_fft) cudaFree(d_impl->d_fft);
        if (d_impl->d_out) cudaFree(d_impl->d_out);
        d_impl->batch_capacity = noutput_items * 2;
        cudaMalloc(&d_impl->d_windowed,
                  d_impl->batch_capacity * d_fft_size * sizeof(float2));
        cudaMalloc(&d_impl->d_fft, d_impl->batch_capacity * d_fft_size * sizeof(float2));
        cudaMalloc(&d_impl->d_out, d_impl->batch_capacity * d_fft_size * sizeof(float));
    }

    if (!d_impl->plan_valid || d_impl->plan_batch != noutput_items) {
        if (d_impl->plan_valid) cufftDestroy(d_impl->plan);
        cufftPlan1d(&d_impl->plan, d_fft_size, CUFFT_C2C, (int)noutput_items);
        cufftSetStream(d_impl->plan, d_impl->stream);
        d_impl->plan_valid = true;
        d_impl->plan_batch = noutput_items;
    }

    cudaMemcpyAsync(d_impl->d_in, in, total_in * sizeof(float2), cudaMemcpyHostToDevice,
                    d_impl->stream);

    size_t window_offset = block_stride - (size_t)d_fft_size;
    size_t total_threads = noutput_items * (size_t)d_fft_size;
    int block = 256;
    int grid = int((total_threads + block - 1) / block);
    logpwrfft_window_kernel<<<grid, block, 0, d_impl->stream>>>(
        d_impl->d_in, d_impl->d_windowed, d_impl->d_window, block_stride,
        window_offset, d_fft_size, noutput_items);

    cufftExecC2C(d_impl->plan, d_impl->d_windowed, d_impl->d_fft, CUFFT_FORWARD);

    int abin = 256;
    int gbin = (d_fft_size + abin - 1) / abin;
    logpwrfft_avg_log_kernel<<<gbin, abin, 0, d_impl->stream>>>(
        d_impl->d_fft, d_impl->d_out, d_impl->d_avg_state, d_fft_size, noutput_items,
        d_alpha, 1.0f - d_alpha, d_offset_db, d_shift);

    cudaMemcpyAsync(out, d_impl->d_out, noutput_items * d_fft_size * sizeof(float),
                    cudaMemcpyDeviceToHost, d_impl->stream);

    cudaStreamSynchronize(d_impl->stream);
    return true;
}

} // namespace sdrexplorer_gpu
} // namespace gr
