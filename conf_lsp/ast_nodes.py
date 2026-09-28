"""Syntax tree for the ``.conf`` configuration language.

Every node keeps absolute source offsets so that ranges can be produced
lazily with a :class:`~conf_lsp.positions.LineIndex`.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Range:
    start: int
    end: int

    def to_lsp(self, index) -> dict:
        return {"start": index.position(self.start), "end": index.position(self.end)}


# --- Values -----------------------------------------------------------------

VALUE_TYPES = ("str", "int", "float", "bool")


@dataclass(frozen=True)
class Reference:
    """A reference expression: ``a.b`` plain or ``${a.b}`` interpolated."""

    path: tuple[str, ...]
    range: Range
    # Range of just the path text (same as ``range`` for bare references,
    # excludes the ``${`` / ``}`` wrapping for interpolated ones).
    path_range: Range
    interpolated: bool = False


@dataclass(frozen=True)
class Interpolation:
    """One ``${...}`` hole inside a string literal."""

    reference: Reference


@dataclass(frozen=True)
class Value:
    kind: str                       # "str" | "int" | "float" | "bool" | "ref"
    raw: str
    range: Range
    # Literals only:
    string_parts: tuple = ()        # alternating str / Interpolation
    # References only:
    reference: Reference | None = None


@dataclass(frozen=True)
class Entry:
    key: str
    key_range: Range
    section: str                    # dotted section path, "" for top level
    value: Value | None             # None for type annotations
    declared_type: str | None       # "str" | "int" | "float" | "bool"
    annotation_range: Range | None
    doc_comment: str | None
    full_range: Range


@dataclass(frozen=True)
class Section:
    name: str                       # dotted path
    header_range: Range
    name_range: Range


@dataclass(frozen=True)
class ImportDirective:
    stem: str                       # e.g. "other" for ``import "other.conf"``
    path_range: Range               # range of the quoted path text
    full_range: Range
    resolved: bool = False          # filled in by analysis (see workspace)


@dataclass(frozen=True)
class ParseError:
    message: str
    range: Range


@dataclass
class Document:
    uri: str
    text: str
    sections: list[Section] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)
    imports: list[ImportDirective] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)

    @property
    def stem(self) -> str:
        from .workspace import uri_stem

        return uri_stem(self.uri)

    def entries_in(self, section: str) -> list[Entry]:
        return [e for e in self.entries if e.section == section]
