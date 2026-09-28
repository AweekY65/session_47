"""JSON-RPC over stdio language server (LSP subset) for SCL.

Run with:  python3 -m scls

The server speaks the Language Server Protocol base protocol
(``Content-Length`` framed JSON-RPC messages) on stdin/stdout. It never
touches the network, a database or any external service: documents live in
memory, and the only filesystem access is reading ``*.scl`` files from the
local workspace folder announced during ``initialize``.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from .analyzer import Workspace, display_path, uri_to_path
from .parser import Diagnostic, Range

# LSP CompletionItemKind
_KIND_FIELD = 5
_KIND_VALUE = 12
_KIND_KEYWORD = 14
_KIND_SNIPPET = 15
_KIND_REFERENCE = 18


def path_to_uri(path: str) -> str:
    return "file://" + quote(os.path.abspath(path))


def range_to_lsp(rng: Range) -> Dict[str, Any]:
    return {
        "start": {"line": rng.start_line, "character": rng.start_col},
        "end": {"line": rng.end_line, "character": rng.end_col},
    }


def diagnostic_to_lsp(diag: Diagnostic) -> Dict[str, Any]:
    return {
        "range": range_to_lsp(diag.range),
        "severity": diag.severity,
        "source": diag.source,
        "message": diag.message,
    }


def position_to_offset(text: str, line: int, character: int) -> int:
    """Convert an LSP position (UTF-16 code units) to a Python offset."""
    lines = text.split("\n")
    if line >= len(lines):
        return len(text)
    offset = sum(len(l) + 1 for l in lines[:line])
    line_text = lines[line]
    index = 0
    units = 0
    while index < len(line_text) and units < character:
        units += 2 if ord(line_text[index]) > 0xFFFF else 1
        index += 1
    return offset + index


def apply_range_edit(text: str, rng: Dict[str, Any], new_text: str) -> str:
    start = position_to_offset(
        text, rng["start"]["line"], rng["start"]["character"]
    )
    end = position_to_offset(
        text, rng["end"]["line"], rng["end"]["character"]
    )
    return text[:start] + new_text + text[end:]


class Server:
    def __init__(self, stdin=None, stdout=None) -> None:
        self.stdin = stdin if stdin is not None else sys.stdin.buffer
        self.stdout = stdout if stdout is not None else sys.stdout.buffer
        self.workspace = Workspace()
        self.shutdown_requested = False
        self.running = True

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------
    def _read_message(self) -> Optional[Dict[str, Any]]:
        headers: Dict[str, str] = {}
        while True:
            line = self.stdin.readline()
            if not line:
                return None  # EOF
            line = line.strip()
            if not line:
                break
            key, _, value = line.decode("ascii", "replace").partition(":")
            headers[key.strip().lower()] = value.strip()
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            return None
        body = self.stdin.read(length)
        if not body:
            return None
        return json.loads(body.decode("utf-8"))

    def _send(self, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.stdout.write(b"Content-Length: %d\r\n\r\n" % len(data) + data)
        self.stdout.flush()

    def _respond(self, msg_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _respond_error(self, msg_id: Any, code: int, message: str) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": code, "message": message},
            }
        )

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send(
            {"jsonrpc": "2.0", "method": method, "params": params}
        )

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def serve(self) -> None:
        while self.running:
            message = self._read_message()
            if message is None:
                break
            self._dispatch(message)

    def _dispatch(self, message: Dict[str, Any]) -> None:
        method = message.get("method", "")
        msg_id = message.get("id")
        params = message.get("params") or {}
        is_request = "id" in message
        try:
            result = self._handle(method, params, is_request)
        except Exception as exc:  # never crash the loop on one bad message
            if is_request:
                self._respond_error(msg_id, -32603, f"{type(exc).__name__}: {exc}")
            return
        if is_request:
            self._respond(msg_id, result)

    def _handle(self, method: str, params: Dict[str, Any],
                is_request: bool) -> Any:
        if method == "initialize":
            return self._initialize(params)
        if method == "initialized":
            return None
        if method == "shutdown":
            self.shutdown_requested = True
            return None
        if method == "exit":
            self.running = False
            return None
        if method == "textDocument/didOpen":
            self._did_open(params)
            return None
        if method == "textDocument/didChange":
            self._did_change(params)
            return None
        if method == "textDocument/didClose":
            self._did_close(params)
            return None
        if method == "textDocument/completion":
            return self._completion(params)
        if method == "textDocument/definition":
            return self._definition(params)
        if method == "textDocument/references":
            return self._references(params)
        if method == "textDocument/hover":
            return self._hover(params)
        if method.startswith("$/") or method.startswith("workspace/"):
            return None  # optional notifications we do not use
        if is_request:
            raise ValueError(f"unsupported method: {method}")
        return None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def _initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        root_uri = params.get("rootUri")
        if not root_uri:
            folders = params.get("workspaceFolders") or []
            if folders:
                root_uri = folders[0].get("uri")
        if root_uri and root_uri.startswith("file://"):
            self._scan_workspace(uri_to_path(root_uri))
        return {
            "capabilities": {
                "textDocumentSync": {"openClose": True, "change": 2},
                "completionProvider": {
                    "triggerCharacters": ["{", ".", ":", "="],
                    "resolveProvider": False,
                },
                "definitionProvider": True,
                "referencesProvider": True,
                "hoverProvider": True,
            },
            "serverInfo": {"name": "scls", "version": "0.1.0"},
        }

    def _scan_workspace(self, root: str) -> None:
        """Index every local *.scl file under *root* (local disk only)."""
        if not os.path.isdir(root):
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for filename in sorted(filenames):
                if not filename.endswith(".scl"):
                    continue
                path = os.path.join(dirpath, filename)
                uri = path_to_uri(path)
                if uri in self.workspace.docs:
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as handle:
                        self.workspace.update(uri, handle.read())
                except OSError:
                    continue

    # ------------------------------------------------------------------
    # document sync
    # ------------------------------------------------------------------
    def _did_open(self, params: Dict[str, Any]) -> None:
        doc = params.get("textDocument", {})
        uri = doc.get("uri", "")
        self.workspace.update(
            uri, doc.get("text", ""), doc.get("version")
        )
        self._publish_all_diagnostics()

    def _did_change(self, params: Dict[str, Any]) -> None:
        doc = params.get("textDocument", {})
        uri = doc.get("uri", "")
        state = self.workspace.docs.get(uri)
        if state is None:
            return
        text = state.text
        for change in params.get("contentChanges", []):
            if "range" in change:  # incremental edit
                text = apply_range_edit(text, change["range"],
                                        change.get("text", ""))
            else:  # full document sync
                text = change.get("text", "")
        self.workspace.update(uri, text, doc.get("version"))
        self._publish_all_diagnostics()

    def _did_close(self, params: Dict[str, Any]) -> None:
        uri = params.get("textDocument", {}).get("uri", "")
        # Fall back to the on-disk copy if there is one, otherwise drop it.
        path = uri_to_path(uri) if uri.startswith("file://") else ""
        if path and os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    self.workspace.update(uri, handle.read())
            except OSError:
                self.workspace.remove(uri)
        else:
            self.workspace.remove(uri)
            self._notify(
                "textDocument/publishDiagnostics",
                {"uri": uri, "diagnostics": []},
            )
        self._publish_all_diagnostics()

    def _publish_all_diagnostics(self) -> None:
        """Republish diagnostics for every known document.

        Cross-file rules (duplicate keys, references) mean one edit can fix
        or break other files, so every document is refreshed and resolved
        errors disappear immediately.
        """
        for uri in sorted(self.workspace.docs):
            diags = self.workspace.diagnostics(uri)
            self._notify(
                "textDocument/publishDiagnostics",
                {
                    "uri": uri,
                    "diagnostics": [diagnostic_to_lsp(d) for d in diags],
                },
            )

    # ------------------------------------------------------------------
    # language features
    # ------------------------------------------------------------------
    def _completion(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        uri = params.get("textDocument", {}).get("uri", "")
        position = params.get("position", {})
        line, character = position.get("line", 0), position.get("character", 0)
        state = self.workspace.docs.get(uri)
        if state is None:
            return []
        lines = state.text.split("\n")
        if line >= len(lines):
            return []
        prefix = lines[line][:character]
        table = self.workspace.symbols()

        # 1. Inside a reference: `${...` or `${section.`
        ref_key = re.search(r"\$\{\s*([\w-]+)\.\s*[\w-]*$", prefix)
        if ref_key:
            section = ref_key.group(1)
            return [
                {
                    "label": key,
                    "kind": _KIND_FIELD,
                    "detail": f"key in [{section}]",
                    "insertText": key,
                }
                for (sec, key) in sorted(table)
                if sec == section
            ]
        if re.search(r"\$\{\s*[\w-]*$", prefix):
            return [
                {
                    "label": f"{section}.{key}",
                    "kind": _KIND_REFERENCE,
                    "detail": "reference",
                    "insertText": f"{section}.{key}",
                }
                for (section, key) in sorted(table)
            ]

        # 2. Type annotation position: `key: ...`
        if re.match(r"^\s*[A-Za-z_][\w-]*\s*:\s*\w*$", prefix):
            return [
                {"label": t, "kind": _KIND_KEYWORD, "detail": "type"}
                for t in ("string", "number", "bool")
            ]

        # 3. Value position: `key = ...`
        if re.match(r"^\s*[A-Za-z_][\w-]*\s*(:\s*\w+\s*)?=\s*[\w$]*$",
                    prefix):
            items = [
                {"label": "true", "kind": _KIND_VALUE, "detail": "bool"},
                {"label": "false", "kind": _KIND_VALUE, "detail": "bool"},
            ]
            items += [
                {
                    "label": f"${{{section}.{key}}}",
                    "kind": _KIND_SNIPPET,
                    "detail": "reference",
                    "insertText": f"${{{section}.{key}}}",
                }
                for (section, key) in sorted(table)
            ]
            return items

        # 4. Key position at the start of a line: keys of the current
        #    section as used across the workspace.
        if re.match(r"^\s*[\w-]*$", prefix):
            section = self.workspace.section_at(uri, line)
            if section is None:
                return []
            return [
                {
                    "label": key,
                    "kind": _KIND_FIELD,
                    "detail": f"key in [{section}]",
                }
                for (sec, key) in sorted(table)
                if sec == section
            ]
        return []

    def _definition(self, params: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
        target = self._symbol_at_params(params)
        if target is None:
            return None
        defs = self.workspace.symbols().get(target)
        if not defs:
            return None
        return [
            {"uri": sym.uri, "range": range_to_lsp(sym.range)}
            for sym in defs
        ]

    def _references(self, params: Dict[str, Any]) -> List[Dict[str, Any]]:
        target = self._symbol_at_params(params)
        if target is None:
            return []
        include_decl = (params.get("context") or {}).get(
            "includeDeclaration", True
        )
        locations: List[Dict[str, Any]] = []
        if include_decl:
            for sym in self.workspace.symbols().get(target, []):
                locations.append(
                    {"uri": sym.uri, "range": range_to_lsp(sym.range)}
                )
        for uri, value in self.workspace.references():
            if (value.ref_section, value.ref_key) == target:
                locations.append(
                    {"uri": uri, "range": range_to_lsp(value.range)}
                )
        return locations

    def _hover(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        uri = params.get("textDocument", {}).get("uri", "")
        position = params.get("position", {})
        target = self.workspace.symbol_at(
            uri, position.get("line", 0), position.get("character", 0)
        )
        if target is None:
            return None  # unknown position: safe no-op
        table = self.workspace.symbols()
        defs = table.get(target)
        if not defs:
            return None  # unknown symbol: safe no-op
        sym = defs[0]
        resolved = self.workspace.resolve_type(table, *target)
        type_text = resolved or "unknown"
        lines = [
            f"**`{sym.name}`** : `{type_text}`",
            "",
            f"Defined in `{display_path(sym.uri)}` "
            f"line {sym.range.start_line + 1}",
        ]
        value = sym.entry.value
        if value is not None and value.kind != "error":
            lines.append(f"\nValue: `{value.raw}`")
        if sym.entry.doc:
            lines.append(f"\n{sym.entry.doc}")
        return {
            "contents": {"kind": "markdown", "value": "\n".join(lines)},
            "range": range_to_lsp(sym.range),
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _symbol_at_params(
        self, params: Dict[str, Any]
    ) -> Optional[Tuple[str, str]]:
        uri = params.get("textDocument", {}).get("uri", "")
        position = params.get("position", {})
        return self.workspace.symbol_at(
            uri, position.get("line", 0), position.get("character", 0)
        )


def main() -> None:
    Server().serve()


if __name__ == "__main__":
    main()
