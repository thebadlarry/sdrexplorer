#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The install script's data and plan, without building anything.

    python3 -m sdrexplorer.tests.test_install

Checks install.json and every module.json the script would read (fields, build systems, API ranges), then
runs install.py --dry-run into a temp folder with the module repos checked out beside this core (each found
module repo is used; missing ones are skipped). The real end-to-end test is a full install in a clean
Debian / Ubuntu container (see install.py).
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile

CORE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.dirname(CORE)
sys.path.insert(0, CORE)

import install as I                                             # noqa: E402  (install.py, the repo root)


def _manifest():
    return json.load(open(os.path.join(CORE, "install.json")))


def _check_ext(e, where):
    for k in ("name", "repo", "commit"):
        assert e.get(k), f"{where}: ext {e.get('name')!r} has no {k!r}"
    b = e.get("build") or {"system": "none"}
    assert isinstance(b, dict) and b.get("system") in ("cmake", "none"), f"{where}: {e['name']}: build {b!r}"
    assert isinstance(e.get("apt", []), list), f"{where}: {e['name']}: apt must be a list"
    for k, v in (e.get("paths") or {}).items():
        v.format(src="s", prefix="p", install="i")               # only known placeholders
    assert e.get("key") or e.get("install_into"), f"{where}: {e['name']}: needs a key or install_into"


def test_manifests():
    m = _manifest()
    assert m["format"] == 1 and m["modules"] and m["apt"]["required"]
    api = I.core_api_version(CORE)
    assert api and api >= (1, 0)
    for e in m["ext"]:
        _check_ext(e, "install.json")
        for p in e.get("patches", []):
            assert os.path.exists(os.path.join(CORE, p)), f"install.json: patch {p} missing"
    checked = 0
    for mod in m["modules"]:
        d = os.path.join(SRC, mod["repo"])
        if not os.path.isdir(d):
            continue
        mj = I.module_json(d)
        assert mj, f"{mod['repo']}: no module.json"
        assert I.api_ok(mj["api"], api), f"{mod['repo']}: api {mj['api']} doesn't take core {api}"
        for e in mj.get("ext", []):
            _check_ext(e, mod["repo"])
            for p in e.get("patches", []):
                assert os.path.exists(os.path.join(d, p)), f"{mod['repo']}: patch {p} missing"
        for v in mj.get("venvs", []):
            assert v.get("pip") and v.get("path_key"), f"{mod['repo']}: venv needs pip and path_key"
        checked += 1
    assert I.api_ok(">=1.0,<2.0", (1, 0)) and not I.api_ok(">=1.0,<2.0", (2, 0))
    assert not I.api_ok(">=1.1", (1, 0)) and I.api_ok("", (1, 0))
    print(f"ok  test_manifests ({checked} module repos beside the core)")


def test_dry_run():
    present = [mod["name"] for mod in _manifest()["modules"] if os.path.isdir(os.path.join(SRC, mod["repo"]))]
    with tempfile.TemporaryDirectory(prefix="sdrexplorer-install-") as tmp:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = I.main([*(present or ["none"]), "--prefix", tmp, "--source", SRC, "--dry-run",
                         "--skip-apt-check"])
        text = out.getvalue()
        assert rc == 0, text
        assert "==> Repositories" in text and "==> Settings" in text and "(dry run" in text, text
        assert not os.path.exists(os.path.join(tmp, "ext", ".stamps")), "a dry run built something"
    print(f"ok  test_dry_run ({', '.join(present) or 'core only'})")


def test_unknown_module():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = I.main(["nosuchmodule", "--dry-run", "--skip-apt-check", "--prefix", tempfile.gettempdir()])
    assert rc == 1 and "unknown module" in out.getvalue()
    print("ok  test_unknown_module")


