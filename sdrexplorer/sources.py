# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The core's source driver: every device gr-osmosdr can open -- its native HackRF / RTL-SDR / bladeRF ...
backends and, through SoapySDR, LimeSDR, Airspy, USRP, PlutoSDR, SDRplay and anything else a SoapySDR
module is installed for. Nothing here is specific to one device: names, gain stages, rates and transmit
come from what the driver library reports (api.sources.Caps).

Devices are listed with osmosdr.device.find() (about a second; safe while another device streams). A
device's caps come from opening it briefly with osmosdr.source(), cached per device (not while it streams:
the running handle answers instead). Whether it can transmit comes from SoapySDR's channel count
(libSoapySDR through ctypes, no Python bindings needed), else from gr-osmosdr's sink types.

Settings are kept per device key ("<family>:<serial>", sdrexplorer.paths memory file sources.json): rate,
gains by stage name, antenna, ppm. The old fixed choice "HackRF" / "RTL-SDR" maps to the first device of
that family.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import sys
import threading

from . import paths as _paths
from .api import sources as api

# gr-osmosdr built by install.sh with sdrexplorer's patch (patches/gr-osmosdr-hackrf-sink-stop.patch: releasing
# PTT on a HackRF could hang), loaded ahead of the system's when it's there. Before any `import osmosdr`.
_paths.activate("gr_osmosdr")

DRIVER = "osmosdr"
SETTINGS_FILE = "sources.json"

# backends whose devices are just helpers / placeholders in osmosdr.device.find(), not radios
_NOT_RADIOS = ("audio", "file", "rtl_tcp", "sdr-iq", "sdr-ip", "netsdr", "cloudiq", "redpitaya")
_LEGACY = {"HackRF": "hackrf", "RTL-SDR": "rtlsdr"}       # the old fixed device names -> family

_lock = threading.Lock()
_caps_cache = {}                 # key -> Caps
CAPS_FILE = "sources_caps.json"  # caps learned from a device, so start-up doesn't open it again

# The device search asks every backend gr-osmosdr has. Their libraries are chatty about it: libusb lists every
# USB device it saw, UHD prints its banner, and SoapySDR's network discovery reports multicast being refused.
# None of it is an error; quiet them unless the user asked for their logs.
_QUIET_ENV = {"SOAPY_SDR_LOG_LEVEL": "FATAL", "LIBUSB_DEBUG": "0", "UHD_LOG_CONSOLE_LEVEL": "fatal"}


def _quiet_libraries():
    for k, v in _QUIET_ENV.items():
        os.environ.setdefault(k, v)


_quiet_libraries()               # they read these once, when loaded: before anything imports gr-osmosdr


# ------------------------------------------------------------------------------------- device args
def _parse(s):
    """'a=1,label=\\'x y\\',b=2' (osmosdr device_t.to_string()) -> dict."""
    out = {}
    for m in re.finditer(r"(\w[\w-]*)=('([^']*)'|[^,]*)", s):
        out[m.group(1)] = m.group(3) if m.group(3) is not None else m.group(2)
    return out


def _family(d):
    if "soapy" in d:
        drv = d.get("driver", "soapy").lower()
        return {"lime": "lime", "lms7": "lime", "limesdr": "lime"}.get(drv, drv)
    for k in ("hackrf", "rtl", "bladerf", "airspy", "airspyhf", "uhd", "miri", "fcd", "xtrx", "freesrp",
              "plutosdr", "soapy"):
        if k in d:
            return "rtlsdr" if k == "rtl" else k
    return next(iter(d), "sdr")


def _serial(d, fam):
    s = d.get("serial") or d.get(fam if fam != "rtlsdr" else "rtl") or ""
    return s.lower().lstrip("0") or s


def _same_radio(a, b):
    """Two listings of one radio (gr-osmosdr's native backend shows a serial's last digits, SoapySDR the
    whole serial): same family and one serial ends with the other."""
    return a.family == b.family and a.serial and b.serial and (a.serial.endswith(b.serial)
                                                               or b.serial.endswith(a.serial))


def _open_args(d):
    """The osmosdr args string that opens exactly this device."""
    keep = {k: v for k, v in d.items() if k not in ("label",)}
    if "soapy" in keep:
        # a Soapy index can change between runs: open by driver + serial instead
        keep.pop("soapy")
        sel = ",".join(f"{k}={v}" for k, v in keep.items() if k in ("driver", "serial", "device_id", "addr",
                                                                  "uri", "label"))
        return f"numchan=1 soapy=0,{sel}" if sel else "numchan=1 soapy=0"
    return "numchan=1 " + ",".join(f"{k}={v}" for k, v in keep.items())


