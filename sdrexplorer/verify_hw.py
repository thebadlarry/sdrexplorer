#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Headless hardware smoke test for the sdrexplorer engine (no GUI).

Confirms the HackRF-facing path the scanner depends on:
  * osmosdr source opens and IQ flows (probe returns a non-flat spectrum)
  * channel_power() reads a plausible signal/noise pair
  * runtime retune works
  * mode switching (lock/unlock) survives on a live graph

Run OUTSIDE the Bash sandbox (device open returns -1000 inside it).
"""
import time

import numpy as np

from sdrexplorer.radio import ScannerRadio, MODES


def main():
    print("constructing ScannerRadio (opens HackRF, headless/no-audio)…")
    radio = ScannerRadio(freq_hz=98_500_000, mode_name="WFM", audio_out=False)
    radio.start()
    try:
        time.sleep(0.6)
        level = np.asarray(radio.probe.level(), dtype=np.float64)
        finite = level[np.isfinite(level)]
        print(f"probe: size={level.size} min={finite.min():.1f} "
              f"max={finite.max():.1f} spread={finite.max()-finite.min():.1f} dB")
        assert level.size == 2048, "probe not producing full vectors"
        assert finite.max() - finite.min() > 3.0, "spectrum looks flat (no IQ?)"

        sig, floor = radio.channel_power(98_500_000, 200_000)
        print(f"channel_power @ 98.5 MHz: sig={sig:.1f} floor={floor:.1f} "
              f"snr={sig-floor:.1f} dB")

        print("switching WFM -> NFM -> AM -> WFM on the live graph…")
        for m in ("NFM", "AM", "WFM"):
            radio.set_mode(m)
            time.sleep(0.2)
            print(f"  mode={m} ok")

        print("retune sweep 154.0 -> 155.0 MHz…")
        for f in np.linspace(154_000_000, 155_000_000, 5):
            radio.retune_only(f)
            time.sleep(0.15)
            lv = np.asarray(radio.probe.level(), dtype=np.float64)
            print(f"  {f/1e6:.3f} MHz  peak={lv.max():.1f} dB")

        print("HW_VERIFY_OK")
    finally:
        radio.stop()
        radio.wait()


if __name__ == "__main__":
    main()
