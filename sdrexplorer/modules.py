# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Finding, checking and running modules (the core side of sdrexplorer.api.Module).

Discovery, in this order (the first module of a name wins):
  1. module repo checkouts from paths.module_dirs() (sibling sdrexplorer-* repos, paths.json "module_dirs",
     SDREXPLORER_MODULE_DIRS): their "sdrexplorer.modules" / "sdrexplorer.proofs" entry points are read
     from pyproject.toml, so a checkout works without `pip install -e`;
  2. installed packages' "sdrexplorer.modules" entry points;
  3. ``builtin`` classes (core features being moved out keep running from inside the core until then).

Which modules run: all available ones, minus those switched off in Settings > Modules (saved in the
user's paths.json as "modules_off", so it's known before the window and the radio exist), overridden
for one run by --modules a,b / --no-modules.
"""
from __future__ import annotations

import importlib
import os
import re
import sys
import traceback
from dataclasses import dataclass, field

from . import paths
from .api import API_VERSION, proofs
from .api.module import Module

GROUP = "sdrexplorer.modules"
STATE_KEY = "modules"              # state.json section: {module name: its save_state()}


@dataclass
class Found:
    name: str
    cls: type = None
    origin: str = ""               # "checkout <dir>" / "installed <dist>" / "builtin"
    folder: str = ""               # the module's folder (checkouts) or package location (installed)
    source: str = ""               # how that folder was found (paths.SRC_*, "installed", "builtin")
    error: str = ""                # import / API problem: can't run at all
    ok: bool = False               # probe() result
    why: str = ""                  # probe() reason when not ok
    enabled: bool = True           # the user's choice
    instance: Module = None
    title: str = ""
    version: str = ""
    api: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def loaded(self):
        return self.instance is not None

    def status(self):
        if self.loaded:
            return "loaded"
        if self.error:
            return f"error: {self.error}"
        if not self.ok:
            return f"unavailable: {self.why}"
        return "off" if not self.enabled else "not loaded"


# --------------------------------------------------------------------------------- API versions
def parse_version(s):
    return tuple(int(x) for x in re.findall(r"\d+", str(s))[:2]) + (0,) * max(0, 2 - len(re.findall(r"\d+", str(s))))


def api_compatible(spec, version=API_VERSION):
    """Whether ``version`` satisfies a spec like ">=0.1,<0.2" (comparisons: >= > <= < == !=)."""
    v = tuple(version)
    for part in (p.strip() for p in str(spec or "").split(",") if p.strip()):
        m = re.match(r"(>=|<=|==|!=|>|<)\s*([\d.]+)$", part)
        if not m:
            return False
        op, want = m.group(1), parse_version(m.group(2))
        ok = {">=": v >= want, "<=": v <= want, ">": v > want, "<": v < want,
              "==": v == want, "!=": v != want}[op]
        if not ok:
            return False
    return True


# --------------------------------------------------------------------------------- discovery
def _pyproject_entry_points(path):
    """{group: {name: "module:attr"}} from a pyproject.toml's [project.entry-points."group"] tables."""
    try:
        import tomllib
    except ImportError:                     # Python < 3.11
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        return {}
    return dict(data.get("project", {}).get("entry-points", {}))


def _load_ref(ref):
    mod, _, attr = ref.partition(":")
    obj = importlib.import_module(mod.strip())
    for a in attr.strip().split(".") if attr.strip() else []:
        obj = getattr(obj, a)
    return obj


def _checkouts(with_source=False):
    """[(dir, {group: {name: ref}})] for each module checkout (with_source: (dir, eps, source)); its source
    dir goes on sys.path."""
    out = []
    for d, source in paths.module_dir_sources():
        eps = _pyproject_entry_points(os.path.join(d, "pyproject.toml"))
        if not eps.get(GROUP) and not eps.get(proofs.GROUP):
            continue
        src = os.path.join(d, "src") if os.path.isdir(os.path.join(d, "src")) else d
        if src not in sys.path:
            sys.path.insert(0, src)
        out.append((d, eps, source) if with_source else (d, eps))
    return out


