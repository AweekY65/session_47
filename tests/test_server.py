"""End-to-end tests: spawn the server as a subprocess and speak JSON-RPC
(LSP base protocol) to it over stdin/stdout pipes. No IDE, no GUI, no
network — everything is local.
"""

import json
import os
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def path_to_uri(path: str) -> str:
    return "file://" + os.path.abspath(path)


class Client:
    """Minimal LSP test client talking to `python3 -m scls`."""

    def __init__(self, root: str):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "scls"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            cwd=str(ROOT),
        )
        self._next_id = 0
        self._recv = bytearray()  # raw bytes read ahead of message framing
        self._buffered = []  # notifications received while waiting
        result = self.request(
            "initialize",
            {
                "processId": None,
                "rootUri": path_to_uri(root),
                "capabilities": {},
            },
        )
        assert "capabilities" in result, result
        self.capabilities = result["capabilities"]
        self.notify("initialized", {})

    # -- transport -----------------------------------------------------
    def _write(self, message):
        data = json.dumps(message).encode("utf-8")
        self.proc.stdin.write(
            b"Content-Length: %d\r\n\r\n" % len(data) + data
        )
        self.proc.stdin.flush()

    def _fill(self, timeout):
        fd = self.proc.stdout.fileno()
        if not select.select([fd], [], [], timeout)[0]:
            raise TimeoutError("no message from server within timeout")
        chunk = os.read(fd, 65536)
        if not chunk:
            raise EOFError("server closed stdout")
        self._recv += chunk

    def _read_message(self, timeout=10.0):
        # Read raw bytes into our own buffer and frame messages there;
        # mixing select() with a BufferedReader loses bytes that were
        # read ahead into the userspace buffer.
        deadline = time.monotonic() + timeout
        while True:
            split = self._recv.find(b"\r\n\r\n")
            if split != -1:
                headers = {}
                for line in bytes(self._recv[:split]).split(b"\r\n"):
                    key, _, value = line.decode("ascii").partition(":")
                    headers[key.strip().lower()] = value.strip()
                total = split + 4 + int(headers["content-length"])
                if len(self._recv) >= total:
                    body = bytes(self._recv[split + 4 : total])
                    del self._recv[:total]
                    return json.loads(body.decode("utf-8"))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no message from server within timeout")
            self._fill(remaining)

    # -- protocol helpers ----------------------------------------------
    def request(self, method, params=None):
        self._next_id += 1
        rid = self._next_id
        self._write(
            {
                "jsonrpc": "2.0",
                "id": rid,
                "method": method,
                "params": params or {},
            }
        )
        while True:
            message = self._read_message()
            if message.get("id") == rid:
                if "error" in message:
                    raise AssertionError(
                        f"{method} failed: {message['error']}"
                    )
                return message.get("result")
            self._buffered.append(message)

    def notify(self, method, params=None):
        self._write(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    def next_diagnostics(self, uri, timeout=10.0):
        """Pop the next publishDiagnostics notification for *uri*."""
        for i, message in enumerate(self._buffered):
            if (
                message.get("method") == "textDocument/publishDiagnostics"
                and message["params"]["uri"] == uri
            ):
                self._buffered.pop(i)
                return message["params"]["diagnostics"]
        while True:
            message = self._read_message(timeout)
            if (
                message.get("method") == "textDocument/publishDiagnostics"
                and message["params"]["uri"] == uri
            ):
                return message["params"]["diagnostics"]
            self._buffered.append(message)

    def open_document(self, uri, text, version=1):
        self.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": "scl",
                    "version": version,
                    "text": text,
                }
            },
        )

    def change_document(self, uri, version, changes):
        self.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": changes,
            },
        )

    def close(self):
        try:
            self.request("shutdown")
            self.notify("exit")
            self.proc.wait(timeout=5)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = Client(self.tmp.name)
        self.addCleanup(self.client.close)

    def uri(self, name):
        return path_to_uri(os.path.join(self.tmp.name, name))


class InitializeTest(ServerTestCase):
    def test_capabilities(self):
        caps = self.client.capabilities
        self.assertEqual(caps["textDocumentSync"]["change"], 2)
        self.assertTrue(caps["completionProvider"])
        self.assertTrue(caps["definitionProvider"])
        self.assertTrue(caps["referencesProvider"])
        self.assertTrue(caps["hoverProvider"])


