#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Install sdrexplorer and its modules on Debian / Ubuntu (run through install.sh).

    ./install.sh [MODULE ...] [--prefix DIR] [--source DIR] [--without rds,adsb] [--no-venvs]
                 [--update] [--portable] [--no-models] [--dry-run] [--jobs N]

MODULE: paging, digital, classifier, train, all, or none (default: every module marked "default" in
install.json). Layout of an install (what sdrexplorer.paths expects):

    <install>/src/sdrexplorer, src/sdrexplorer-<module>    the repos (modules beside the core are found)
    <install>/ext/<name>                                   external builds (found by default, no config)
    <install>/ext/src, ext/build                           their sources (pinned + patched) and build trees
    <install>/venv-*                                       modules' own Python environments
    <install>/paths.json, bin/sdrexplorer, logs/           settings for what isn't at a default, launcher, logs
    <install>/src/sdrexplorer-models                       the classifier's model release (manifest, cards)

With the classifier, its trained models come from a release of the sdrexplorer-models repo: the model files
are that release's GitHub release files, checked against the sha256 in its manifest, and installed into the
classifier's models folder (paths key models_dir). Models you deployed yourself (sdrexplorer-train) are
never replaced.

Nothing here runs sudo: missing system packages are listed with the one apt-get command that installs them,
and the script stops; run it again afterwards. Re-running is safe: repos already cloned are kept (--update
pulls them), builds already done at the same commit / patches / recipe are skipped.

What gets installed comes from data, not from this file: install.json (the core's packages, its optional
builds, the module repos) and each module's module.json ("apt", "ext", "venvs"). Standard library only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.request

REPO = os.path.dirname(os.path.abspath(__file__))           # this core checkout
CORE = "sdrexplorer"
MIN_PY = (3, 11)


class Fail(Exception):
    """A step failed; the message says what to do."""


# ------------------------------------------------------------------------------------------------ output
def say(msg=""):
    print(msg, flush=True)


def step(msg):
    say(f"\n==> {msg}")


def run(cmd, cwd=None, log=None, env=None, dry=False):
    """Run a command; with ``log``, its output goes to that file and only the tail is shown on failure."""
    shown = " ".join(shlex.quote(str(c)) for c in cmd)
    if dry:
        say(f"    (dry run) {shown}")
        return
    if log is None:
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env)
        if r.returncode:
            raise Fail(f"failed ({r.returncode}): {shown}")
        return
    with open(log, "a") as fh:
        fh.write(f"\n$ {shown}\n")
        fh.flush()
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env, stdout=fh, stderr=subprocess.STDOUT)
    if r.returncode:
        tail = open(log, errors="replace").read().splitlines()[-25:]
        raise Fail(f"failed ({r.returncode}): {shown}\n    log: {log}\n" + "\n".join("    | " + t for t in tail))


def git(*args, cwd=None, dry=False):
    run(["git", *args], cwd=cwd, dry=dry)


# ------------------------------------------------------------------------------------------------ apt
def dpkg_installed(pkgs):
    """The packages from ``pkgs`` that aren't installed (dpkg). None when there is no dpkg."""
    if not shutil.which("dpkg-query"):
        return None
    missing = []
    for p in pkgs:
        r = subprocess.run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}", p], capture_output=True, text=True)
        if not r.stdout.startswith("ii"):
            missing.append(p)
    return missing