# ------------------------------------------------------------------------------------- SoapySDR TX
_soapy = None


def _soapy_lib():
    global _soapy
    if _soapy is None:
        try:
            for name in ("libSoapySDR.so.0.8", "libSoapySDR.so", "libSoapySDR.dylib", "SoapySDR.dll"):
                try:
                    lib = ctypes.CDLL(name)
                    break
                except OSError:
                    lib = None
            if lib is None:
                raise OSError("libSoapySDR not found")

            class KW(ctypes.Structure):
                _fields_ = [("size", ctypes.c_size_t), ("keys", ctypes.POINTER(ctypes.c_char_p)),
                            ("vals", ctypes.POINTER(ctypes.c_char_p))]
            lib.SoapySDRDevice_make.restype = ctypes.c_void_p
            lib.SoapySDRDevice_make.argtypes = [ctypes.POINTER(KW)]
            lib.SoapySDRDevice_getNumChannels.restype = ctypes.c_size_t
            lib.SoapySDRDevice_getNumChannels.argtypes = [ctypes.c_void_p, ctypes.c_int]
            lib.SoapySDRDevice_getFullDuplex.restype = ctypes.c_bool
            lib.SoapySDRDevice_getFullDuplex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
            lib.SoapySDRDevice_unmake.argtypes = [ctypes.c_void_p]
            lib._KW = KW
            _soapy = lib
        except Exception:
            _soapy = False
    return _soapy or None


def _soapy_tx(args):
    """(tx channels, full duplex) for SoapySDR device args ({"driver": "hackrf", "serial": ...}), or None."""
    lib = _soapy_lib()
    if lib is None:
        return None
    args = {k: v for k, v in args.items() if k in ("driver", "serial", "device_id", "addr", "uri")}
    if not args:
        return None
    keys = (ctypes.c_char_p * len(args))(*[k.encode() for k in args])
    vals = (ctypes.c_char_p * len(args))(*[str(v).encode() for v in args.values()])
    kw = lib._KW(len(args), keys, vals)
    dev = lib.SoapySDRDevice_make(ctypes.byref(kw))
    if not dev:
        return None
    try:
        ntx = int(lib.SoapySDRDevice_getNumChannels(dev, 0))
        dup = bool(lib.SoapySDRDevice_getFullDuplex(dev, 0, 0)) if ntx else False
        return ntx, dup
    finally:
        lib.SoapySDRDevice_unmake(dev)


# ------------------------------------------------------------------------------------- ranges
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?", re.I)


def _range_values(r):
    """A gr-osmosdr meta_range_t as a list of numbers (its .values() isn't bound in Python)."""
    try:
        return sorted({float(x) for x in _NUM.findall(r.to_pp_string())})
    except Exception:
        return []


