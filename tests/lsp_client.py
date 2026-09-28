"""A tiny LSP test client that speaks raw JSON-RPC over a subprocess pipe.

Frames use the standard ``Content-Length`` header; tests drive the server
entirely through this client, so no IDE or GUI is involved.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import threading


class LSPClient:
    def __init__(self, root_path: str | None = None):
        self.root_path = root_path
        self.process = None
        self._lock = threading.Lock()
        self.notifications: list[dict] = []
        self._id = 0

    # -- process lifecycle ----------------------------------------------------

    def start(self):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.process = subprocess.Popen(
            [sys.executable, "-m", "conf_lsp"],
            cwd=repo_root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return self

    def close(self):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except Exception:
            self.process.kill()
        self.process = None

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- framing --------------------------------------------------------------

    def _send(self, message: dict) -> None:
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        wire = b"Content-Length: %d\r\n\r\n" % len(body) + body
        assert self.process is not None
        self.process.stdin.write(wire)
        self.process.stdin.flush()

    def _read_exact(self, n: int, timeout: float):
        """Read exactly *n* bytes, waiting up to *timeout* for the first."""
        stream = self.process.stdout
        data = b""
        first = True
        while len(data) < n:
            remaining_timeout = timeout if first else max(timeout, 1.0)
            ready, _, _ = select.select([stream], [], [], remaining_timeout)
            if not ready:
                if first:
                    return None
                raise AssertionError("server sent a partial frame")
            chunk = os.read(stream.fileno(), n - len(data))
            if not chunk:
                return None
            data += chunk
            first = False
        return data

    def _read_line(self, timeout: float):
        stream = self.process.stdout
        line = b""
        first = True
        while not line.endswith(b"\n"):
            remaining_timeout = timeout if first else max(timeout, 1.0)
            ready, _, _ = select.select([stream], [], [], remaining_timeout)
            if not ready:
                if first:
                    return None
                raise AssertionError("server sent a partial header")
            ch = os.read(stream.fileno(), 1)
            if not ch:
                return None
            line += ch
            first = False
        return line[:-2] if line.endswith(b"\r\n") else line[:-1]

    def _read_message(self, timeout: float = 5.0):
        headers = {}
        while True:
            line = self._read_line(timeout)
            if line is None:
                return None
            if line == b"":
                break
            name, _, value = line.partition(b":")
            headers[name.strip().lower()] = value.strip()
        if b"content-length" not in headers:
            raise AssertionError("frame without Content-Length")
        body = self._read_exact(int(headers[b"content-length"]), timeout)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))

    # -- requests / notifications ---------------------------------------------

    def request(self, method: str, params=None, timeout: float = 5.0):
        self._id += 1
        msg_id = self._id
        with self._lock:
            self._send({"jsonrpc": "2.0", "id": msg_id,
                        "method": method, "params": params or {}})
            while True:
                message = self._read_message(timeout)
                if message is None:
                    raise AssertionError("server closed the connection")
                if message.get("id") == msg_id:
                    return message
                if "method" in message:
                    self.notifications.append(message)

    def notify(self, method: str, params=None, pump: float = 0.0) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method,
                        "params": params or {}})
        if pump:
            self.pump_notifications(timeout=pump)

    def pump_notifications(self, timeout: float = 0.5) -> list[dict]:
        """Collect any already-pending server notifications."""
        with self._lock:
            stream = self.process.stdout
            while True:
                ready, _, _ = select.select([stream], [], [], timeout)
                if not ready:
                    break
                message = self._read_message(timeout=timeout)
                if message is None:
                    break
                if "method" in message:
                    self.notifications.append(message)
                # Drain only notifications; unexpected responses are ignored.
        return self.drain_notifications()

    def drain_notifications(self) -> list[dict]:
        return [n for n in self.notifications
                if n.get("method") == "textDocument/publishDiagnostics"]

    def diagnostics_for(self, uri: str, wait: float = 0.3) -> list[dict]:
        # Make sure any in-flight notifications have arrived before looking.
        self.pump_notifications(timeout=wait)
        for note in reversed(self.drain_notifications()):
            if note["params"]["uri"] == uri:
                return note["params"]["diagnostics"]
        return []

    # -- high level shortcuts -------------------------------------------------

    def initialize(self) -> dict:
        if self.process is None:
            self.start()
        params = {}
        if self.root_path is not None:
            from conf_lsp.workspace import path_to_uri

            params["rootUri"] = path_to_uri(self.root_path)
        result = self.request("initialize", params)
        if "error" in result:
            raise AssertionError(result["error"])
        self.notify("initialized", {})
        return result["result"]

    def shutdown(self) -> dict:
        return self.request("shutdown", {})

    def send_exit(self) -> None:
        self._send({"jsonrpc": "2.0", "method": "exit"})

    def open(self, uri: str, text: str, language_id: str = "conf",
             pump: float = 0.5) -> None:
        self.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": language_id,
                             "version": 1, "text": text},
        }, pump=pump)

    def change_full(self, uri: str, text: str, version: int = 2,
                    pump: float = 0.5) -> None:
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": text}],
        }, pump=pump)

    def change_range(self, uri: str, start, end, text: str,
                     version: int = 3, pump: float = 0.5) -> None:
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{
                "range": {"start": start, "end": end},
                "text": text,
            }],
        }, pump=pump)

    def close_document(self, uri: str, pump: float = 0.5) -> None:
        self.notify("textDocument/didClose",
                    {"textDocument": {"uri": uri}}, pump=pump)

    def hover(self, uri: str, line: int, character: int):
        return self.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })["result"]

    def definition(self, uri: str, line: int, character: int):
        return self.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })["result"]

    def references(self, uri: str, line: int, character: int):
        return self.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
            "context": {"includeDeclaration": False},
        })["result"]

    def completion(self, uri: str, line: int, character: int):
        return self.request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        })["result"]
