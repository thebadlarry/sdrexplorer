# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""A small local keystore for secret strings a module shouldn't keep in session state.

Modules store things like decryption keys here instead of in state.json / banks: the file sits in the
config directory (paths.config_file), is written atomically and chmod 0600, and is namespaced by
``owner`` so two modules can't collide. Values are opaque strings -- a module that needs structure
(a list, bytes) serializes it itself (e.g. json.dumps / hex) and parses it back.

    api.secrets.set("mymodule", "keys", json.dumps(keys))
    api.secrets.get("mymodule", "keys")       # -> the string, or None
    api.secrets.keys("mymodule")              # -> ["keys", ...]
    api.secrets.delete("mymodule", "keys")

Standard library only, like paths.py. On-disk shape: {"<owner>": {"<name>": "<value>"}}.
"""
from __future__ import annotations

import json
import os

from .. import paths

FILE = "secrets.json"
MODE = 0o600


def _path():
    return paths.config_file(FILE)


def _read():
    """The whole store as a dict ({} if missing or unreadable)."""
    try:
        with open(_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(d):
    """Write the store atomically, mode 0600 (tightened on an existing file too)."""
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    # create the temp file 0600 from the start so the secret is never briefly world-readable
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, MODE)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(d, fh, indent=2)
            fh.write("\n")
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, MODE)
    except OSError:
        pass


def get(owner, name):
    """The stored string for (owner, name), or None."""
    v = _read().get(owner, {}).get(name)
    return v if isinstance(v, str) else None


def set(owner, name, value):  # noqa: A001 - deliberately mirrors a mapping's set
    """Store ``value`` (a string) under (owner, name)."""
    d = _read()
    d.setdefault(owner, {})[name] = str(value)
    _write(d)


def delete(owner, name):
    """Remove (owner, name) if present. No error if it isn't."""
    d = _read()
    bucket = d.get(owner)
    if isinstance(bucket, dict) and name in bucket:
        del bucket[name]
        if not bucket:
            del d[owner]
        _write(d)


def keys(owner):
    """The names stored for ``owner`` (empty list if none)."""
    bucket = _read().get(owner, {})
    return list(bucket) if isinstance(bucket, dict) else []
