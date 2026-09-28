"""Workspace analysis: symbol index, diagnostics, references.

Everything is computed in memory from the documents the workspace knows
about (opened via the protocol or scanned from the local workspace folder).
No external services are involved.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from .parser import Diagnostic, Document, Entry, Range, Value, parse


def uri_to_path(uri: str) -> str:
    return unquote(urlparse(uri).path)


def display_path(uri: str) -> str:
    """Short human-readable path for messages."""
    try:
        return os.path.basename(uri_to_path(uri)) or uri
    except Exception:
        return uri


@dataclass
class Symbol:
    section: str
    key: str
    uri: str
    range: Range  # range of the defining key token
    entry: Entry

    @property
    def name(self) -> str:
        return f"{self.section}.{self.key}"


class DocumentState:
    def __init__(self, uri: str, text: str, version: Optional[int] = None):
        self.uri = uri
        self.update(text, version)

    def update(self, text: str, version: Optional[int]) -> None:
        self.text = text
        self.version = version
        self.ast: Document = parse(text)


class Workspace:
    """Holds every known document plus derived indexes."""

    def __init__(self) -> None:
        self.docs: Dict[str, DocumentState] = {}

    # ------------------------------------------------------------------
    # document lifecycle
    # ------------------------------------------------------------------
    def update(self, uri: str, text: str,
               version: Optional[int] = None) -> None:
        if uri in self.docs:
            self.docs[uri].update(text, version)
        else:
            self.docs[uri] = DocumentState(uri, text, version)

    def remove(self, uri: str) -> None:
        self.docs.pop(uri, None)

    # ------------------------------------------------------------------
    # indexes
    # ------------------------------------------------------------------
    def symbols(self) -> Dict[Tuple[str, str], List[Symbol]]:
        """Map (section, key) -> definitions, in deterministic order."""
        table: Dict[Tuple[str, str], List[Symbol]] = {}
        for uri in sorted(self.docs):
            for entry in self.docs[uri].ast.entries():
                symbol = Symbol(
                    entry.section, entry.key, uri, entry.key_range, entry
                )
                table.setdefault((entry.section, entry.key), []).append(
                    symbol
                )
        return table

    def references(self) -> List[Tuple[str, Value]]:
        """All reference expressions as (uri, value) pairs."""
        out: List[Tuple[str, Value]] = []
        for uri in sorted(self.docs):
            for entry in self.docs[uri].ast.entries():
                if entry.value is not None and entry.value.kind == "ref":
                    out.append((uri, entry.value))
        return out

    def resolve_type(
        self,
        table: Dict[Tuple[str, str], List[Symbol]],
        section: str,
        key: str,
        seen: Tuple[Tuple[str, str], ...] = (),
    ) -> Optional[str]:
        """Effective type of a symbol, following references transitively.

        Returns None for unknown symbols or reference cycles (safe default).
        """
        defs = table.get((section, key))
        if not defs:
            return None
        entry = defs[0].entry
        if entry.annotation:
            return entry.annotation
        value = entry.value
        if value is None:
            return None
        if value.kind == "ref":
            target = (value.ref_section or "", value.ref_key or "")
            if target in seen:
                return None  # reference cycle: give up safely
            return self.resolve_type(table, target[0], target[1],
                                     seen + ((section, key),))
        return value.kind

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def symbol_at(self, uri: str, line: int,
                  col: int) -> Optional[Tuple[str, str]]:
        """The (section, key) symbol referenced or defined at a position."""
        state = self.docs.get(uri)
        if state is None:
            return None
        for entry in state.ast.entries():
            value = entry.value
            if value is not None and value.kind == "ref":
                if (
                    value.ref_section_range
                    and value.ref_section_range.contains(line, col)
                ) or (
                    value.ref_key_range
                    and value.ref_key_range.contains(line, col)
                ):
                    return (value.ref_section or "", value.ref_key or "")
            if entry.key_range.contains(line, col):
                return (entry.section, entry.key)
        return None

    def section_at(self, uri: str, line: int) -> Optional[str]:
        """Name of the section enclosing *line* (for completion)."""
        state = self.docs.get(uri)
        if state is None:
            return None
        name: Optional[str] = None
        for section in state.ast.sections:
            if section.name_range.start_line <= line:
                name = section.name
            else:
                break
        return name

    # ------------------------------------------------------------------
    # diagnostics
    # ------------------------------------------------------------------
    def diagnostics(self, uri: str) -> List[Diagnostic]:
        state = self.docs.get(uri)
        if state is None:
            return []
        diags: List[Diagnostic] = list(state.ast.errors)
        table = self.symbols()

        # Duplicate keys (same section.key defined more than once,
        # possibly across files).
        for (section, key), defs in table.items():
            for dup in defs[1:]:
                if dup.uri != uri:
                    continue
                first = defs[0]
                diags.append(
                    Diagnostic(
                        dup.range,
                        f"duplicate key '{section}.{key}': first defined in "
                        f"{display_path(first.uri)}:"
                        f"{first.range.start_line + 1}",
                    )
                )

        for entry in state.ast.entries():
            value = entry.value
            # Type mismatch between annotation and literal value.
            if (
                entry.annotation
                and value is not None
                and value.kind not in ("ref", "error")
                and entry.annotation != value.kind
            ):
                diags.append(
                    Diagnostic(
                        value.range,
                        f"type mismatch: value is '{value.kind}' but "
                        f"'{entry.key}' is declared '{entry.annotation}'",
                    )
                )

            if value is None or value.kind != "ref":
                continue
            target_name = f"{value.ref_section}.{value.ref_key}"
            if (value.ref_section, value.ref_key) not in table:
                diags.append(
                    Diagnostic(
                        value.range,
                        f"unknown reference '${{{target_name}}}': "
                        f"no such key defined in the workspace",
                    )
                )
                continue
            # Type mismatch between annotation and referenced symbol.
            if entry.annotation:
                target_type = self.resolve_type(
                    table, value.ref_section or "", value.ref_key or ""
                )
                if target_type and target_type != entry.annotation:
                    diags.append(
                        Diagnostic(
                            value.range,
                            f"type mismatch: '${{{target_name}}}' resolves "
                            f"to '{target_type}' but '{entry.key}' is "
                            f"declared '{entry.annotation}'",
                        )
                    )
        return diags
