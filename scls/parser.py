"""Lexer + recursive-descent parser for the Simple Configuration Language.

Grammar (line oriented)::

    document     := (section | entry | comment | blank)*
    section      := '[' IDENT ']'
    entry        := IDENT (':' TYPE)? '=' value
    value        := STRING | NUMBER | BOOL | reference
    reference    := '${' IDENT '.' IDENT '}'
    comment      := ('#' | ';') <rest of line>

The parser is error recovering: a malformed line produces a
:class:`Diagnostic` with an exact 0-based (line, column) range and parsing
continues on the next line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

TYPES = ("string", "number", "bool")
LITERAL_KIND = {"string": "string", "number": "number", "bool": "bool"}


@dataclass
class Range:
    """0-based half-open range, LSP style."""

    start_line: int
    start_col: int
    end_line: int
    end_col: int

    def contains(self, line: int, col: int) -> bool:
        if line < self.start_line or line > self.end_line:
            return False
        if line == self.start_line and col < self.start_col:
            return False
        if line == self.end_line and col > self.end_col:
            return False
        return True


@dataclass
class Diagnostic:
    range: Range
    message: str
    severity: int = 1  # 1 = error, 2 = warning
    source: str = "scls"


@dataclass
class Value:
    kind: str  # 'string' | 'number' | 'bool' | 'ref' | 'error'
    range: Range
    raw: str = ""
    data: object = None
    ref_section: Optional[str] = None
    ref_key: Optional[str] = None
    ref_section_range: Optional[Range] = None
    ref_key_range: Optional[Range] = None


@dataclass
class Entry:
    key: str
    key_range: Range
    section: str
    annotation: Optional[str] = None
    annotation_range: Optional[Range] = None
    value: Optional[Value] = None
    doc: Optional[str] = None


@dataclass
class Section:
    name: str
    name_range: Range
    entries: List[Entry] = field(default_factory=list)


@dataclass
class Document:
    sections: List[Section] = field(default_factory=list)
    errors: List[Diagnostic] = field(default_factory=list)

    def entries(self):
        for section in self.sections:
            yield from section.entries


_IDENT_RE = re.compile(r"[A-Za-z_][\w-]*")
_BARE_WORD_RE = re.compile(r"[A-Za-z_][\w-]*")
_SECTION_RE = re.compile(r"^\s*\[\s*([A-Za-z_][\w-]*)\s*\]")
_REF_RE = re.compile(
    r"\$\{\s*([A-Za-z_][\w-]*)\s*\.\s*([A-Za-z_][\w-]*)\s*\}"
)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_STRING_ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _is_comment(text: str, i: int) -> bool:
    j = i
    while j < len(text) and text[j] in " \t":
        j += 1
    return j >= len(text) or text[j] == "#" or text[j] == ";"


def _error_at(line: int, col: int, end_col: int, message: str) -> Diagnostic:
    return Diagnostic(Range(line, col, line, end_col), message)


def _parse_string_value(
    text: str, line: int, pos: int
) -> Tuple[Value, int, List[Diagnostic]]:
    """Parse a double-quoted string starting at *pos* (the opening quote)."""
    i = pos + 1
    chars: List[str] = []
    errors: List[Diagnostic] = []
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            chars.append(_STRING_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            raw = text[pos : i + 1]
            value = Value(
                "string", Range(line, pos, line, i + 1), raw, "".join(chars)
            )
            return value, i + 1, errors
        chars.append(ch)
        i += 1
    errors.append(
        _error_at(line, pos, len(text), "unterminated string literal")
    )
    return (
        Value("error", Range(line, pos, line, len(text)), text[pos:]),
        len(text),
        errors,
    )


def _parse_value(
    text: str, line: int, pos: int
) -> Tuple[Optional[Value], int, List[Diagnostic]]:
    """Parse one value starting at *pos* (first non-space after '=')."""
    if pos >= len(text):
        return None, pos, [
            _error_at(
                line, pos, pos, "expected a value (string, number, bool or "
                "'${section.key}')"
            )
        ]
    ch = text[pos]
    if ch == '"':
        return _parse_string_value(text, line, pos)

    if ch == "$":
        match = _REF_RE.match(text, pos)
        if match:
            value = Value(
                "ref",
                Range(line, pos, line, match.end()),
                match.group(0),
                None,
                match.group(1),
                match.group(2),
                Range(
                    line, match.start(1), line, match.end(1)
                ),
                Range(
                    line, match.start(2), line, match.end(2)
                ),
            )
            return value, match.end(), []
        return None, pos, [
            _error_at(
                line,
                pos,
                len(text),
                "malformed reference, expected '${section.key}'",
            )
        ]

    word_match = _BARE_WORD_RE.match(text, pos)
    if word_match:
        word = word_match.group(0)
        if word in ("true", "false"):
            rng = Range(line, pos, line, word_match.end())
            return Value("bool", rng, word, word == "true"), word_match.end(), []

    number_match = _NUMBER_RE.match(text, pos)
    if (
        number_match
        and number_match.start() == pos
        and (
            number_match.end() == len(text)
            or not (text[number_match.end()].isalnum()
                    or text[number_match.end()] == "_")
        )
    ):
        raw = number_match.group(0)
        number = float(raw) if "." in raw else int(raw)
        rng = Range(line, pos, line, number_match.end())
        return Value("number", rng, raw, number), number_match.end(), []

    return None, pos, [
        _error_at(
            line,
            pos,
            pos + 1,
            "expected a value (string, number, bool or '${section.key}')",
        )
    ]


def parse(source: str) -> Document:
    """Parse *source* into a :class:`Document`, never raising."""
    doc = Document()
    current: Optional[Section] = None
    pending_doc: Optional[str] = None

    for lineno, raw_line in enumerate(source.split("\n")):
        line = raw_line.rstrip("\r")
        stripped = line.strip()
        if not stripped:
            pending_doc = None
            continue
        if stripped.startswith("#") or stripped.startswith(";"):
            pending_doc = stripped.lstrip("#;").strip()
            continue

        # Section header.
        if stripped.startswith("["):
            match = _SECTION_RE.match(line)
            if not match:
                ident_match = _IDENT_RE.search(line)
                col = ident_match.start() if ident_match else line.index("[")
                end = ident_match.end() if ident_match else min(
                    col + 1, len(line)
                )
                doc.errors.append(
                    _error_at(
                        lineno, col, end,
                        "malformed section header, expected '[name]'",
                    )
                )
                current = None
                pending_doc = None
                continue
            section = Section(
                match.group(1),
                Range(
                    lineno, match.start(1), lineno, match.end(1)
                ),
            )
            doc.sections.append(section)
            current = section
            tail_col = match.end()
            while tail_col < len(line) and line[tail_col] in " \t":
                tail_col += 1
            if tail_col < len(line) and line[tail_col] not in "#;":
                doc.errors.append(
                    _error_at(
                        lineno, tail_col, len(line),
                        "unexpected characters after section header",
                    )
                )
            pending_doc = None
            continue

        # Entry: IDENT (':' type)? '=' value
        key_match = _IDENT_RE.match(line)
        if not key_match:
            first = len(line) - len(line.lstrip())
            doc.errors.append(
                _error_at(
                    lineno, first, len(line),
                    "expected a section header '[name]' or a "
                    "'key = value' assignment",
                )
            )
            pending_doc = None
            continue

        key = key_match.group(0)
        key_range = Range(
            lineno, key_match.start(), lineno, key_match.end()
        )
        pos = key_match.end()
        while pos < len(line) and line[pos] in " \t":
            pos += 1

        annotation: Optional[str] = None
        annotation_range: Optional[Range] = None
        if pos < len(line) and line[pos] == ":":
            colon = pos
            pos += 1
            while pos < len(line) and line[pos] in " \t":
                pos += 1
            type_match = _BARE_WORD_RE.match(line, pos)
            if type_match and type_match.group(0) in TYPES:
                annotation = type_match.group(0)
                annotation_range = Range(
                    lineno, type_match.start(), lineno, type_match.end()
                )
                pos = type_match.end()
                while pos < len(line) and line[pos] in " \t":
                    pos += 1
            else:
                doc.errors.append(
                    _error_at(
                        lineno, pos, type_match.end() if type_match
                        else min(pos + 1, len(line)),
                        "unknown type annotation: expected one of "
                        "'string', 'number', 'bool'",
                    )
                )
                # Recover by skipping to the '=' on this line.
                eq = line.find("=", pos)
                pos = eq if eq != -1 else len(line)
            _ = colon  # colon column only needed for error messages

        if pos >= len(line) or line[pos] != "=":
            doc.errors.append(
                _error_at(
                    lineno, pos, min(pos + 1, len(line)),
                    f"expected '=' after key '{key}'",
                )
            )
            pending_doc = None
            continue
        pos += 1
        while pos < len(line) and line[pos] in " \t":
            pos += 1

        value, end, errors = _parse_value(line, lineno, pos)
        doc.errors.extend(errors)

        # Anything left must be a comment.
        if end < len(line) and not _is_comment(line, end):
            doc.errors.append(
                _error_at(
                    lineno, end, len(line),
                    f"unexpected characters after value: "
                    f"'{line[end:].strip()}'",
                )
            )

        section_name = current.name if current is not None else ""
        if current is None:
            doc.errors.append(
                _error_at(
                    lineno, key_range.start_col, key_range.end_col,
                    f"key '{key}' is defined outside of any section",
                )
            )
        entry = Entry(
            key=key,
            key_range=key_range,
            section=section_name,
            annotation=annotation,
            annotation_range=annotation_range,
            value=value,
            doc=pending_doc,
        )
        if current is not None:
            current.entries.append(entry)
        else:
            # Keep the entry indexed under a synthetic section so later
            # analysis can still inspect it, but it has already been flagged.
            orphan = Section("", Range(lineno, 0, lineno, 0))
            orphan.entries.append(entry)
            doc.sections.append(orphan)
            current = None
        pending_doc = None

    return doc
