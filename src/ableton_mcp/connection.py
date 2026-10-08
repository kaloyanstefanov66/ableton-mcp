"""Socket client for the ClaudeMCP Remote Script running inside Ableton Live."""
from __future__ import annotations

import itertools
import json
import socket
import threading
from typing import Any

HOST = "127.0.0.1"
PORT = 9890
TIMEOUT = 40.0

NOT_RUNNING_HINT = (
    "Can't reach Ableton. Make sure Live is open and 'ClaudeMCP' is selected as a Control "
    "Surface in Preferences > Link, Tempo & MIDI (restart Live after installing the script)."
)


class AbletonError(RuntimeError):
    """Live reported an error, or could not be reached."""


class AbletonConnection:
    def __init__(self, host: str = HOST, port: int = PORT) -> None:
        self._addr = (host, port)
        self._sock: socket.socket | None = None
        self._buf = b""
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def _connect(self) -> socket.socket:
        try:
            sock = socket.create_connection(self._addr, timeout=5.0)
        except OSError as e:
            raise AbletonError(f"{NOT_RUNNING_HINT} ({e})") from None
        sock.settimeout(TIMEOUT)
        self._buf = b""
        return sock

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None

    def _roundtrip(self, payload: bytes) -> dict:
        if self._sock is None:
            self._sock = self._connect()
        self._sock.sendall(payload)
        while b"\n" not in self._buf:
            chunk = self._sock.recv(1 << 16)
            if not chunk:
                raise ConnectionError("Live closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return json.loads(line)

    def call(self, cmd: str, **args: Any) -> Any:
        args = {k: v for k, v in args.items() if v is not None}
        payload = (json.dumps({"id": next(self._ids), "cmd": cmd, "args": args}) + "\n").encode()
        with self._lock:
            try:
                resp = self._roundtrip(payload)
            except socket.timeout:
                # Don't retry: the command may already have run in Live.
                self._close()
                raise AbletonError(f"Live did not answer '{cmd}' within {TIMEOUT:.0f}s") from None
            except (OSError, ConnectionError):
                # Live may have restarted or reloaded the script: reconnect once.
                self._close()
                try:
                    resp = self._roundtrip(payload)
                except (OSError, ConnectionError) as e:
                    self._close()
                    raise AbletonError(f"{NOT_RUNNING_HINT} ({e})") from None
        if not resp.get("ok"):
            raise AbletonError(resp.get("error", "unknown error from Live"))
        return resp.get("result")
