# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""Out-of-process workers (module API): run a module's heavy or fragile code in its own process.

A module's inference worker pattern, made general: a worker can use another Python
(a venv with its own numpy, a vendor runtime), can't starve the flowgraph of the GIL, and can't take the app
down when a native library faults. The worker side is ``sdrexplorer_worker`` (a standalone file, see its
docstring); this is the app side.

    from sdrexplorer.api import worker

    w = worker.Worker(worker.python_command(py, "my_module.my_worker", "arg"),
                      env=worker.clean_env(MY_SRC))
    w.info                                   # the worker's ready line
    header, payload = w.call({"op": "run"}, x.tobytes())
    w.close()

One request is in flight at a time (``call`` holds a lock, so threads may share a worker). A worker that
fails to start raises ``WorkerError`` with the reason from its ready line.
"""
from __future__ import annotations

import json
import os
import selectors
import socket
import subprocess
import tempfile
import threading
import time

from . import paths

_HERE = os.path.dirname(os.path.abspath(__file__))
SOCKET = "{socket}"                    # placeholder in an argv: replaced by the worker's socket path
_SCRUB = ("PYTHONPATH", "PYTHONHOME", "LD_LIBRARY_PATH", "PYTHONSTARTUP", "PYTHONNOUSERSITE")


class WorkerError(RuntimeError):
    pass


def server_path():
    """The folder holding ``sdrexplorer_worker.py``: put it on a worker's PYTHONPATH (clean_env does)."""
    return os.path.join(_HERE, "worker_server")


def _wire():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sdrexplorer_worker",
                                                  os.path.join(server_path(), "sdrexplorer_worker.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


W = _wire()                            # the framing, shared with the worker side


def clean_env(*pythonpath, extra=None):
    """os.environ without the app's Python / library paths (GNU Radio's, its numpy), with PYTHONPATH =
    ``pythonpath`` + server_path(). ``extra``: more variables."""
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env["PYTHONPATH"] = os.pathsep.join([str(p) for p in pythonpath if p] + [server_path()])
    env.update({k: str(v) for k, v in (extra or {}).items()})
    return env


def python_command(python, module, *args):
    """argv for ``<python> -u -m <module> <socket> args...``."""
    return [python, "-u", "-m", module, SOCKET] + [str(a) for a in args]


def find_python(paths_key, venv_dir):
    """A worker's Python: the paths key's value if it names a file, else ``<venv_dir>/bin/python``, else None."""
    p = paths.get(paths_key) if paths_key else None
    if p and os.path.isfile(p):
        return p
    p = (os.path.join(venv_dir, "Scripts", "python.exe") if os.name == "nt"
         else os.path.join(venv_dir, "bin", "python"))
    return p if os.path.isfile(p) else None


def _with_server_path(env):
    env = dict(os.environ if env is None else env)
    pp = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    if server_path() not in pp:
        env["PYTHONPATH"] = os.pathsep.join(pp + [server_path()])
    return env


class Worker:
    """A worker process and the connection to it."""

    def __init__(self, argv, env=None, cwd=None, ready_timeout=60.0, name="worker", sock_path=None):
        self.name = name
        self.sock_path = sock_path or os.path.join(
            tempfile.gettempdir(), f"sdrexplorer-{name}-{os.getpid()}-{id(self) & 0xffff:x}.sock")
        argv = [self.sock_path if a == SOCKET else a for a in argv]
        self._lock = threading.Lock()
        self.sock = None
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     env=_with_server_path(env), cwd=cwd, text=True)
        info = self._wait_ready(ready_timeout)
        if not info.get("ready"):
            self.close()
            raise WorkerError(info.get("error", f"{name} worker failed to start"))
        info.pop("ready", None)
        self.info = info
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.sock_path)

    def _wait_ready(self, timeout):
        sel = selectors.DefaultSelector()
        sel.register(self.proc.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not sel.select(timeout=max(0.0, deadline - time.monotonic())):
                break
            line = self.proc.stdout.readline()
            if not line:
                return {"ready": False, "error": f"{self.name} worker exited ({self.proc.poll()})"}
            try:
                msg = json.loads(line)
            except ValueError:
                continue                      # a library's chatter on stdout
            if isinstance(msg, dict) and "ready" in msg:
                return msg
        return {"ready": False, "error": f"{self.name} worker did not start in time"}

    def alive(self):
        return self.proc.poll() is None

    def call(self, header, payload=b""):
        """Send one request; (reply header, reply payload bytes). A reply {"error": ...} raises WorkerError."""
        with self._lock:
            if self.sock is None:
                raise WorkerError(f"{self.name} worker is closed")
            try:
                W.send(self.sock, header, payload)
                rh, rp = W.recv(self.sock)
            except (OSError, ConnectionError) as e:
                raise WorkerError(f"{self.name} worker: {e}") from e
        if "error" in rh:
            raise WorkerError(rh["error"])
        return rh, bytes(rp)

    def close(self):
        """Ask it to quit, then make sure it's gone."""
        with self._lock:
            s, self.sock = self.sock, None
            if s is not None:
                try:
                    s.settimeout(2.0)
                    W.send(s, {"cmd": "quit"})
                    W.recv(s)
                except (OSError, ConnectionError, ValueError):
                    pass
                s.close()
        if self.proc.poll() is None:
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(2)
        if self.proc.stdout:
            self.proc.stdout.close()