class DiagnosticsTest(ServerTestCase):
    def test_syntax_and_semantic_diagnostics(self):
        uri = self.uri("app.scl")
        self.client.open_document(
            uri,
            '[server]\n'
            'host: string = "localhost"\n'
            'port: number = 8080\n'
            'port: number = 9090\n'
            'alias: string = ${server.port}\n'
            'broken = ${ghost.key}\n'
            'bad line here\n',
        )
        diags = self.client.next_diagnostics(uri)
        messages = [d["message"] for d in diags]
        self.assertTrue(any("duplicate key" in m for m in messages),
                        messages)
        self.assertTrue(any("type mismatch" in m for m in messages),
                        messages)
        self.assertTrue(any("unknown reference" in m for m in messages),
                        messages)
        self.assertTrue(any("expected '='" in m for m in messages),
                        messages)
        # Positions are 0-based and accurate.
        dup = next(d for d in diags if "duplicate key" in d["message"])
        self.assertEqual(dup["range"]["start"],
                         {"line": 3, "character": 0})
        unknown = next(d for d in diags if "unknown reference" in d["message"])
        self.assertEqual(unknown["range"]["start"],
                         {"line": 5, "character": 9})

    def test_incremental_change_clears_error(self):
        uri = self.uri("app.scl")
        self.client.open_document(
            uri,
            '[s]\n'
            'target = "x"\n'
            'ref = ${s.targ}\n',
        )
        diags = self.client.next_diagnostics(uri)
        self.assertEqual(len(diags), 1)
        self.assertIn("unknown reference", diags[0]["message"])

        # Incremental edit: insert 'et' to turn ${s.targ} into ${s.target}.
        self.client.change_document(
            uri,
            2,
            [
                {
                    "range": {
                        "start": {"line": 2, "character": 14},
                        "end": {"line": 2, "character": 14},
                    },
                    "text": "et",
                }
            ],
        )
        diags = self.client.next_diagnostics(uri)
        self.assertEqual(diags, [])  # resolved error disappeared

        # Incremental edit that *introduces* an error: delete the 'a' in
        # 'target' so the reference no longer resolves.
        self.client.change_document(
            uri,
            3,
            [
                {
                    "range": {
                        "start": {"line": 2, "character": 11},
                        "end": {"line": 2, "character": 12},
                    },
                    "text": "",
                }
            ],
        )
        diags = self.client.next_diagnostics(uri)
        self.assertEqual(len(diags), 1)
        self.assertIn("unknown reference", diags[0]["message"])

    def test_incremental_delete_line_removes_duplicate(self):
        uri = self.uri("dup.scl")
        self.client.open_document(uri, "[s]\nk = 1\nk = 2\n")
        diags = self.client.next_diagnostics(uri)
        self.assertTrue(any("duplicate" in d["message"] for d in diags))
        # Delete the whole second 'k = 2' line (including its newline).
        self.client.change_document(
            uri,
            2,
            [
                {
                    "range": {
                        "start": {"line": 2, "character": 0},
                        "end": {"line": 3, "character": 0},
                    },
                    "text": "",
                }
            ],
        )
        diags = self.client.next_diagnostics(uri)
        self.assertEqual(diags, [])

    def test_full_document_change(self):
        uri = self.uri("full.scl")
        self.client.open_document(uri, "[s]\nbroken = ${no.key}\n")
        diags = self.client.next_diagnostics(uri)
        self.assertEqual(len(diags), 1)
        self.client.change_document(
            uri, 2, [{"text": "[s]\nok = 1\n"}]
        )
        diags = self.client.next_diagnostics(uri)
        self.assertEqual(diags, [])


class CompletionTest(ServerTestCase):
    def test_key_completion_uses_current_section(self):
        other = self.uri("other.scl")
        self.client.open_document(
            other, "[client]\nretry = 3\ntimeout = 10\n"
        )
        self.client.next_diagnostics(other)
        uri = self.uri("main.scl")
        self.client.open_document(uri, "[client]\nre\n")
        self.client.next_diagnostics(uri)
        items = self.client.request(
            "textDocument/completion",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 1, "character": 2},
            },
        )
        labels = {i["label"] for i in items}
        self.assertIn("retry", labels)
        self.assertIn("timeout", labels)

    def test_reference_completion(self):
        uri = self.uri("refs.scl")
        self.client.open_document(
            uri,
            '[server]\nhost = "h"\nport = 1\n[client]\ntarget = ${\n',
        )
        self.client.next_diagnostics(uri)
        items = self.client.request(
            "textDocument/completion",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 4, "character": 11},
            },
        )
        labels = {i["label"] for i in items}
        self.assertIn("server.host", labels)
        self.assertIn("server.port", labels)

        # After `${server.` only keys of that section are offered.
        self.client.change_document(
            uri, 2, [{"text": '[server]\nhost = "h"\nport = 1\n'
                               '[client]\ntarget = ${server.\n'}]
        )
        self.client.next_diagnostics(uri)
        items = self.client.request(
            "textDocument/completion",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 4, "character": 18},
            },
        )
        labels = {i["label"] for i in items}
        self.assertEqual(labels, {"host", "port"})

    def test_value_and_type_completion(self):
        uri = self.uri("values.scl")
        self.client.open_document(uri, "[s]\nflag = \nname: \n")
        self.client.next_diagnostics(uri)
        items = self.client.request(
            "textDocument/completion",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 1, "character": 8},
            },
        )
        labels = {i["label"] for i in items}
        self.assertIn("true", labels)
        self.assertIn("false", labels)
        items = self.client.request(
            "textDocument/completion",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 2, "character": 6},
            },
        )
        labels = {i["label"] for i in items}
        self.assertEqual(labels, {"string", "number", "bool"})