def check_folder(folder):
    """(ok, message, names) for a folder someone wants to add as a module: a pyproject.toml declaring
    "sdrexplorer.modules" entry points, each importing to an api.Module subclass whose api range fits
    this core. Imports the module code (so its own errors show up here), but runs nothing of it beyond
    import and probe()."""
    folder = os.path.abspath(os.path.expanduser(folder))
    pp = os.path.join(folder, "pyproject.toml")
    if not os.path.isfile(pp):
        return False, f"no pyproject.toml in {folder}: not a module repo", []
    eps = _pyproject_entry_points(pp)
    mods = eps.get(GROUP, {})
    if not mods:
        if eps.get(proofs.GROUP):
            return True, "provides decoder proofs only (no module UI)", []
        return False, ('pyproject.toml declares no [project.entry-points."sdrexplorer.modules"]: '
                       "not an sdrexplorer module"), []
    src = os.path.join(folder, "src") if os.path.isdir(os.path.join(folder, "src")) else folder
    added = src not in sys.path
    if added:
        sys.path.insert(0, src)
    names, problems = [], []
    try:
        for name, ref in mods.items():
            try:
                cls = _load_ref(ref)
            except Exception as e:
                problems.append(f"{name}: can't import {ref}: {type(e).__name__}: {e}")
                continue
            if not (isinstance(cls, type) and issubclass(cls, Module)):
                problems.append(f"{name}: {ref} is not an sdrexplorer.api.Module subclass")
                continue
            if not api_compatible(cls.api):
                problems.append(f"{name}: needs module API {cls.api}, this sdrexplorer has "
                                f"{'%d.%d' % API_VERSION}")
                continue
            names.append(name)
            try:
                ok, why = cls().probe()
                if not ok:
                    problems.append(f"{name}: loads, but is unavailable here: {why}")
            except Exception as e:
                problems.append(f"{name}: probe failed: {type(e).__name__}: {e}")
    finally:
        if added and not names:
            sys.path.remove(src)
    if not names:
        return False, "; ".join(problems), []
    return True, "; ".join(problems), names


def discover(builtin=()):
    """[Found] for every module that can be found, with probe() run on each importable one."""
    found, names = [], set()

    def add(name, origin, load, folder="", source=""):
        if name in names:
            return
        names.add(name)
        f = Found(name=name, origin=origin, folder=folder, source=source)
        try:
            cls = load()
            if not (isinstance(cls, type) and issubclass(cls, Module)):
                raise TypeError(f"{cls!r} is not an sdrexplorer.api.Module subclass")
            f.cls, f.title, f.version, f.api = cls, cls.title or name, str(cls.version), cls.api
            if not api_compatible(cls.api):
                f.error = f"needs module API {cls.api}, the core has {'%d.%d' % API_VERSION}"
        except Exception as e:
            f.error = f"{type(e).__name__}: {e}"
        found.append(f)

    for d, eps, source in _checkouts(with_source=True):
        for name, ref in eps.get(GROUP, {}).items():
            add(name, f"checkout {d}", lambda ref=ref: _load_ref(ref), folder=d, source=source)
        for name, ref in eps.get(proofs.GROUP, {}).items():
            _register_proofs(name, ref)
    from importlib.metadata import entry_points
    for ep in entry_points(group=GROUP):
        dist = getattr(ep, "dist", None)
        where = ""
        try:
            where = str(dist.locate_file("")) if dist is not None else ""
        except Exception:
            pass
        add(ep.name, f"installed {dist.name if dist is not None else ep.value}", ep.load,
            folder=where, source="installed (pip)")
    for cls in builtin:
        add(cls.name, "builtin", lambda cls=cls: cls, source="built in")

    off = disabled()
    for f in found:
        f.enabled = f.name not in off
        if f.error:
            continue
        try:
            for key, what in dict(getattr(f.cls, "paths", {}) or {}).items():   # the module's own path keys
                paths.register(key, what)
            inst = f.cls()
            f.ok, f.why = inst.probe()
            if f.ok and should_run(f):
                from .api import sources as _src
                for drv in inst.sources() or []:            # its device drivers, before any device search
                    _src.register(drv)
        except Exception as e:
            f.ok, f.why = False, f"probe failed: {type(e).__name__}: {e}"
    return found