def uniq(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ------------------------------------------------------------------------------------------------ repos
def clone(url, dest, ref, update, dry):
    """Clone ``url`` at ``ref`` into ``dest`` (kept if there; --update pulls it)."""
    if os.path.isdir(os.path.join(dest, ".git")):
        if update:
            git("-C", dest, "pull", "-q", "--ff-only", dry=dry)
        return False
    git("clone", "-q", url, dest, dry=dry)
    if ref and not dry:
        git("-C", dest, "checkout", "-q", ref)
    return True


def module_json(path):
    p = os.path.join(path, "module.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def api_ok(spec, version):
    """Whether API ``version`` (major, minor) satisfies ``spec`` (">=1.0,<2.0"), as the core checks it."""
    import re
    for part in (p.strip() for p in str(spec or "").split(",") if p.strip()):
        m = re.match(r"(>=|<=|==|!=|>|<)\s*(\d+)(?:\.(\d+))?$", part)
        if not m:
            return False
        want = (int(m.group(2)), int(m.group(3) or 0))
        ok = {">=": version >= want, "<=": version <= want, ">": version > want, "<": version < want,
              "==": version == want, "!=": version != want}[m.group(1)]
        if not ok:
            return False
    return True


def core_api_version(core):
    import re
    text = open(os.path.join(core, "sdrexplorer", "api", "__init__.py")).read()
    m = re.search(r"^API_VERSION = \((\d+), (\d+)\)", text, re.M)
    return (int(m.group(1)), int(m.group(2))) if m else None


# ------------------------------------------------------------------------------------------------ builds
def ext_dirname(e):
    return (e.get("key") or e["name"]).replace("_", "-")


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]


class Builder:
    def __init__(self, install, jobs, dry):
        self.install, self.jobs, self.dry = install, jobs, dry
        self.ext = os.path.join(install, "ext")
        self.prefixes = []                         # prefixes built so far: CMAKE_PREFIX_PATH for the next
        self.paths = {}                            # paths.json entries (relative to the install)
        os.makedirs(os.path.join(install, "logs"), exist_ok=True)

    def prefix_of(self, e):
        target = e.get("install_into") or e.get("key") or e["name"]
        return os.path.join(self.ext, target.replace("_", "-"))

    def build(self, e, base):
        """Fetch, patch and build one external build. ``base``: the repo its patches are relative to."""
        name = ext_dirname(e)
        spec = e.get("build") or {"system": "none"}
        system = spec.get("system", "none")
        src = os.path.join(self.ext, name) if system == "none" else os.path.join(self.ext, "src", name)
        prefix = src if system == "none" else self.prefix_of(e)
        patches = [os.path.join(base, p) for p in e.get("patches", [])]
        stamp_dir = os.path.join(self.ext, ".stamps")
        stamp_file = os.path.join(stamp_dir, name + ".json")
        stamp = {"commit": e["commit"], "patches": [sha(p) for p in patches if os.path.exists(p)],
                 "build": spec, "prefix": prefix}
        subs = {"src": src, "prefix": prefix, "install": self.install}
        for k, v in (e.get("paths") or {}).items():
            self.paths[k] = os.path.relpath(v.format(**subs), self.install)
        if os.path.exists(stamp_file) and json.load(open(stamp_file)) == stamp:
            say(f"    {e['name']}: already built at {e['commit']} (skipped)")
            if system != "none":
                self.prefixes.append(prefix)
            return
        log = os.path.join(self.install, "logs", name + ".log")
        say(f"    {e['name']} @ {e['commit']}" + (f" + {len(patches)} patch(es)" if patches else "")
            + f"  (log: logs/{name}.log)")
        if self.dry:
            return
        open(log, "w").close()
        if not os.path.isdir(os.path.join(src, ".git")):
            os.makedirs(os.path.dirname(src), exist_ok=True)
            run(["git", "clone", "-q", e["repo"], src], log=log)
        run(["git", "-C", src, "fetch", "-q", "--all"], log=log)
        run(["git", "-C", src, "reset", "-q", "--hard", e["commit"]], log=log)
        run(["git", "-C", src, "clean", "-fdq"], log=log)
        for p in patches:
            if not os.path.exists(p):
                raise Fail(f"{e['name']}: patch {p} is missing")
            run(["git", "-C", src, "apply", "--whitespace=nowarn", p], log=log)
        if system == "cmake":
            bdir = os.path.join(self.ext, "build", name)
            shutil.rmtree(bdir, ignore_errors=True)
            cfg = ["cmake", "-S", os.path.join(src, spec.get("subdir", "")), "-B", bdir,
                   f"-DCMAKE_INSTALL_PREFIX={prefix}"]
            if self.prefixes:
                cfg.append("-DCMAKE_PREFIX_PATH=" + ";".join(self.prefixes))
            cfg += [a.format(**subs) for a in spec.get("args", [])]
            run(cfg, log=log)
            run(["cmake", "--build", bdir, "-j", str(self.jobs)], log=log)
            run(["cmake", "--install", bdir], log=log)
            self.prefixes.append(prefix)
        elif system != "none":
            raise Fail(f"{e['name']}: unknown build system {system!r}")
        os.makedirs(stamp_dir, exist_ok=True)
        json.dump(stamp, open(stamp_file, "w"), indent=1)


def make_venv(install, v, dry):
    """A module's own Python environment: ``python3 -m venv`` + pip. Returns (path key, python) or None."""
    d = os.path.join(install, v.get("dir") or ("venv-" + v["path_key"].replace("_python", "")))
    py = os.path.join(d, "bin", "python")
    stamp = os.path.join(d, ".sdrexplorer-pip.json")
    want = sorted(v.get("pip", []))
    say(f"    {os.path.basename(d)}: {' '.join(want)}" + (f"  ({v['for']})" if v.get("for") else ""))
    if dry:
        return v.get("path_key"), py
    if not (os.path.exists(stamp) and json.load(open(stamp)) == want):
        log = os.path.join(install, "logs", os.path.basename(d) + ".log")
        open(log, "w").close()
        if not os.path.exists(py):
            run([sys.executable, "-m", "venv", d], log=log)
        run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"], log=log)
        run([py, "-m", "pip", "install", "-q", *want], log=log)
        json.dump(want, open(stamp, "w"))
    else:
        say("      (already set up)")
    return v.get("path_key"), py


# ------------------------------------------------------------------------------------------------ models
INSTALLED = "sdrexplorer-models"         # manifest "source" of the models installed from a release


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def models_dir_of(core):
    """Where the classifier reads its models, as the app resolves it (paths key models_dir)."""
    probe = ("import os\nfrom sdrexplorer import paths\n"
             "if 'models_dir' not in paths.KEYS: paths.register('models_dir', 'classifier models')\n"
             "print(paths.get('models_dir') or os.path.join(paths.get('data_dir'), 'models'))")
    r = subprocess.run([sys.executable, "-c", probe], cwd=core, capture_output=True, text=True)
    if r.returncode or not r.stdout.strip():
        raise Fail("can't tell where the classifier's models go:\n" + r.stderr[-1500:])
    return r.stdout.strip().splitlines()[-1]


def fetch_asset(url, local, want, dest):
    """The release file: from ``local`` when it exists (--source), else downloaded from ``url``; checked
    against the manifest's sha256 and size, then moved to ``dest``."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), suffix=".part")
    os.close(fd)
    try:
        if local and os.path.exists(local):
            shutil.copyfile(local, tmp)
        else:
            try:
                with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as fh:
                    shutil.copyfileobj(r, fh, 1 << 20)
            except OSError as e:
                raise Fail(f"download failed: {url} ({e})")
        got = sha256_file(tmp)
        if got != want["sha256"] or os.path.getsize(tmp) != want.get("size", os.path.getsize(tmp)):
            raise Fail(f"{os.path.basename(dest)}: checksum mismatch (got {got[:16]}…, the manifest says "
                       f"{want['sha256'][:16]}…); not installed")
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)               # mkstemp makes it owner-only
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def install_models(release_dir, models_dir, asset_base, local_dist, dry):
    """Install a model release (the sdrexplorer-models checkout ``release_dir``) into ``models_dir``."""
    rel = json.load(open(os.path.join(release_dir, "manifest.json")))
    tag = rel["release"]
    source = f"{INSTALLED} {tag}"
    man_path = os.path.join(models_dir, "manifest.json")
    try:
        cur = json.load(open(man_path)) if os.path.exists(man_path) else {}
    except ValueError:
        cur = {}
    changed = False
    for prof, e in rel["models"].items():
        have = cur.get(prof) if isinstance(cur.get(prof), dict) else None
        if have and not str(have.get("source", "")).startswith(INSTALLED):
            say(f"    {prof}: kept your own model ({have.get('version')}); not replaced")
            continue
        d = os.path.join(models_dir, e["dir"])
        if have and have.get("source") == source and have.get("dir") == e["dir"] and all(
                os.path.exists(os.path.join(d, f)) and os.path.getsize(os.path.join(d, f)) == w.get("size")
                for f, w in e["files"].items()):
            say(f"    {prof}: {e['version']} already installed")
            continue
        say(f"    {prof}: {e['version']}  ({', '.join(w['asset'] for w in e['files'].values())})")
        if dry:
            continue
        os.makedirs(d, exist_ok=True)
        for fname, w in e["files"].items():
            fetch_asset(f"{asset_base}/{w['asset']}", os.path.join(local_dist, w["asset"]) if local_dist else None,
                        w, os.path.join(d, fname))
        for extra in ("meta.json", "eval.json"):
            p = os.path.join(release_dir, "models", prof, e["version"], extra)
            if os.path.exists(p):
                shutil.copyfile(p, os.path.join(d, extra))
        cur[prof] = dict({k: v for k, v in e.items() if k != "files"}, source=source)
        changed = True
    if changed and not dry:
        tmp = man_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(cur, fh, indent=1)
        os.replace(tmp, man_path)
    say(f"    models folder: {models_dir}")


# ------------------------------------------------------------------------------------------------ main
def main(argv=None):
    ap = argparse.ArgumentParser(prog="install.sh", description="Install sdrexplorer and its modules "
                                 "(Debian / Ubuntu). Run it again after installing what it asks for.")
    ap.add_argument("modules", nargs="*", help="paging, digital, classifier, train, all or none "
                                              "(default: the default set in install.json)")
    ap.add_argument("--prefix", help="install folder (default: ~/sdrexplorer, or the folder holding src/ "
                                     "when this checkout is <install>/src/sdrexplorer)")
    ap.add_argument("--source", help="take the repos from this folder of checkouts instead of repo_base "
                                     "(…/sdrexplorer-paging, …)")
    ap.add_argument("--without", default="", help="core extras to leave out: rds, adsb, osmosdr (comma-separated; osmosdr = the patched gr-osmosdr build)")
    ap.add_argument("--no-venvs", action="store_true", help="don't create the modules' Python environments")
    ap.add_argument("--update", action="store_true", help="pull the repos that are already cloned")
    ap.add_argument("--portable", action="store_true", help="keep settings and data under <install>/data")
    ap.add_argument("--no-models", action="store_true", help="don't install the classifier's trained models")
    ap.add_argument("--dry-run", action="store_true", help="show what would be done")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2, help="parallel build jobs")
    ap.add_argument("--skip-apt-check", action="store_true",
                    help="don't check system packages (another distro: install the equivalents yourself)")
    a = ap.parse_args(argv)
    try:
        return install(a)
    except Fail as e:
        say(f"\nerror: {e}")
        return 1


def install(a):
    manifest = json.load(open(os.path.join(REPO, "install.json")))
    known = {m["name"]: m for m in manifest["modules"]}
    picked = a.modules or [m["name"] for m in manifest["modules"] if m.get("default")]
    if picked == ["all"]:
        picked = list(known)
    elif picked == ["none"]:
        picked = []
    bad = [m for m in picked if m not in known]
    if bad:
        raise Fail(f"unknown module(s): {', '.join(bad)} (choose from {', '.join(known)}, all, none)")
    without = {w.strip() for w in a.without.split(",") if w.strip()}
    dry = a.dry_run

    # ---- where
    parent = os.path.dirname(REPO)
    if a.prefix:
        install_dir = os.path.abspath(os.path.expanduser(a.prefix))
    elif os.path.basename(parent) == "src":
        install_dir = os.path.dirname(parent)
    else:
        install_dir = os.path.expanduser("~/sdrexplorer")
    src = os.path.join(install_dir, "src")
    core = os.path.join(src, CORE)
    say(f"sdrexplorer install: {install_dir}")
    say(f"  modules: {', '.join(picked) or '(core only)'}"
        + (f"; without: {', '.join(sorted(without))}" if without else ""))

    # ---- the first packages we need before anything else (git, to fetch the rest)
    if not shutil.which("git") and not a.skip_apt_check:
        raise Fail("git is missing: sudo apt-get install git   (then run this again)")

    # ---- repos
    step("Repositories")
    os.makedirs(src, exist_ok=True)
    if os.path.realpath(REPO) != os.path.realpath(core):
        if clone(REPO, core, None, a.update, dry):
            origin = subprocess.run(["git", "-C", REPO, "remote", "get-url", "origin"], capture_output=True,
                                    text=True).stdout.strip()
            if origin and not dry:
                git("-C", core, "remote", "set-url", "origin", origin)
        say(f"    {CORE}: {core}")
    else:
        if a.update:
            git("-C", core, "pull", "-q", "--ff-only", dry=dry)
        say(f"    {CORE}: this checkout")
    core_for_data = core if os.path.isdir(core) else REPO
    api = core_api_version(core_for_data)
    mods = {}
    for name in picked:
        m = known[name]
        dest = os.path.join(src, m["repo"])
        url = (os.path.join(os.path.abspath(a.source), m["repo"]) if a.source
               else f"{manifest['repo_base'].rstrip('/')}/{m['repo']}.git")
        if "CHANGE-ME" in url and not os.path.isdir(dest):
            raise Fail(f"install.json's repo_base isn't set yet; use --source DIR (a folder holding "
                       f"{m['repo']}) or put the repo in {dest}")
        clone(url, dest, m.get("ref") or manifest.get("ref"), a.update, dry)
        mj = module_json(dest) if os.path.isdir(dest) else {}
        if mj and api and not api_ok(mj.get("api"), api):
            raise Fail(f"{m['repo']} needs module API {mj.get('api')}, this core has {api[0]}.{api[1]}: "
                       f"update one of them (--update)")
        mods[name] = (dest, mj)
        say(f"    {m['repo']}: {dest}")

    # ---- system packages
    core_ext = [e for e in manifest.get("ext", []) if e.get("feature") not in without]
    builds = [(e, core_for_data) for e in core_ext]
    for name, (dest, mj) in mods.items():
        builds += [(e, dest) for e in mj.get("ext", []) if not e.get("skip")]
    need = list(manifest["apt"]["required"])
    if any((e.get("build") or {}).get("system", "none") != "none" for e, _b in builds):
        need += manifest["apt"]["build"]
    for e, _b in builds:
        need += e.get("apt", [])
    for name, (_d, mj) in mods.items():
        need += mj.get("apt", [])
    need = uniq(need)
    step("System packages")
    if a.skip_apt_check:
        say("    not checked (--skip-apt-check). Debian names: " + " ".join(need))
    else:
        missing = dpkg_installed(need)
        if missing is None:
            raise Fail("no dpkg here: this script knows Debian / Ubuntu package names. Install the equivalents "
                       "of these, then run it with --skip-apt-check:\n    " + " ".join(need))
        rec = dpkg_installed(manifest["apt"].get("recommended", [])) or []
        if missing:
            raise Fail("missing system packages. Install them, then run this again:\n\n"
                       f"    sudo apt-get install {' '.join(missing)}\n"
                       + (f"\n  also recommended (live map, SoapySDR tools, RadioReference import):\n"
                          f"    sudo apt-get install {' '.join(rec)}\n" if rec else ""))
        say(f"    all {len(need)} present" + (f"; recommended, not installed: {' '.join(rec)}" if rec else ""))
    if sys.version_info < MIN_PY:
        raise Fail(f"Python {MIN_PY[0]}.{MIN_PY[1]} or later is needed, this is {sys.version.split()[0]}")

    # ---- external builds
    b = Builder(install_dir, a.jobs, dry)
    failed = []                                    # optional builds that failed: the rest goes on
    if builds:
        step("External builds")
        for e, base in builds:
            if any(dep in failed for dep in [e.get("install_into")] if dep):
                say(f"    {e['name']}: skipped ({e['install_into']} failed)")
                failed.append(e.get("key") or e["name"])
                continue
            try:
                b.build(e, base)
            except Fail as err:
                if not (e.get("optional") or e.get("feature")):
                    raise
                say(f"    {e['name']}: FAILED (optional: {e.get('for', 'not needed by the rest')})\n"
                    + "\n".join("      " + l for l in str(err).splitlines()))
                failed.append(e.get("key") or e["name"])

    # ---- venvs
    venv_paths = {}
    venvs = [v for _n, (_d, mj) in mods.items() for v in mj.get("venvs", [])]
    if venvs:
        step("Python environments")
        if a.no_venvs:
            say("    skipped (--no-venvs)")
        else:
            for v in venvs:
                key, py = make_venv(install_dir, v, dry)
                if key:
                    venv_paths[key] = os.path.relpath(py, install_dir)

    # ---- paths.json, launcher
    step("Settings")
    pj = os.path.join(install_dir, "paths.json")
    cur = json.load(open(pj)) if os.path.exists(pj) else {}
    cur["_comment"] = ("Written by install.sh: where things are that aren't at a default place. Your own "
                       "overrides go in ~/.config/sdrexplorer/paths.json. Relative paths are relative to this file.")
    cur.update(b.paths)
    cur.update(venv_paths)
    if a.portable:
        cur["portable"] = True
    launcher = os.path.join(install_dir, "bin", "sdrexplorer")
    if not dry:
        json.dump(cur, open(pj, "w"), indent=2)
        os.makedirs(os.path.dirname(launcher), exist_ok=True)
        with open(launcher, "w") as fh:
            fh.write("#!/bin/sh\n# Written by install.sh\nexec \"$(dirname \"$0\")/../src/sdrexplorer/"
                     "run_sdrexplorer.sh\" \"$@\"\n")
        os.chmod(launcher, 0o755)
    say(f"    {pj}")
    say(f"    {launcher}")

    # ---- the classifier's models
    mrel = manifest.get("models")
    if mrel and "classifier" in mods:
        step("Classifier models")
        if a.no_models:
            say("    skipped (--no-models)")
        else:
            try:
                mdest = os.path.join(src, mrel["repo"])
                if a.source:
                    murl = os.path.join(os.path.abspath(a.source), mrel["repo"])
                    local_dist = os.path.join(murl, "dist")      # a release built here: its files beside it
                else:
                    murl = f"{manifest['repo_base'].rstrip('/')}/{mrel['repo']}.git"
                    local_dist = None
                if "CHANGE-ME" in murl and not os.path.isdir(mdest):
                    raise Fail("install.json's repo_base isn't set yet; use --source DIR")
                clone(murl, mdest, mrel.get("ref") or manifest.get("ref"), a.update, dry)
                if not dry or os.path.isdir(mdest):
                    tag = json.load(open(os.path.join(mdest, "manifest.json")))["release"]
                    base = f"{manifest['repo_base'].rstrip('/')}/{mrel['repo']}/releases/download/{tag}"
                    install_models(mdest, models_dir_of(core), base, local_dist, dry)
            except (Fail, OSError, ValueError, KeyError) as err:
                say("    FAILED: " + str(err).replace("\n", "\n    ")
                    + "\n    The classifier runs without models until they're installed (run this again).")
                failed.append("classifier models")

    # ---- check
    step("Check")
    if dry:
        say("    (dry run: nothing to check)")
        return 0
    keys = sorted({e["key"] for e, _b in builds if e.get("key")} | set(b.paths) | set(venv_paths))
    probe = """
import json, os, sys
from sdrexplorer import features, modules, paths
bad = 0
for f in modules.discover():               # (registers the modules' path keys too)
    st = "ok" if f.ok and not f.error else f.status()
    print(f"    module  {f.name:<14} {st}")
    bad += st != "ok"
for line in features.report().splitlines():
    print("    core    " + line)
for k in json.loads(sys.argv[1]):
    try:
        v, where = paths.lookup(k)
    except KeyError:
        v, where = None, "not registered"
    ok = bool(v) and os.path.exists(v)
    print(f"    path    {k:<14} {'found' if ok else 'NOT FOUND'}  {v or ''}")
    bad += not ok
sys.exit(1 if bad else 0)
"""
    env = dict(os.environ, QT_QPA_PLATFORM=os.environ.get("QT_QPA_PLATFORM", "offscreen"))
    r = subprocess.run([sys.executable, "-c", probe, json.dumps(keys)], cwd=core, env=env,
                       capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.startswith("    ")]
    say("\n".join(lines) if lines else r.stderr[-2000:])
    groups = subprocess.run(["id", "-nG"], capture_output=True, text=True).stdout.split()
    if "plugdev" not in groups:
        say("\n    note: you aren't in the 'plugdev' group, which SDRs usually need: "
            "sudo usermod -aG plugdev $USER (then log out and in)")
    if failed:
        say(f"\nNot built or not installed: {', '.join(failed)}. Everything else is installed and works without them; "
            "the logs in logs/ say why. Fix the cause and run this again to retry just those.")
    say(f"\nDone. Start it with: {launcher}")
    return 1 if (r.returncode or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
