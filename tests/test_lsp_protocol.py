"""End-to-end LSP tests: real server process driven over raw JSON-RPC.

Run them with ``pytest`` (or ``python -m unittest``) from the repository
root. No editor is launched anywhere.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from conf_lsp.protocol import FrameReader, FrameWriter
from conf_lsp.workspace import (
    DUPLICATE_KEY,
    MISSING_IMPORT,
    SYNTAX_ERROR,
    TYPE_MISMATCH,
    UNKNOWN_REFERENCE,
    path_to_uri,
)

from lsp_client import LSPClient


class FramingTests(unittest.TestCase):
    def test_frame_roundtrip_with_crlf(self):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "shutdown"}
        sink = io.BytesIO()
        FrameWriter(sink).write_frame(payload)
        sink.seek(0)
        self.assertTrue(sink.getvalue().startswith(b"Content-Length:"))
        self.assertIn(b"\r\n\r\n", sink.getvalue())
        decoded = FrameReader(sink).read_frame()
        self.assertEqual(decoded, payload)

    def test_partial_frames_buffered(self):
        class ChunkStream(io.RawIOBase):
            def __init__(self, data):
                self.data = data
                self.pos = 0

            def readable(self):
                return True

            def readinto(self, buffer):
                if self.pos >= len(self.data):
                    return 0
                n = min(len(buffer), 3)  # deliver only three bytes at a time
                chunk = self.data[self.pos:self.pos + n]
                buffer[:len(chunk)] = chunk
                self.pos += len(chunk)
                return len(chunk)

        payload = {"jsonrpc": "2.0", "id": 7, "method": "ping",
                   "params": {"x": "héllo"}}
        sink = io.BytesIO()
        FrameWriter(sink).write_frame(payload)
        decoded = FrameReader(io.BufferedReader(ChunkStream(sink.getvalue()))).read_frame()
        self.assertEqual(decoded, payload)


class ServerTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="conf-lsp-test-")
        self.client = LSPClient(self.tmpdir)

    def tearDown(self):
        self.client.close()

    def write_conf(self, name: str, text: str) -> str:
        path = os.path.join(self.tmpdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path_to_uri(path)

    def uri(self, name: str) -> str:
        return path_to_uri(os.path.join(self.tmpdir, name))

    def start_server(self):
        info = self.client.initialize()
        return info


class InitializationTests(ServerTestBase):
    def test_initialize_handshake_and_disk_scan(self):
        self.write_conf("base.conf", "name: \"on disk\"\n")
        info = self.start_server()
        self.assertIn("capabilities", info)
        caps = info["capabilities"]
        self.assertTrue(caps["hoverProvider"])
        self.assertTrue(caps["definitionProvider"])
        self.assertTrue(caps["referencesProvider"])
        self.assertEqual(caps["textDocumentSync"]["change"], 2)
        # The disk scan indexes files even before they are opened: an
        # in-memory file referencing a disk-only symbol resolves cleanly.
        uri = self.uri("main.conf")
        self.client.open(uri, 'import "base.conf"\nref: base.name\n')
        self.assertEqual(self.client.diagnostics_for(uri), [])

    def test_shutdown_then_exit_is_clean(self):
        self.start_server()
        result = self.client.shutdown()
        self.assertNotIn("error", result)
        self.client.send_exit()
        # exit with status 0 after a successful shutdown
        self.client.process.stdin.close()
        code = self.client.process.wait(timeout=5)
        self.assertEqual(code, 0)
        self.client.process = None

    def test_unknown_request_returns_method_not_found(self):
        self.start_server()
        response = self.client.request("workspace/didChangeWatchedFiles", {})
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32601)


class DiagnosticsOverWireTests(ServerTestBase):
    def test_diagnostics_codes_on_open(self):
        uri = self.uri("a.conf")
        text = ('import "ghost.conf"\n'
                '[s]\nx: int "bad"\ny: nowhere.key\nz: 1\nz: 2\n[s]\n')
        self.start_server()
        self.client.open(uri, text)
        diagnostics = self.client.diagnostics_for(uri)
        codes = {d["code"] for d in diagnostics}
        self.assertIn(MISSING_IMPORT, codes)
        self.assertIn(TYPE_MISMATCH, codes)
        self.assertIn(UNKNOWN_REFERENCE, codes)
        self.assertIn(DUPLICATE_KEY, codes)
        # Duplicate section is reported too.
        from conf_lsp.workspace import DUPLICATE_SECTION
        self.assertIn(DUPLICATE_SECTION, codes)

    def test_clean_file_has_no_diagnostics(self):
        uri = self.uri("a.conf")
        self.start_server()
        self.client.open(uri, "x: 1\n[s]\ny: \"ok\"\n")
        self.assertEqual(self.client.diagnostics_for(uri), [])


class IncrementalSyncTests(ServerTestBase):
    def test_fixing_error_clears_diagnostic_without_restart(self):
        uri = self.uri("a.conf")
        self.start_server()
        self.client.open(uri, "x: int \"bad\"\n")
        codes = {d["code"] for d in self.client.diagnostics_for(uri)}
        self.assertIn(TYPE_MISMATCH, codes)

        # Replace just the string with a number (incremental range edit).
        text = "x: int \"bad\"\n"
        start = {"line": 0, "character": text.index('"')}
        end = {"line": 0, "character": len(text.rstrip("\n"))}
        self.client.change_range(uri, start, end, "8080", version=2)
        diagnostics = self.client.diagnostics_for(uri)
        self.assertEqual(diagnostics, [])

    def test_introducing_error_publishes_diagnostic(self):
        uri = self.uri("a.conf")
        self.start_server()
        self.client.open(uri, "x: 1\n")
        self.assertEqual(self.client.diagnostics_for(uri), [])
        self.client.change_full(uri, "x: int true\n", version=2)
        codes = {d["code"] for d in self.client.diagnostics_for(uri)}
        self.assertIn(TYPE_MISMATCH, codes)

    def test_unknown_reference_disappears_after_target_added(self):
        target = self.uri("b.conf")
        main = self.uri("a.conf")
        self.write_conf("b.conf", "")  # exists but empty
        self.start_server()
        self.client.open(main, 'import "b.conf"\nref: b.name\n')
        codes = {d["code"] for d in self.client.diagnostics_for(main)}
        self.assertIn(UNKNOWN_REFERENCE, codes)

        # Add the referenced key; diagnostics on the referencing file update
        # immediately (the workspace reindexes the open document).
        self.client.open(target, "name: \"now defined\"\n")
        codes = {d["code"] for d in self.client.diagnostics_for(main)}
        self.assertNotIn(UNKNOWN_REFERENCE, codes)

    def test_close_returns_to_disk_content(self):
        uri = self.write_conf("a.conf", "x: 1\n")
        self.start_server()
        # Introduce a broken in-memory version.
        self.client.open(uri, "x: int \"broken\"\n")
        self.assertIn(TYPE_MISMATCH,
                      {d["code"] for d in self.client.diagnostics_for(uri)})
        self.client.close_document(uri)
        diagnostics = self.client.diagnostics_for(uri)
        self.assertEqual(diagnostics, [])


class CompletionTests(ServerTestBase):
    def _labels(self, items):
        return sorted(item["label"] for item in items)

    def test_key_completion_suggests_unused_keys(self):
        self.write_conf("base.conf", "[server]\nhost: \"a\"\nport: 80\n")
        self.start_server()
        uri = self.uri("a.conf")
        self.client.open(uri, "[server]\nhost: \"a\"\npor")
        items = self.client.completion(uri, 2, 3)
        labels = self._labels(items)
        self.assertIn("port", labels)
        self.assertNotIn("host", labels)  # already present in this section

    def test_completion_filters_by_prefix(self):
        uri = self.uri("a.conf")
        self.start_server()
        self.client.open(uri, "alpha: 1\nbeta: 2\n")
        items = self.client.completion(uri, 1, 1)  # after the "b" in "beta"
        labels = self._labels(items)
        self.assertIn("beta", labels)
        self.assertNotIn("alpha", labels)

    def test_reference_completion_in_value(self):
        uri = self.uri("a.conf")
        self.start_server()
        self.client.open(uri, "host: \"a\"\nport: 80\ncopy: hos")
        items = self.client.completion(uri, 2, len("copy: hos"))
        labels = self._labels(items)
        self.assertIn("host", labels)
        self.assertNotIn("beta", labels)

    def test_interpolation_completion(self):
        uri = self.uri("a.conf")
        self.start_server()
        self.client.open(uri, "host: \"a\"\nurl: \"http://${hos}\"")
        line = 1
        character = 'url: "http://${hos'.__len__()
        items = self.client.completion(uri, line, character)
        labels = self._labels(items)
        self.assertIn("host", labels)

    def test_type_annotation_completion(self):
        uri = self.uri("a.conf")
        self.start_server()
        self.client.open(uri, "x: in")
        items = self.client.completion(uri, 0, 5)
        labels = self._labels(items)
        self.assertIn("int", labels)
        # Other builtin types that do not match the prefix are filtered out.
        self.assertNotIn("bool", labels)


class CrossFileNavigationTests(ServerTestBase):
    def setUp(self):
        super().setUp()
        self.write_conf("base.conf", "[server]\n# The host name\nhost: \"a\"\n")
        self.main_uri = self.uri("main.conf")
        self.main_text = 'import "base.conf"\n[server]\nref: base.server.host\n'
        self.start_server()
        self.client.open(self.main_uri, self.main_text)

    def _ref_position(self):
        line = 2
        prefix = "ref: base.server."
        return line, prefix.__len__()  # inside "host"

    def test_goto_definition_cross_file(self):
        line, char = self._ref_position()
        result = self.client.definition(self.main_uri, line, char)
        self.assertEqual(result["uri"], self.uri("base.conf"))
        rng = result["range"]
        self.assertEqual(rng["start"]["line"], 2)
        self.assertEqual(rng["start"]["character"], 0)

    def test_find_references_cross_file(self):
        base = self.uri("base.conf")
        results = self.client.references(base, 2, 0)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["uri"], self.main_uri)

    def test_hover_declaration(self):
        base = self.uri("base.conf")
        result = self.client.hover(base, 2, 0)
        self.assertIsNotNone(result)
        value = result["contents"]["value"]
        self.assertIn("host", value)
        self.assertIn("The host name", value)
        self.assertIn("base.conf", value)

    def test_hover_reference_target(self):
        line, char = self._ref_position()
        result = self.client.hover(self.main_uri, line, char)
        self.assertIsNotNone(result)
        self.assertIn("host", result["contents"]["value"])
        self.assertIn("base.conf", result["contents"]["value"])

    def test_hover_unknown_reference_is_safe(self):
        uri = self.uri("other.conf")
        self.client.open(uri, "x: ghost.ref\n")
        result = self.client.hover(uri, 0, len("x: ghost"))
        self.assertIsNotNone(result)
        self.assertIn("unknown", result["contents"]["value"].lower())

    def test_hover_on_blank_area_returns_null(self):
        result = self.client.hover(self.main_uri, 0, 0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
