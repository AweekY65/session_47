"""The Language Server itself: JSON-RPC dispatch over stdio.

Supported methods
-----------------
- ``initialize`` / ``initialized`` / ``shutdown`` / ``exit``
- ``textDocument/publishDiagnostics`` notifications (server -> client)
- ``textDocument/didOpen`` / ``didChange`` (incremental) / ``didClose`` /
  ``didSave``
- ``textDocument/completion``
- ``textDocument/hover``
- ``textDocument/definition``
- ``textDocument/references``

The process never opens a network connection: the workspace is scanned from
the local root directory supplied at initialization and all document state
lives in memory.
"""

from __future__ import annotations

import sys

from . import __version__
from .positions import LineIndex
from .protocol import FrameReader, FrameWriter, ProtocolError
from .workspace import Workspace, uri_to_path

PARSE_ERROR_CODE = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601

# LSP diagnostic severities
PUBLISH_ERROR = 1
PUBLISH_WARNING = 2


class LanguageServer:
    def __init__(self, reader: FrameReader, writer: FrameWriter):
        self.reader = reader
        self.writer = writer
        self.workspace = Workspace()
        self.indexes: dict[str, LineIndex] = {}
        self.open_documents: set[str] = set()
        self.root_path: str | None = None
        self.shutdown_requested = False

    # -- main loop ------------------------------------------------------------

    def run(self) -> int:
        while True:
            try:
                message = self.reader.read_frame()
            except ProtocolError as exc:
                # A malformed frame must not take the server down; report a
                # JSON-RPC parse error (id unknown -> null) and keep going.
                self._error(None, PARSE_ERROR_CODE, str(exc))
                continue
            except Exception:
                break
            if message is None:
                break
            self.dispatch(message)
        # Per the LSP spec, exit code 1 unless shutdown was requested.
        return 0 if self.shutdown_requested else 1

    def dispatch(self, message: dict) -> None:
        if not isinstance(message, dict):
            return
        method = message.get("method")
        msg_id = message.get("id")

        if method == "exit":
            sys.exit(0 if self.shutdown_requested else 1)

        handler = getattr(self, "m_" + method.replace("/", "_"), None)
        if handler is None:
            if msg_id is not None:
                self._error(msg_id, METHOD_NOT_FOUND,
                            "method not found: %s" % method)
            return

        params = message.get("params") or {}
        try:
            result = handler(params)
        except Exception as exc:  # a bad request must never kill the server
            if msg_id is not None:
                self._error(msg_id, INVALID_REQUEST, str(exc))
            return
        if msg_id is not None:
            self.writer.write_frame(
                {"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id, code: int, message: str) -> None:
        self.writer.write_frame({
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message},
        })

    def notify(self, method: str, params: dict) -> None:
        self.writer.write_frame(
            {"jsonrpc": "2.0", "method": method, "params": params})

    # -- lifecycle ------------------------------------------------------------

    def m_initialize(self, params: dict) -> dict:
        root = (params.get("rootUri")
                or params.get("rootPath")
                or params.get("workspaceFolders"))
        if isinstance(root, list) and root:
            root = root[0].get("uri")
        if isinstance(root, str) and root.startswith("file://"):
            self.root_path = uri_to_path(root)
        elif isinstance(root, str) and root:
            self.root_path = root
        if self.root_path:
            self.workspace.load_from_disk(self.root_path)
        return {
            "capabilities": {
                "textDocumentSync": {
                    "openClose": True,
                    "change": 2,  # Incremental
                    "save": {"includeText": False},
                },
                "completionProvider": {
                    "resolveProvider": False,
                    "triggerCharacters": [".", ":"],
                },
                "hoverProvider": True,
                "definitionProvider": True,
                "referencesProvider": True,
            },
            "serverInfo": {"name": "conf-language-server", "version": __version__},
        }

    def m_initialized(self, params: dict) -> None:
        return None

    def m_shutdown(self, params: dict) -> None:
        self.shutdown_requested = True
        return None

    # -- document sync --------------------------------------------------------

    def _publish_diagnostics(self, uri: str) -> None:
        doc = self.workspace.get_document(uri)
        if doc is None:
            self.notify("textDocument/publishDiagnostics",
                        {"uri": uri, "diagnostics": []})
            return
        index = self.indexes.get(uri) or LineIndex.create(doc.text)
        lsp_diagnostics = []
        for diag in self.workspace.diagnostics(uri):
            lsp_diagnostics.append({
                "range": index.range_for(diag.start, diag.end),
                "severity": (PUBLISH_WARNING if diag.severity == 2
                             else PUBLISH_ERROR),
                "code": diag.code,
                "source": "conf",
                "message": diag.message,
            })
        self.notify("textDocument/publishDiagnostics",
                    {"uri": uri, "diagnostics": lsp_diagnostics})

    def _text(self, params: dict) -> tuple[str, str]:
        item = params["textDocument"]
        return item["uri"], item.get("text")

    def m_textDocument_didOpen(self, params: dict) -> None:
        uri, text = self._text(params)
        if text is None:
            return
        self.workspace.set_document(uri, text)
        self.indexes[uri] = LineIndex.create(text)
        self.open_documents.add(uri)
        self._publish_all()

    def m_textDocument_didChange(self, params: dict) -> None:
        uri = params["textDocument"]["uri"]
        doc = self.workspace.get_document(uri)
        if doc is None:
            text = ""
            for change in params.get("contentChanges", []):
                if "range" not in change:
                    text = change["text"]
            self.workspace.set_document(uri, text)
            self.indexes[uri] = LineIndex.create(text)
        else:
            text = doc.text
            index = self.indexes.get(uri) or LineIndex.create(text)
            for change in params.get("contentChanges", []):
                if "range" not in change:
                    text = change["text"]
                    index = LineIndex.create(text)
                else:
                    text, index = apply_incremental(text, index, change)
            self.workspace.set_document(uri, text)
            self.indexes[uri] = index
        self.open_documents.add(uri)
        self._publish_all()

    def m_textDocument_didSave(self, params: dict) -> None:
        return None

    def m_textDocument_didClose(self, params: dict) -> None:
        uri = params["textDocument"]["uri"]
        self.open_documents.discard(uri)
        self.indexes.pop(uri, None)
        self.workspace.close_document(uri)
        doc = self.workspace.get_document(uri)
        if doc is not None:
            self.indexes[uri] = LineIndex.create(doc.text)
        # Publish once more even though it is now closed, so the client
        # clears diagnostics that only existed in the in-memory version.
        self._publish_diagnostics(uri)
        self._publish_all()

    def _publish_all(self) -> None:
        """Republish diagnostics for every open document.

        Editing one file can change the diagnostics of another (e.g. adding
        a previously unknown reference target), so all open documents are
        re-published together.
        """
        for uri in sorted(self.open_documents):
            self._publish_diagnostics(uri)

    # -- language features ----------------------------------------------------

    def _position_params(self, params: dict):
        uri = params["textDocument"]["uri"]
        return uri, params.get("position", {})

    def m_textDocument_hover(self, params: dict):
        uri, position = self._position_params(params)
        doc = self.workspace.get_document(uri)
        if doc is None:
            return None
        index = self.indexes.get(uri) or LineIndex.create(doc.text)
        offset = index.offset(position)
        markup = self.workspace.hover(uri, offset)
        if markup is None:
            return None
        return {"contents": {"kind": "markdown", "value": markup}}

    def m_textDocument_definition(self, params: dict):
        uri, position = self._position_params(params)
        doc = self.workspace.get_document(uri)
        if doc is None:
            return None
        index = self.indexes.get(uri) or LineIndex.create(doc.text)
        found = self.workspace.definition(uri, index.offset(position))
        if found is None:
            return None
        target_uri, target_range = found
        target_doc = self.workspace.get_document(target_uri)
        target_index = self.indexes.get(target_uri) or \
            LineIndex.create(target_doc.text)
        return {
            "uri": target_uri,
            "range": target_range.to_lsp(target_index),
        }

    def m_textDocument_references(self, params: dict):
        uri, position = self._position_params(params)
        doc = self.workspace.get_document(uri)
        if doc is None:
            return []
        index = self.indexes.get(uri) or LineIndex.create(doc.text)
        locations = []
        for target_uri, target_range in self.workspace.find_references(
                uri, index.offset(position)):
            target_doc = self.workspace.get_document(target_uri)
            target_index = self.indexes.get(target_uri) or \
                LineIndex.create(target_doc.text)
            locations.append({
                "uri": target_uri,
                "range": target_range.to_lsp(target_index),
            })
        return locations

    def m_textDocument_completion(self, params: dict):
        uri, position = self._position_params(params)
        doc = self.workspace.get_document(uri)
        if doc is None:
            return []
        index = self.indexes.get(uri) or LineIndex.create(doc.text)
        return self.workspace.completion(uri, index.offset(position))


def apply_incremental(text: str, index: LineIndex, change: dict):
    """Apply one LSP range-based content change; return ``(text, index)``.

    The old range is interpreted in UTF-16 units as LSP requires.
    """
    rng = change["range"]
    start = index.offset(rng["start"])
    end = index.offset(rng["end"])
    new_text = text[:start] + change["text"] + text[end:]
    return new_text, LineIndex.create(new_text)


def main() -> None:
    # Binary stdio keeps framing byte-exact; ensure Windows is sane too.
    reader = FrameReader(getattr(sys.stdin, "buffer", sys.stdin))
    writer = FrameWriter(getattr(sys.stdout, "buffer", sys.stdout))
    server = LanguageServer(reader, writer)
    sys.exit(server.run())


if __name__ == "__main__":
    main()