def _register_proofs(name, ref):
    try:
        obj = _load_ref(ref)
        table = obj() if callable(obj) else obj
        for label, fn in dict(table).items():
            if label not in proofs._registry:          # (not proofs.get: that would load again)
                proofs.register(label, fn, provider=name)
    except Exception as e:
        print(f"[sdrexplorer] proofs from {name} not loaded: {e}", file=sys.stderr)


# --------------------------------------------------------------------------------- the user's choice
_run_override = None               # --modules / --no-modules: the set to run, for this process only


_options = {}                      # --module-option NAME.KEY=VALUE: {name: {key: value}}, this process only


def set_options(pairs):
    """``pairs``: ["name.key=value", ...] from the command line. Raises ValueError on a malformed one."""
    _options.clear()
    for p in pairs or ():
        k, sep, v = p.partition("=")
        name, dot, key = k.strip().partition(".")
        if not sep or not dot or not name or not key:
            raise ValueError(f"--module-option wants NAME.KEY=VALUE, got {p!r}")
        _options.setdefault(name, {})[key] = v.strip()


def options(name):
    """{key: value} given for module ``name`` on the command line."""
    return dict(_options.get(name, {}))


def set_run_override(names):
    """names: a set of module names to run (empty set: none), or None to use the saved choice."""
    global _run_override
    _run_override = None if names is None else set(names)


def disabled():
    v = paths._read(paths.user_json()).get("modules_off")
    return set(v) if isinstance(v, list) else set()


def set_enabled(name, on):
    off = disabled()
    (off.discard if on else off.add)(name)
    paths.update_user_json({"modules_off": sorted(off)})


def should_run(f):
    if f.error or not f.ok:
        return False
    if _run_override is not None:
        return f.name in _run_override
    return f.enabled


# --------------------------------------------------------------------------------- running
class ModuleHost:
    """The loaded modules of one app. The core calls these at the matching points of its own start-up."""

    def __init__(self, builtin=()):
        self.found = discover(builtin)
        self.loaded = []
        for f in self.found:
            if should_run(f):
                try:
                    f.instance = f.cls()
                    self.loaded.append(f.instance)
                except Exception as e:
                    f.error = f"{type(e).__name__}: {e}"

    def get(self, name):
        return next((m for m in self.loaded if m.name == name), None)

    def _each(self, what, fn):
        for m in list(self.loaded):
            try:
                fn(m)
            except Exception:
                print(f"[sdrexplorer] module {m.name}: {what} failed:", file=sys.stderr)
                traceback.print_exc()

    def attach_radio(self, radio):
        self._each("attach_radio", lambda m: m.attach_radio(radio, radio.taps))

    def attach_ui(self, app, ui):
        self._each("attach_ui", lambda m: m.attach_ui(app, ui))

    def collect(self, what):
        """Every loaded module's modes() / overlays() / menu_entries() / services(), as (module, item)."""
        out = []
        for m in self.loaded:
            try:
                items = getattr(m, what)()
            except Exception:
                print(f"[sdrexplorer] module {m.name}: {what}() failed:", file=sys.stderr)
                traceback.print_exc()
                continue
            if isinstance(items, dict):
                out += [(m, kv) for kv in items.items()]
            else:
                out += [(m, it) for it in items]
        return out

    def load_state(self, state):
        sec = state.get(STATE_KEY) if isinstance(state.get(STATE_KEY), dict) else {}
        self._each("load_state", lambda m: m.load_state(dict(sec.get(m.name) or {})))

    def save_state(self, state):
        """Fill state[STATE_KEY] with each loaded module's section; switched-off modules keep theirs."""
        old = dict(self._saved_sections) if hasattr(self, "_saved_sections") else {}
        sec = {k: v for k, v in old.items() if self.get(k) is None}
        for m in self.loaded:
            try:
                sec[m.name] = m.save_state() or {}
            except Exception:
                print(f"[sdrexplorer] module {m.name}: save_state failed:", file=sys.stderr)
                traceback.print_exc()
        state[STATE_KEY] = sec
        return state

    def remember_sections(self, state):
        """Keep the saved sections of modules that aren't loaded, so saving doesn't drop them."""
        sec = state.get(STATE_KEY)
        self._saved_sections = dict(sec) if isinstance(sec, dict) else {}

    def shutdown(self):
        self._each("shutdown", lambda m: m.shutdown())