# ------------------------------------------------------------------------------------- the driver
class OsmosdrDriver(api.SourceDriver):
    name = DRIVER
    title = "gr-osmosdr (native backends + SoapySDR)"

    def probe(self):
        try:
            import osmosdr  # noqa: F401
            return True, ""
        except Exception as e:
            return False, f"gr-osmosdr not importable ({type(e).__name__})"

    def enumerate(self):
        _quiet_libraries()
        import osmosdr
        found = []
        try:
            devs = osmosdr.device.find(osmosdr.device_t("nofake"))
        except Exception as e:
            print(f"[sdrexplorer] device search failed: {e}", file=sys.stderr)
            return []
        for dv in devs:
            d = _parse(dv.to_string())
            if any(k in d for k in _NOT_RADIOS) or d.get("driver") in _NOT_RADIOS:
                continue
            fam = _family(d)
            ser = _serial(d, fam)
            label = d.get("label") or fam
            info = api.DeviceInfo(driver=self.name, key="", label=label, args=_open_args(d),
                                  family=fam, serial=ser)
            info.soapy = "soapy" in d
            info.soapy_args = d if info.soapy else None
            twin = next((f for f in found if _same_radio(f, info)), None)
            if twin is not None:
                # one radio through two backends: open it through the native backend (gr-osmosdr's own,
                # what this app has always used), keep SoapySDR's label, full serial and description
                native, soapy = (info, twin) if twin.soapy else (twin, info)
                twin.args, twin.soapy = native.args, False
                twin.label = soapy.label
                twin.serial = max(native.serial, soapy.serial, key=len)
                twin.soapy_args = soapy.soapy_args
                continue
            found.append(info)
        for f in found:
            f.key = f"{f.family}:{f.serial}" if f.serial else f"{f.family}:0"
        return found

    def caps(self, dev, handle=None):
        with _lock:
            hit = _caps_cache.get(dev.key)
        if hit is None and handle is None:
            hit = _caps_from_disk(dev)
        if hit is not None:
            return hit
        _quiet_libraries()
        import osmosdr
        # SoapySDR's view first: most radios can be opened by one handle at a time, so this has to happen
        # before the native source below is opened (and can't happen while the device streams)
        sa = getattr(dev, "soapy_args", None) or {}
        alt_rates, tx = None, None
        if sa and handle is None:
            tx = _soapy_tx({k: v for k, v in sa.items() if k in ("driver", "serial") and v})
            if not dev.args.startswith("numchan=1 soapy="):
                try:
                    alt = osmosdr.source(args=_open_args(sa))
                    ar = alt.get_sample_rates()
                    alt_rates = (ar.start(), ar.stop()) if ar.start() < ar.stop() else None
                    del alt
                except Exception:
                    pass
        src = handle.block if handle is not None and handle.block is not None else osmosdr.source(args=dev.args)
        try:
            c = api.Caps()
            fr = src.get_freq_range()
            c.freq_range = (fr.start(), fr.stop())
            rr = src.get_sample_rates()
            vals = _range_values(rr)
            c.rate_ranges = [(rr.start(), rr.stop())] if rr.start() < rr.stop() else []
            if vals and len(vals) > 2 and rr.step() == 0:
                c.rates = vals
            # a backend's list can be narrower than the radio: gr-osmosdr's native HackRF lists 8-20 Msps,
            # SoapySDR's 1-20 (and 2.4 Msps works). Take the wider of the two where both describe it.
            if alt_rates:
                c.rate_ranges = [(min([r[0] for r in c.rate_ranges] + [alt_rates[0]]),
                                  max([r[1] for r in c.rate_ranges] + [alt_rates[1]]))]
            c.bandwidths = _range_values(src.get_bandwidth_range())
            for g in src.get_gain_names():
                r = src.get_gain_range(g)
                c.gains.append(api.GainStage(str(g), r.start(), r.stop(), r.step() or 1.0))
            try:
                c.agc = src.set_gain_mode(src.get_gain_mode(0), 0) is not None and dev.family not in ("hackrf",)
            except Exception:
                c.agc = False
            c.antennas = [str(a) for a in src.get_antennas()]
            c.ppm = True
            c.dc_spike = dev.family not in ("lime", "uhd", "airspy", "airspyhf", "sdrplay")
            if tx is None:
                c.tx = False
                c.tx_why = "no transmitter reported for this device"
            else:
                c.tx, c.full_duplex = tx[0] > 0, tx[1]
                c.tx_why = "" if c.tx else "receive-only device"
        finally:
            if handle is None:
                del src
        with _lock:
            _caps_cache[dev.key] = c
        _caps_to_disk(dev, c)
        return c

    def open(self, dev, settings):
        import osmosdr
        src = osmosdr.source(args=dev.args)
        h = OsmosdrHandle(src, dev)
        h.set_rate(settings.get("rate"))
        h.set_center(settings.get("center_hz"))
        try:
            src.set_gain_mode(bool(settings.get("agc", False)), 0)
        except Exception:
            pass
        # Only set stages the device actually reports. gr-osmosdr routes an UNKNOWN gain name (e.g. a
        # stray "LNA" left in a HackRF's saved gains by an old cross-device leak) to the overall gain,
        # which corrupts the amp and kills the signal until the user toggles RF. Filter them out.
        valid = {str(n) for n in src.get_gain_names()}
        for stage, db in (settings.get("gains") or {}).items():
            if stage in valid:
                h.set_gain(stage, db)
        if settings.get("bandwidth"):
            h.set_bandwidth(settings["bandwidth"])
        if settings.get("antenna"):
            h.set_antenna(settings["antenna"])
        if settings.get("ppm"):
            h.set_freq_corr(settings["ppm"])
        return h

    def sink_args(self, dev):
        return dev.args


