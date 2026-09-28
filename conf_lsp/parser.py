"""Parser for the simple ``.conf`` configuration language.

Grammar (line oriented)::

    file        := line*
    line        := section | import_directive | entry | doc_comment | comment | blank
    section     := "[" dotted-name "]" ["#" comment]
    import_dir  := 'import' ws quoted-path ["#" comment]
    entry       := key ":" (type-annotation | value) ["#" comment]
    value       := string | number | boolean | reference
    reference   := dotted-name
    string      := '"' char* '"'        # supports escapes and ${ref} interpolation
    number      := "-"? digits ("." digits)?
    boolean     := "true" | "false"

The parser is permissive: when a line is malformed a :class:`ParseError` is
recorded (with an exact source range) and parsing continues on the next
line, so that a single mistake does not hide every later symbol.
"""

from __future__ import annotations

import re

from .ast_nodes import (
    Document,
    Entry,
    ImportDirective,
    Interpolation,
    ParseError,
    Range,
    Reference,
    Section,
    Value,
    VALUE_TYPES,
)

KEY_RE = re.compile(r"[^\W\d][\w-]*", re.UNICODE)
SEGMENT_RE = re.compile(r"[^\W\d][\w-]*", re.UNICODE)
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"',
            "0": "\0", "$": "$", "/": "/", "b": "\b", "f": "\f"}


def parse_document(uri: str, text: str) -> Document:
    return _Parser(uri, text).parse()