class CrossFileTest(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.a_uri = self.uri("server.scl")
        self.b_uri = self.uri("client.scl")
        self.client.open_document(
            self.a_uri,
            '[server]\n'
            '# primary host\n'
            'host: string = "localhost"\n'
            'port: number = 8080\n',
        )
        self.client.next_diagnostics(self.a_uri)
        self.client.open_document(
            self.b_uri,
            '[client]\n'
            'endpoint = ${server.host}\n'
            'backup = ${server.host}\n',
        )
        self.client.next_diagnostics(self.b_uri)

    def test_go_to_definition_cross_file(self):
        # 'host' inside ${server.host} on line 1 of client.scl (cols 15-19).
        result = self.client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": self.b_uri},
                "position": {"line": 1, "character": 16},
            },
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["uri"], self.a_uri)
        self.assertEqual(result[0]["range"]["start"],
                         {"line": 2, "character": 0})

    def test_find_references_cross_file(self):
        # On the definition of 'host' in server.scl.
        result = self.client.request(
            "textDocument/references",
            {
                "textDocument": {"uri": self.a_uri},
                "position": {"line": 2, "character": 1},
                "context": {"includeDeclaration": True},
            },
        )
        self.assertEqual(len(result), 3)  # 1 def + 2 refs
        uris = [loc["uri"] for loc in result]
        self.assertEqual(uris.count(self.b_uri), 2)
        self.assertEqual(uris.count(self.a_uri), 1)
        # Without the declaration only the two reference sites remain.
        result = self.client.request(
            "textDocument/references",
            {
                "textDocument": {"uri": self.a_uri},
                "position": {"line": 2, "character": 1},
                "context": {"includeDeclaration": False},
            },
        )
        self.assertEqual(len(result), 2)

    def test_hover_known_symbol(self):
        result = self.client.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": self.a_uri},
                "position": {"line": 2, "character": 1},
            },
        )
        value = result["contents"]["value"]
        self.assertIn("server.host", value)
        self.assertIn("string", value)
        self.assertIn("server.scl", value)
        self.assertIn("primary host", value)

    def test_hover_unknown_symbol_is_safe(self):
        uri = self.uri("unknown.scl")
        self.client.open_document(uri, "[s]\nx = ${ghost.key}\n")
        self.client.next_diagnostics(uri)
        result = self.client.request(
            "textDocument/hover",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 1, "character": 10},
            },
        )
        self.assertIsNone(result)

    def test_definition_of_unknown_symbol_is_safe(self):
        uri = self.uri("unknown2.scl")
        self.client.open_document(uri, "[s]\nx = ${ghost.key}\n")
        self.client.next_diagnostics(uri)
        result = self.client.request(
            "textDocument/definition",
            {
                "textDocument": {"uri": uri},
                "position": {"line": 1, "character": 10},
            },
        )
        self.assertIsNone(result)


class WorkspaceScanTest(unittest.TestCase):
    """Symbols from *.scl files on local disk are indexed at initialize."""

    def test_disk_files_are_indexed(self):
        with tempfile.TemporaryDirectory() as tmp:
            defs_path = os.path.join(tmp, "defs.scl")
            with open(defs_path, "w", encoding="utf-8") as handle:
                handle.write('[shared]\nname: string = "disk"\n')
            client = Client(tmp)
            self.addCleanup(client.close)
            uri = path_to_uri(os.path.join(tmp, "use.scl"))
            client.open_document(uri, "[app]\ncopy = ${shared.name}\n")
            diags = client.next_diagnostics(uri)
            self.assertEqual(diags, [])  # reference resolves to disk file
            result = client.request(
                "textDocument/definition",
                {
                    "textDocument": {"uri": uri},
                    "position": {"line": 1, "character": 15},
                },
            )
            self.assertEqual(result[0]["uri"], path_to_uri(defs_path))


if __name__ == "__main__":
    unittest.main()
