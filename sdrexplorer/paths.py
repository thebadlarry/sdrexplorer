# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Where sdrexplorer finds things: external builds, interpreters, models, and the user's data.

Nothing here is tied to one machine. Each key is resolved in this order, first match wins:

  1. an environment variable, SDREXPLORER_<KEY> (older names still read, e.g. ADSB_PATH);
  2. the user's paths.json, in the config directory (~/.config/sdrexplorer/paths.json on Linux);
  3. the install's paths.json, written by the install script (see paths.example.json);
  4. a default relative to the install directory (external builds: <install>/ext/<name>);
  5. external builds only: nothing, and the caller uses the system (PATH, the Python path, distro packages).

A relative path in a paths.json is relative to that file's directory, so an install can be moved.

The install directory is SDREXPLORER_INSTALL, else <install> when this checkout is <install>/src/sdrexplorer
and <install>/paths.json exists, else the checkout itself.

User data lives outside the program: ~/.local/share/sdrexplorer (Windows %LOCALAPPDATA%, macOS
~/Library/Application Support), or <install>/data for a portable install (SDREXPLORER_PORTABLE=1,
"portable": true in the install's paths.json, or a file named "portable" in the install directory).

Standard library only: the app, modules' worker venvs and the training tools all import it.

    python3 -m sdrexplorer.paths        every key, its value and where it came from
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import shutil
import sys
import time

APP = "sdrexplorer"
PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(PACKAGE_DIR)
PATHS_JSON = "paths.json"

# external builds: key -> what it is for (shown by `python -m sdrexplorer.paths` and in "not found" messages)
EXT = {
    "gr_rds": "gr-rds install prefix (RDS)",
    "gr_adsb": "gr-adsb python directory (ADS-B)",
    "gr_sdrexplorer_gpu": "gr-sdrexplorer_gpu build directory (CUDA channelizer)",
    "gr_osmosdr": "gr-osmosdr install prefix (patched: HackRF transmit stop; else the system's)",
}
OTHER = {}                  # other core keys (none at present)
DATA = {
    "data_dir": "user data root",
    "memory_dir": "banks, session state, keys, caches, modules' logs",
    "recordings_dir": "IQ recordings",
    "uls_dir": "FCC ULS downloads and search index",
    "store_dir": "real-data store: labeled examples (Label as...) for training",
    "config_dir": "user settings: paths.json, config.json (RadioReference)",
}
KEYS = {**DATA, **EXT, **OTHER}
_PY_EXTRA = {}              # key -> extra python subdirectories (register(..., py_dirs=...))
_DEFAULTS = {}              # key -> () -> its default directory (register(..., default=...))
_PROGRAMS = {"gr_sdrexplorer_gpu"}   # keys that are no Python build (register(..., program=True))


def register(key, description, legacy_env=(), py_dirs=(), default=None, program=False):
    """A module's own external build (``Module.paths``): resolved like the core's keys --
    SDREXPLORER_<KEY>, paths.json, <install>/ext/<key with _ as ->, then the system.

    legacy_env  older environment variable names, still read
    py_dirs     extra Python subdirectories of the build ("." for the directory itself), for activate()
                and tool_env()
    default     () -> a directory or None: the default when nothing sets the key (e.g. derived from another
                key); <install>/ext/<key> is tried after it
    program     the build is a program, not a Python package: tool_env() adds only its libraries"""
    if key in DATA or key in OTHER:
        raise ValueError(f"{key} is a core data key")
    EXT.setdefault(key, description)
    KEYS[key] = EXT[key]
    if legacy_env:
        _LEGACY_ENV.setdefault(key, tuple(legacy_env))
    if py_dirs:
        _PY_EXTRA[key] = tuple(py_dirs)
    if default is not None:
        _DEFAULTS[key] = default
    if program:
        _PROGRAMS.add(key)
_LEGACY_ENV = {"gr_adsb": ("ADSB_PATH",)}
_DATA_SUBDIR = {"memory_dir": "memory", "recordings_dir": "recordings", "uls_dir": "uls",
                "store_dir": "realdata"}

_override: dict = {}        # tests: override(root) points every data key at a temp dir
_json_cache: dict = {}
_activated: set = set()


# ----------------------------------------------------------------------------- resolution
def install_dir():
    env = os.environ.get("SDREXPLORER_INSTALL")
    if env:
        return _abs(env)
    up = os.path.dirname(REPO_DIR)
    if os.path.basename(up) == "src" and os.path.isfile(os.path.join(os.path.dirname(up), PATHS_JSON)):
        return os.path.dirname(up)
    return REPO_DIR


def install_json():
    return os.path.join(install_dir(), PATHS_JSON)


def user_json():
    return os.path.join(get("config_dir"), PATHS_JSON)


def lookup(key):
    """(value, source) for a key; value None when an external build isn't found."""
    if key not in KEYS:
        raise KeyError(f"unknown path key {key!r}")
    if key in _override:
        return _override[key], "override"
    for name in ("SDREXPLORER_" + key.upper(),) + _LEGACY_ENV.get(key, ()):
        v = os.environ.get(name)
        if v:
            return _abs(v), f"env {name}"
    # the user's paths.json sits in the config directory, so the config directory itself can't come from it
    files = (install_json(),) if key == "config_dir" else (user_json(), install_json())
    for f in files:
        v = _from_file(f, key)
        if v is not None:
            return v, f
    return _default(key), "default"


def get(key):
    return lookup(key)[0]


def portable():
    if os.environ.get("SDREXPLORER_PORTABLE", "").strip() not in ("", "0"):
        return True
    return bool(_read(install_json()).get("portable")) or os.path.exists(os.path.join(install_dir(), "portable"))


def _default(key):
    inst = install_dir()
    if key == "data_dir":
        return os.path.join(inst, "data") if portable() else os.path.join(_platform_root("data"), APP)
    if key == "config_dir":
        return os.path.join(inst, "data", "config") if portable() else os.path.join(_platform_root("config"), APP)
    if key in _DATA_SUBDIR:
        return os.path.join(get("data_dir"), _DATA_SUBDIR[key])
    if key in _DEFAULTS:
        try:
            d = _DEFAULTS[key]()
        except Exception:
            d = None
        if d and os.path.isdir(d):
            return d
    if key == "gr_sdrexplorer_gpu":
        return _first_dir([os.path.join(REPO_DIR, "gr-sdrexplorer_gpu", "build"),
                           os.path.join(inst, "ext", "gr-sdrexplorer_gpu")])
    if key in EXT:
        return _first_dir([os.path.join(inst, "ext", key.replace("_", "-"))])
    return None


def _platform_root(kind):
    home = os.path.expanduser("~")
    if sys.platform == "win32":
        var = "LOCALAPPDATA" if kind == "data" else "APPDATA"
        return os.environ.get(var) or os.path.join(home, "AppData", "Local" if kind == "data" else "Roaming")
    if sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support")
    var, sub = ("XDG_DATA_HOME", ".local/share") if kind == "data" else ("XDG_CONFIG_HOME", ".config")
    return os.environ.get(var) or os.path.join(home, sub)


def _abs(p):
    return os.path.abspath(os.path.expanduser(p))


def _first_dir(cands):
    return next((c for c in cands if os.path.isdir(c)), None)


def _read(path):
    """A paths.json as a dict ({} if missing or unreadable); re-read when the file changes."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _json_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path) as fh:
            d = json.load(fh)
        d = d if isinstance(d, dict) else {}
    except (OSError, ValueError) as e:
        print(f"[sdrexplorer] ignoring {path}: {e}", file=sys.stderr)
        d = {}
    _json_cache[path] = (mtime, d)
    return d


def _from_file(path, key):
    v = _read(path).get(key)
    if not isinstance(v, str) or not v:
        return None
    v = os.path.expanduser(v)
    return v if os.path.isabs(v) else os.path.normpath(os.path.join(os.path.dirname(path), v))


# ----------------------------------------------------------------------------- data files
def memory_file(name):
    return os.path.join(get("memory_dir"), name)


def recordings_dir():
    return get("recordings_dir")


def uls_dir():
    return get("uls_dir")


def config_file(name):
    return os.path.join(get("config_dir"), name)


# where a module folder was found (module_dir_sources); the labels Settings > Modules shows
SRC_ENV = "SDREXPLORER_MODULE_DIRS"
SRC_ADDED = "added (Settings > Modules)"
SRC_INSTALL = "install paths.json"
SRC_BESIDE = "beside sdrexplorer"


def module_dir_sources():
    """[(folder, source)] of module repo checkouts to load modules from without installing them, in the
    order they're searched (the first module of a name wins):
      1. SDREXPLORER_MODULE_DIRS (os.pathsep-separated);
      2. "module_dirs" in the user's paths.json -- what Settings > Modules > Add module... writes;
      3. "module_dirs" in the install's paths.json;
      4. every sdrexplorer-* folder with a pyproject.toml beside this checkout (<install>/src/ for an install).
    SDREXPLORER_MODULE_DIRS_ONLY (or a test override) replaces the whole list; SDREXPLORER_SKIP_BESIDE
    leaves out 4."""
    only = os.environ.get("SDREXPLORER_MODULE_DIRS_ONLY")
    if "module_dirs" in _override:
        out = [(p, "override") for p in _override["module_dirs"]]
    elif only is not None:
        out = [(_abs(p), "override") for p in only.split(os.pathsep) if p]
    else:
        out = []
        env = os.environ.get("SDREXPLORER_MODULE_DIRS")
        if env:
            out += [(_abs(p), SRC_ENV) for p in env.split(os.pathsep) if p]
        for f, src in ((user_json(), SRC_ADDED), (install_json(), SRC_INSTALL)):
            out += [(p, src) for p in _json_dirs(f)]
        up = os.path.dirname(REPO_DIR)
        try:
            sib = ([] if os.environ.get("SDREXPLORER_SKIP_BESIDE")
                   else sorted(n for n in os.listdir(up) if n.startswith(APP + "-")))
        except OSError:
            sib = []
        out += [(os.path.join(up, n), SRC_BESIDE) for n in sib
                if os.path.isfile(os.path.join(up, n, "pyproject.toml"))]
    seen, res = set(), []
    for p, src in out:
        r = os.path.realpath(p)
        if r not in seen and os.path.isdir(p):
            seen.add(r)
            res.append((p, src))
    return res


def module_dirs():
    return [p for p, _src in module_dir_sources()]


def _json_dirs(f):
    v = _read(f).get("module_dirs")
    out = []
    for p in (v if isinstance(v, list) else []):
        if isinstance(p, str) and p:
            p = os.path.expanduser(p)
            out.append(p if os.path.isabs(p) else os.path.normpath(os.path.join(os.path.dirname(f), p)))
    return out


def added_module_dirs():
    """The folders the user added (the user paths.json's "module_dirs"), whether or not they still exist."""
    return _json_dirs(user_json())


def add_module_dir(folder):
    """Add a module folder to the user's paths.json (absolute). False if it was already there."""
    folder = _abs(folder)
    have = [os.path.realpath(p) for p in added_module_dirs()]
    if os.path.realpath(folder) in have:
        return False
    v = _read(user_json()).get("module_dirs")
    update_user_json({"module_dirs": (list(v) if isinstance(v, list) else []) + [folder]})
    return True


def remove_module_dir(folder):
    """Take a folder out of the user's paths.json (nothing on disk is touched). False if it wasn't there."""
    target = os.path.realpath(_abs(folder))
    v = _read(user_json()).get("module_dirs")
    v = list(v) if isinstance(v, list) else []
    keep = [p for p in v if not (isinstance(p, str) and os.path.realpath(_abs(p)) == target)]
    if len(keep) == len(v):
        return False
    update_user_json({"module_dirs": keep})
    return True


def override(root=None, **keys):
    """Tests: point every data key under ``root`` (and/or set single keys). override() with no
    arguments clears all overrides."""
    if root is None and not keys:
        _override.clear()
        return
    if root is not None:
        root = _abs(root)
        _override.update(data_dir=root, config_dir=os.path.join(root, "config"),
                         **{k: os.path.join(root, sub) for k, sub in _DATA_SUBDIR.items()})
    for k, v in keys.items():
        if k not in KEYS and k != "module_dirs":
            raise KeyError(f"unknown path key {k!r}")
        _override[k] = v


def update_user_json(updates):
    """Merge keys into the user's paths.json (written atomically)."""
    path = user_json()
    d = dict(_read(path))
    d.update(updates)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(d, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------- external builds
def ext(key):
    """An external build's directory if it exists, else None."""
    p = get(key)
    return p if p and os.path.isdir(p) else None


def program(key, name):
    """Path of a program from an external build (<prefix>/bin/<name>), else from PATH, else None."""
    p = ext(key)
    if p:
        for cand in (os.path.join(p, "bin", name), os.path.join(p, name)):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
    return shutil.which(name)


def _lib_dirs(prefix):
    return [d for d in [os.path.join(prefix, "lib")] + sorted(glob.glob(os.path.join(prefix, "lib", "*-linux-gnu")))
            if os.path.isdir(d)]


def _py_dirs(key, prefix):
    if key == "gr_sdrexplorer_gpu":
        py = os.path.join(prefix, "python", "sdrexplorer_gpu")
        return [d for d in (os.path.join(py, "bindings"), py) if os.path.isdir(d)]
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    out = [d for sub in ("site-packages", "dist-packages")
           for d in [os.path.join(prefix, "lib", ver, sub)] if os.path.isdir(d)]
    out += [d for sub in _PY_EXTRA.get(key, ()) for d in [os.path.normpath(os.path.join(prefix, sub))]
            if os.path.isdir(d) and d not in out]
    if not out:                    # a plain python directory (gr-adsb's python/)
        out = [prefix]
    return out


def activate(key):
    """Make an external GNU Radio build importable in this process: preload its shared libraries
    (the dynamic loader doesn't see LD_LIBRARY_PATH changes made after start-up), and put its Python
    directories on sys.path and in the gnuradio namespace package. Returns its directory, or None if
    it isn't there (then whatever the system provides is imported instead)."""
    prefix = ext(key)
    if prefix is None or key in _activated:
        return prefix
    _activated.add(key)
    libs = []
    for d in _lib_dirs(prefix):
        for pat in ("libgnuradio-*.so", "libgnuradio-*.dylib"):
            libs += glob.glob(os.path.join(d, pat))
    for so in sorted(libs):
        try:
            ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
        except OSError as e:
            print(f"[sdrexplorer] {key}: can't load {so}: {e}", file=sys.stderr)
    gr = sys.modules.get("gnuradio")
    for d in reversed(_py_dirs(key, prefix)):
        if d not in sys.path:
            sys.path.insert(0, d)
        ns = os.path.join(d, "gnuradio")
        if gr is not None and os.path.isdir(ns) and ns not in list(gr.__path__):
            gr.__path__.append(ns)
    return prefix


def tool_env(*keys, env=None):
    """Environment for a subprocess from external builds: their library directories on
    LD_LIBRARY_PATH and, for Python builds, their Python directories on PYTHONPATH."""
    env = dict(os.environ if env is None else env)
    libs, pys = [], []
    for key in keys:
        prefix = ext(key)
        if prefix is None:
            continue
        libs += _lib_dirs(prefix)
        if key not in _PROGRAMS:                            # python builds only
            pys += _py_dirs(key, prefix)
    for var, add in (("LD_LIBRARY_PATH", libs), ("PYTHONPATH", pys)):
        if add:
            old = env.get(var)
            env[var] = os.pathsep.join(add + ([old] if old else []))
    return env


# ----------------------------------------------------------------------------- moving old data
def migrate_legacy(log=None):
    """Once: take over user data from the package directory, where versions before the module split
    kept it. memory/ and config.json are copied (the originals stay, with a note); recordings/ and
    uls/ are large, so the user's paths.json points at them where they are. Returns what was done."""
    log = log or (lambda m: print(f"[sdrexplorer] {m}", flush=True))
    if _override:
        return []
    done, updates = [], {}
    old_mem, mem = os.path.join(PACKAGE_DIR, "memory"), get("memory_dir")
    if (os.path.isdir(old_mem) and _has_files(old_mem) and not _has_files(mem)
            and os.path.realpath(old_mem) != os.path.realpath(mem)):
        shutil.copytree(old_mem, mem, dirs_exist_ok=True)
        try:
            with open(os.path.join(old_mem, "MOVED.txt"), "w") as fh:
                fh.write(f"Copied to {mem} on {time.strftime('%Y-%m-%d %H:%M')}.\n"
                         "sdrexplorer now uses that copy; this one is kept only as a backup.\n")
        except OSError:
            pass
        done.append(f"copied {old_mem} -> {mem}")
    for key, sub in (("recordings_dir", "recordings"), ("uls_dir", "uls")):
        old, (cur, src) = os.path.join(PACKAGE_DIR, sub), lookup(key)
        if src == "default" and os.path.isdir(old) and _has_files(old) and not _has_files(cur):
            updates[key] = old
            done.append(f"{key}: keeps using {old}")
    old_cfg, cfg = os.path.join(PACKAGE_DIR, "config.json"), config_file("config.json")
    if os.path.isfile(old_cfg) and not os.path.exists(cfg):
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        shutil.copy2(old_cfg, cfg)
        done.append(f"copied {old_cfg} -> {cfg}")
    if updates:
        update_user_json(updates)
    for m in done:
        log(m)
    return done


def _has_files(d):
    try:
        return any(not n.startswith(".") for n in os.listdir(d))
    except OSError:
        return False


# ----------------------------------------------------------------------------- report
def report():
    rows = [("install", install_dir(), "SDREXPLORER_INSTALL" if os.environ.get("SDREXPLORER_INSTALL")
             else "detected"), ("portable", str(portable()), "")]
    for key in list(DATA) + list(EXT) + list(OTHER):
        v, src = lookup(key)
        if key in EXT and v and not os.path.isdir(v):
            v = f"{v}  (missing)"
        rows.append((key, v or "-  (not found: system fallback)" if key in EXT else v or "-", src))
    for d, src in module_dir_sources() or [("-", "")]:
        rows.append(("module folder", d, src))
    w = max(len(r[0]) for r in rows)
    return "\n".join(f"{k:<{w}}  {v}  [{s}]" if s else f"{k:<{w}}  {v}" for k, v, s in rows)


if __name__ == "__main__":
    print(report())