class _Parser:
    def __init__(self, uri: str, text: str):
        self.doc = Document(uri=uri, text=text)
        self.text = text
        self.n = len(text)
        self.i = 0
        self.current_section = ""
        self.pending_docs: list[str] = []

    # -- low level helpers ---------------------------------------------------

    def _line_end(self, start: int) -> int:
        j = self.text.find("\n", start)
        return self.n if j == -1 else j

    def _skip_inline_ws(self) -> None:
        while self.i < self.n and self.text[self.i] in " \t":
            self.i += 1

    def _skip_rest_of_line(self) -> None:
        end = self._line_end(self.i)
        self.i = end
        if self.i < self.n:
            self.i += 1  # consume newline

    def _error(self, message: str, start: int, end: int | None = None) -> None:
        if end is None:
            end = min(start + 1, self.n)
        self.doc.errors.append(ParseError(message, Range(start, end)))

    # -- main loop -----------------------------------------------------------

    def parse(self) -> Document:
        while self.i < self.n:
            line_start = self.i
            self._skip_inline_ws()
            ch = self.text[self.i] if self.i < self.n else ""

            if ch == "\n":
                # Blank lines break doc-comment attachment.
                self.pending_docs = []
                self.i += 1
                continue
            if ch == "":
                break
            if ch == "#":
                self._parse_doc_comment()
                continue
            if ch == "[":
                self._parse_section()
                continue
            if self._match_import():
                self._parse_import()
                continue
            if KEY_RE.match(self.text, self.i):
                self._parse_entry()
                continue

            # Unrecognised text at column 0.
            end = self._line_end(self.i)
            self._error("syntax error: expected section, import or key",
                        self.i, max(self.i + 1, end))
            self.pending_docs = []
            self._skip_rest_of_line()
            _ = line_start

        return self.doc

    # -- comments ------------------------------------------------------------

    def _parse_doc_comment(self) -> None:
        start = self.i
        end = self._line_end(start)
        body = self.text[start + 1:end].strip()
        if body.startswith(" "):
            body = body[1:]
        self.pending_docs.append(body)
        self.i = end
        if self.i < self.n:
            self.i += 1

    def _skip_trailing_comment(self, line_end: int) -> bool:
        """After a value, only whitespace and a ``#`` comment may remain."""
        self._skip_inline_ws()
        if self.i >= line_end:
            return True
        if self.text[self.i] == "#":
            self.i = line_end
            return True
        self._error("syntax error: unexpected text after value",
                    self.i, line_end)
        self.i = line_end
        return False

    # -- sections / imports --------------------------------------------------

    def _parse_section(self) -> None:
        start = self.i
        line_end = self._line_end(start)
        self.i += 1  # '['
        name_start = self.i
        close = self.text.find("]", self.i, line_end)
        if close == -1:
            self._error("syntax error: unterminated section header, expected ']'",
                        start, line_end)
            self.pending_docs = []
            self.i = line_end
            return
        name = self.text[name_start:close].strip(" \t")
        leading = self.text[name_start:close]
        name_offset = name_start + (len(leading) - len(leading.lstrip(" \t")))
        # Section names are permissive: any non-empty, non-whitespace token
        # (dotted segments are allowed); keys themselves stay identifiers.
        segments = name.split(".") if name else []
        valid_name = bool(segments) and all(
            seg and not any(c.isspace() or c in "[]" for c in seg)
            for seg in segments
        )
        if not valid_name:
            self._error("syntax error: invalid section name %r" % name,
                        name_start, close)
            self.pending_docs = []
            self.i = close + 1
            return
        section = Section(
            name=name,
            header_range=Range(start, close + 1),
            name_range=Range(name_offset, name_offset + len(name)),
        )
        self.doc.sections.append(section)
        self.current_section = name
        self.i = close + 1
        self._skip_trailing_comment(line_end)
        self.pending_docs = []

    def _match_import(self) -> bool:
        if self.text.startswith("import", self.i):
            after = self.i + len("import")
            if after < self.n and self.text[after] in " \t":
                return True
        return False

    def _parse_import(self) -> None:
        start = self.i
        line_end = self._line_end(start)
        self.i += len("import")
        self._skip_inline_ws()
        if self.i >= line_end or self.text[self.i] != '"':
            self._error("syntax error: import expects a quoted path",
                        start, line_end)
            self.pending_docs = []
            self.i = line_end
            return
        value = self._parse_string(line_end)
        if value is None:
            self.pending_docs = []
            self.i = line_end
            return
        # Interpolation inside import paths is rejected.
        has_interpolation = any(
            isinstance(part, Interpolation) for part in value.string_parts)
        if has_interpolation:
            for part in value.string_parts:
                if isinstance(part, Interpolation):
                    ref = part.reference
                    self._error(
                        "syntax error: import paths may not contain references",
                        ref.range.start, ref.range.end)
            self.i = value.range.end
            self._skip_trailing_comment(line_end)
            self.pending_docs = []
            return
        raw_path = value.raw
        stem = raw_path[:-len(".conf")] if raw_path.endswith(".conf") else raw_path
        if not stem:
            self._error("syntax error: empty import path",
                        value.range.start, value.range.end)
        directive = ImportDirective(
            stem=stem,
            path_range=Range(value.range.start + 1, value.range.end - 1),
            full_range=Range(start, value.range.end),
        )
        self.doc.imports.append(directive)
        self.i = value.range.end
        self._skip_trailing_comment(line_end)
        self.pending_docs = []

    # -- entries -------------------------------------------------------------

    def _parse_entry(self) -> None:
        line_end = self._line_end(self.i)
        key_match = KEY_RE.match(self.text, self.i)
        key = key_match.group(0)
        key_range = Range(self.i, self.i + len(key))
        self.i += len(key)
        self._skip_inline_ws()
        if self.i >= line_end or self.text[self.i] != ":":
            bad = self.i if self.i < line_end else line_end
            self._error("syntax error: expected ':' after key %r" % key,
                        bad, min(bad + 1, line_end) if bad < line_end else bad)
            self.pending_docs = []
            self.i = line_end
            return
        self.i += 1  # ':'
        self._skip_inline_ws()

        if self.i >= line_end or self.text[self.i] == "#":
            self._error("syntax error: key %r requires a value" % key,
                        self.i, self.i)
            self.pending_docs = []
            self.i = line_end
            return

        # Optional type annotation: ``key: int`` or ``key: int 8080``.
        declared_type = None
        annotation_range = None
        type_match = KEY_RE.match(self.text, self.i)
        if type_match and type_match.group(0) in VALUE_TYPES:
            token = type_match.group(0)
            after = self.i + len(token)
            probe = after
            while probe < line_end and self.text[probe] in " \t":
                probe += 1
            if probe >= line_end or self.text[probe] == "#":
                # Declaration without a value.
                doc = "\n".join(self.pending_docs) if self.pending_docs else None
                self.doc.entries.append(Entry(
                    key=key, key_range=key_range, section=self.current_section,
                    value=None, declared_type=token,
                    annotation_range=Range(self.i, after),
                    doc_comment=doc,
                    full_range=Range(key_range.start, after)))
                self.pending_docs = []
                self.i = after
                self._skip_trailing_comment(line_end)
                return
            # ``key: <type> <value>`` — keep the annotation and parse value.
            declared_type = token
            annotation_range = Range(self.i, after)
            self.i = probe

        value = self._parse_value(line_end)
        doc = "\n".join(self.pending_docs) if self.pending_docs else None

        if value is None:
            self.pending_docs = []
            self.i = line_end
            return

        entry = Entry(
            key=key,
            key_range=key_range,
            section=self.current_section,
            value=value,
            declared_type=declared_type,
            annotation_range=annotation_range,
            doc_comment=doc,
            full_range=Range(key_range.start, value.range.end),
        )
        self.doc.entries.append(entry)
        self.pending_docs = []
        if self.i < line_end:
            self._skip_trailing_comment(line_end)

    def _parse_value(self, line_end: int) -> Value | None:
        start = self.i
        ch = self.text[self.i]

        if ch == '"':
            return self._parse_string(line_end)

        if ch.isdigit() or (ch == "-" and self.i + 1 < line_end
                            and self.text[self.i + 1].isdigit()):
            token = self._scan_bare(line_end, extra="-.")
            if NUMBER_RE.fullmatch(token) and not token.endswith("."):
                kind = "float" if "." in token else "int"
                return Value(kind, token, Range(start, start + len(token)), ())
            self._error("syntax error: malformed number %r" % token,
                        start, start + len(token))
            self.i = start + len(token)
            return None

        if ch == "-":
            self._error("syntax error: expected a value", self.i,
                        min(self.i + 1, line_end))
            return None

        if ch == "t" or ch == "f" or KEY_RE.match(self.text, self.i):
            token = self._scan_bare(line_end, extra=".")
            end = start + len(token)
            if ".." in token or token.endswith(".") or token.startswith("."):
                self._error("syntax error: malformed reference %r" % token,
                            start, end)
                self.i = end
                return None
            # Exact bare true/false (up to a comment) are booleans.
            if token in ("true", "false"):
                if not self._only_comment_remains(end, line_end):
                    self._error(
                        "syntax error: unexpected text after boolean %r" % token,
                        end, line_end)
                    self.i = line_end
                    return None
                return Value("bool", token, Range(start, end), ())
            ref = Reference(
                path=tuple(token.split(".")),
                range=Range(start, end),
                path_range=Range(start, end),
            )
            return Value("ref", token, Range(start, end), (), reference=ref)

        self._error("syntax error: expected a value", self.i,
                    min(self.i + 1, line_end))
        return None

    def _scan_bare(self, line_end: int, extra: str = "") -> str:
        start = self.i
        while self.i < line_end:
            ch = self.text[self.i]
            if ch.isalnum() or ch == "_" or ch == "-" or ch in extra:
                self.i += 1
            else:
                break
        return self.text[start:self.i]

    def _only_comment_remains(self, pos: int, line_end: int) -> bool:
        while pos < line_end and self.text[pos] in " \t":
            pos += 1
        return pos >= line_end or self.text[pos] == "#"

    # -- strings -------------------------------------------------------------

    def _parse_string(self, line_end: int) -> Value | None:
        start = self.i
        assert self.text[start] == '"'
        i = start + 1
        parts: list = []
        buf: list[str] = []
        references: list[Reference] = []
        closed = False

        def flush():
            if buf:
                parts.append("".join(buf))
                buf.clear()

        while i < line_end:
            ch = self.text[i]
            if ch == '"':
                closed = True
                i += 1
                break
            if ch == "\\":
                if i + 1 >= line_end:
                    self._error("syntax error: unterminated escape sequence",
                                i, line_end)
                    self.i = line_end
                    return None
                esc = self.text[i + 1]
                if esc not in _ESCAPES:
                    self._error("syntax error: invalid escape sequence '\\%s'" % esc,
                                i, i + 2)
                    buf.append(esc)
                    i += 2
                    continue
                buf.append(_ESCAPES[esc])
                i += 2
                continue
            if ch == "$" and i + 1 < line_end and self.text[i + 1] == "{":
                close = self.text.find("}", i + 2, line_end)
                if close == -1:
                    self._error(
                        "syntax error: unterminated interpolation, expected '}'",
                        i, line_end)
                    self.i = line_end
                    return None
                inner = self.text[i + 2:close].strip()
                segments = inner.split(".") if inner else []
                if not segments or not all(SEGMENT_RE.fullmatch(seg) for seg in segments):
                    self._error("syntax error: invalid reference %r" % inner,
                                i + 2, close)
                    self.i = line_end
                    return None
                lead = self.text[i + 2:close]
                offset = i + 2 + (len(lead) - len(lead.lstrip()))
                ref = Reference(
                    path=tuple(inner.split(".")),
                    range=Range(i, close + 1),
                    path_range=Range(offset, offset + len(inner)),
                    interpolated=True,
                )
                references.append(ref)
                flush()
                parts.append(Interpolation(reference=ref))
                i = close + 1
                continue
            buf.append(ch)
            i += 1

        if not closed:
            self._error("syntax error: unterminated string, expected '\"'",
                        start, line_end)
            self.i = line_end
            return None

        flush()
        self.i = i
        return Value(
            "str",
            self.text[start + 1:i - 1],
            Range(start, i),
            tuple(parts),
        )
