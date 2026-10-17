# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Bad Larry
"""The worker side of sdrexplorer's out-of-process workers (module API, ``sdrexplorer.api.worker``).

A module runs heavy or fragile code (an inference runtime, a decoder library with its own numpy) in a process
of its own, often under another venv's Python. That process imports only this file -- standard library, numpy
optional -- which ``api.worker.server_path()`` puts on its PYTHONPATH; nothing else of sdrexplorer is
importable there.

    import sdrexplorer_worker as W

    class Echo:
        def info(self):                       # extra keys for the ready line
            return {"what": "echo"}
        def handle(self, header, payload):    # one request -> (reply header, reply bytes)
            return {"n": len(payload)}, payload
        def close(self):                      # optional
            pass

    if __name__ == "__main__":
        raise SystemExit(W.main(lambda args: Echo()))

The process is started as ``<python> -u -m <module> <socket path> [args...]``. It prints one JSON line on
stdout when ready -- {"ready": true, ...info()} or {"ready": false, "error": ...} -- then serves a Unix stream
socket, one connection and one request at a time. Every message, both ways, little-endian:

    u32 header length, header JSON (with "nbytes": payload length), payload bytes

A request header {"cmd": "quit"} ends it; so does the death of the process that started it (it polls its
parent once a second, and on Linux also asks the kernel for SIGTERM when the parent dies), so an accelerator or
a device is never left held by an orphan. A handler exception is answered with {"error": "..."} and the worker
carries on.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import struct
import sys

PARENT_CHECK_S = 1.0


def recv_exact(conn, n):
    buf = bytearray(n)
    view, got = memoryview(buf), 0
    while got < n:
        k = conn.recv_into(view[got:], n - got)
        if not k:
            raise ConnectionError("peer closed")
        got += k
    return buf


def send(conn, header, payload=b""):
    """One message: ``header`` (a JSON-able dict; "nbytes" is filled in) and ``payload`` (bytes-like)."""
    payload = memoryview(payload).cast("B") if payload is not None else b""   # bytes, bytearray, numpy...
    h = dict(header)
    h["nbytes"] = len(payload)
    hb = json.dumps(h).encode()
    conn.sendall(struct.pack("<I", len(hb)) + hb)
    if len(payload):
        conn.sendall(payload)


def recv(conn):
    """(header, payload bytearray) of the next message (bytearray-backed, so a numpy view is writable)."""
    (hl,) = struct.unpack("<I", recv_exact(conn, 4))
    h = json.loads(bytes(recv_exact(conn, hl)))
    n = int(h.get("nbytes", 0))
    return h, (recv_exact(conn, n) if n else bytearray())


def _die_with_parent():
    """Linux: SIGTERM when the parent goes (PR_SET_PDEATHSIG). The getppid poll covers other systems."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0)   # 1 = PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def _exit_on_signal(*_):
    raise SystemExit(0)


def serve(sock_path, handler, parent=None):
    """Serve ``handler`` on ``sock_path`` until "quit" or the parent's death. Returns the exit status."""
    parent = os.getppid() if parent is None else parent
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock_path)
    srv.listen(1)
    srv.settimeout(PARENT_CHECK_S)
    signal.signal(signal.SIGTERM, _exit_on_signal)        # (runs the finally below)
    info = handler.info() if hasattr(handler, "info") else {}
    print(json.dumps(dict(info, ready=True)), flush=True)
    try:
        while True:
            if os.getppid() != parent:           # the app is gone without saying "quit"
                return 0
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(None)
            with conn:
                try:
                    while True:
                        h, payload = recv(conn)
                        if h.get("cmd") == "quit":
                            send(conn, {"ok": True})
                            return 0
                        try:
                            rh, rp = handler.handle(h, payload)
                            send(conn, rh or {}, rp if rp is not None else b"")
                        except Exception as e:
                            send(conn, {"error": f"{type(e).__name__}: {e}"})
                except ConnectionError:
                    continue                     # client went away; wait for the next
    finally:
        if hasattr(handler, "close"):
            try:
                handler.close()
            except Exception:
                pass
        srv.close()
        try:
            os.unlink(sock_path)
        except OSError:
            pass


def main(load, argv=None):
    """``<worker> <socket path> [args...]``: ``load(args)`` builds the handler; if it raises, the ready line says
    why and the process exits 1."""
    argv = sys.argv[1:] if argv is None else argv
    parent = os.getppid()
    _die_with_parent()
    sock_path, args = argv[0], list(argv[1:])
    try:
        handler = load(args)
    except Exception as e:                       # no runtime / device / bad file: tell the app, exit
        print(json.dumps({"ready": False, "error": f"{type(e).__name__}: {e}"}), flush=True)
        return 1
    return serve(sock_path, handler, parent)