def _release(root, files):
    """A made-up model release: a sdrexplorer-models checkout with dist/ beside it."""
    import hashlib
    rel = os.path.join(root, "sdrexplorer-models")
    os.makedirs(os.path.join(rel, "dist"))
    models = {}
    for prof, ver, data in files:
        asset = f"{prof}-{ver}.onnx"
        open(os.path.join(rel, "dist", asset), "wb").write(data)
        os.makedirs(os.path.join(rel, "models", prof, ver))
        json.dump({"profile": prof, "version": ver}, open(os.path.join(rel, "models", prof, ver, "meta.json"), "w"))
        models[prof] = {"dir": f"{prof}/{ver}", "fs": 24000, "L": 4096, "classes": ["noise", "NBFM"],
                        "version": ver, "display": {}, "bands": {},
                        "files": {"model.onnx": {"asset": asset, "sha256": hashlib.sha256(data).hexdigest(),
                                                 "size": len(data), "backend": "onnx"}}}
    json.dump({"format": 1, "release": "models-9.9.9", "license": "CC-BY-4.0", "models": models},
              open(os.path.join(rel, "manifest.json"), "w"))
    return rel


def test_models_install():
    """The model step: the release files are checked against the manifest's sha256 and installed with their
    meta.json; a profile the user deployed is kept; a re-run skips what's there; a bad file is refused."""
    root = tempfile.mkdtemp(prefix="sdrx-models-")
    rel = _release(root, [("vu", "r1", b"onnx-vu"), ("hf", "h1", b"onnx-hf")])
    md = os.path.join(root, "models")
    os.makedirs(md)
    json.dump({"hf": {"dir": "hf/mine", "version": "mine", "fs": 9600, "L": 4096, "classes": ["noise"]}},
              open(os.path.join(md, "manifest.json"), "w"))                  # the user's own hf model
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        I.install_models(rel, md, "https://example.invalid/none", os.path.join(rel, "dist"), False)
    man = json.load(open(os.path.join(md, "manifest.json")))
    assert man["hf"]["version"] == "mine" and "kept your own" in out.getvalue(), out.getvalue()
    assert man["vu"]["source"] == "sdrexplorer-models models-9.9.9" and "files" not in man["vu"], man["vu"]
    assert open(os.path.join(md, "vu", "r1", "model.onnx"), "rb").read() == b"onnx-vu"
    assert os.path.exists(os.path.join(md, "vu", "r1", "meta.json"))
    assert os.stat(os.path.join(md, "vu", "r1", "model.onnx")).st_mode & 0o044, "not readable by others"
    # without local files the release file is downloaded (a file:// URL here) and checked the same way
    w = json.load(open(os.path.join(rel, "manifest.json")))["models"]["vu"]["files"]["model.onnx"]
    got = os.path.join(root, "dl.onnx")
    I.fetch_asset("file://" + os.path.join(rel, "dist", w["asset"]), None, w, got)
    assert open(got, "rb").read() == b"onnx-vu"
    with contextlib.redirect_stdout(io.StringIO()):
        I.install_models(rel, md, "https://example.invalid/none", os.path.join(rel, "dist"), False)
    # a release file that doesn't match its checksum is refused, and nothing is installed from it
    open(os.path.join(rel, "dist", "vu-r1.onnx"), "wb").write(b"tampered")
    shutil.rmtree(os.path.join(md, "vu"))
    m2 = json.load(open(os.path.join(rel, "manifest.json")))
    m2["release"] = "models-9.9.10"
    json.dump(m2, open(os.path.join(rel, "manifest.json"), "w"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            I.install_models(rel, md, "https://example.invalid/none", os.path.join(rel, "dist"), False)
        raise AssertionError("a tampered file was installed")
    except I.Fail as e:
        assert "checksum mismatch" in str(e), e
    assert not os.path.exists(os.path.join(md, "vu", "r1", "model.onnx"))
    assert json.load(open(os.path.join(md, "manifest.json")))["vu"]["source"].endswith("9.9.9")
    shutil.rmtree(root, ignore_errors=True)
    print("ok  test_models_install")


if __name__ == "__main__":
    test_manifests()
    test_dry_run()
    test_unknown_module()
    test_models_install()