class OsmosdrHandle(api.SourceHandle):
    def __init__(self, src, dev):
        self.block, self.dev = src, dev

    def set_center(self, hz):
        if hz is not None and self.block is not None:
            self.block.set_center_freq(float(hz), 0)

    def set_rate(self, rate):
        if rate and self.block is not None:
            self.block.set_sample_rate(float(rate))
            try:
                self.block.set_bandwidth(float(rate), 0)   # flat across the window (snaps to the nearest filter)
            except Exception:
                pass

    def set_gain(self, stage, db):
        if self.block is None:
            return
        try:
            self.block.set_gain(float(db), str(stage), 0)
        except Exception:
            pass

    def set_bandwidth(self, hz):
        try:
            self.block.set_bandwidth(float(hz), 0)
        except Exception:
            pass

    def set_antenna(self, name):
        try:
            self.block.set_antenna(str(name), 0)
        except Exception:
            pass

    def set_freq_corr(self, ppm):
        try:
            self.block.set_freq_corr(float(ppm), 0)
        except Exception:
            pass


api.register(OsmosdrDriver())


# ------------------------------------------------------------------------------------- the device list
_devices = []                    # last enumerate() of every driver
_scanned = False


def scan():
    """Ask every driver for its devices (blocking, ~1 s). Remembered devices that aren't attached stay
    listed as not present, so a saved choice survives an unplugged device."""
    global _devices, _scanned
    out = []
    for drv in api.drivers().values():
        ok, _why = drv.probe()
        if not ok:
            continue
        try:
            out += drv.enumerate()
        except Exception as e:
            print(f"[sdrexplorer] {drv.name}: device search failed: {e}", file=sys.stderr)
    keys = {d.key for d in out}
    for key, s in _load().items():
        if key not in keys and s.get("label"):
            out.append(api.DeviceInfo(driver=s.get("driver", DRIVER), key=key, label=s["label"],
                                      args=s.get("args", ""), family=s.get("family", ""),
                                      serial=s.get("serial", ""), present=False))
    _devices, _scanned = out, True
    return list(out)


def devices():
    return list(_devices) if _scanned else scan()


def find(key_or_name):
    """A device by its key, or by an old fixed name ("HackRF" / "RTL-SDR": the first of that family)."""
    devs = devices()
    for d in devs:
        if d.key == key_or_name:
            return d
    fam = _LEGACY.get(key_or_name)
    if fam:
        present = [d for d in devs if d.family == fam and d.present] or [d for d in devs if d.family == fam]
        if present:
            return present[0]
    return None


def caps(dev, handle=None):
    drv = api.get(dev.driver)
    return drv.caps(dev, handle) if drv is not None else api.Caps()


def open_device(dev, settings):
    drv = api.get(dev.driver)
    if drv is None:
        raise RuntimeError(f"no driver {dev.driver!r} for {dev.label}")
    return drv.open(dev, settings)


# ------------------------------------------------------------------------------------- settings
def _settings_path():
    return _paths.memory_file(SETTINGS_FILE)


def _load():
    try:
        with open(_settings_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def settings(dev):
    """This device's saved settings: rate, gains {stage: dB}, antenna, ppm, agc."""
    return dict(_load().get(dev.key) or {})


def save_settings(dev, **values):
    allset = _load()
    s = dict(allset.get(dev.key) or {})
    s.update({k: v for k, v in values.items()})
    s.update(driver=dev.driver, label=dev.label, args=dev.args, family=dev.family, serial=dev.serial)
    allset[dev.key] = s
    path = _settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(allset, fh, indent=1)
    os.replace(tmp, path)


def _caps_path():
    return _paths.memory_file(CAPS_FILE)


def _caps_from_disk(dev):
    """Caps saved the last time this device was opened (same driver and open args), or None."""
    try:
        with open(_caps_path()) as fh:
            d = json.load(fh).get(dev.key)
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(d, dict) or d.get("args") != dev.args or d.get("driver") != dev.driver:
        return None
    try:
        c = api.Caps(**{k: v for k, v in d["caps"].items() if k != "gains"})
        c.gains = [api.GainStage(**g) for g in d["caps"].get("gains", [])]
        c.freq_range = tuple(c.freq_range)
        c.rate_ranges = [tuple(r) for r in c.rate_ranges]
    except (KeyError, TypeError):
        return None
    with _lock:
        _caps_cache[dev.key] = c
    return c


def _caps_to_disk(dev, c):
    from dataclasses import asdict
    path = _caps_path()
    try:
        with open(path) as fh:
            allc = json.load(fh)
    except (OSError, ValueError):
        allc = {}
    allc[dev.key] = {"driver": dev.driver, "args": dev.args, "caps": asdict(c)}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(allc, fh, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def forget_caps():
    """Settings > Find SDR devices: ask every device for its caps again."""
    _caps_cache.clear()
    try:
        os.remove(_caps_path())
    except OSError:
        pass


def reset_cache():
    """Tests."""
    global _devices, _scanned
    _devices, _scanned = [], False
    _caps_cache.clear()
