"""Position / range helpers.

The LSP protocol measures columns in **UTF-16 code units**. The parser and
the semantic analysis work with plain UTF-8 byte offsets into the source
text; :class:`LineIndex` translates between the two representations.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LineIndex:
    """Maps between zero-based (line, column) positions and absolute offsets.

    Columns handed out by :meth:`position` are counted in UTF-16 code units,
    as required by the Language Server Protocol.
    """

    text: str
    line_starts: tuple[int, ...]

    @classmethod
    def create(cls, text: str) -> "LineIndex":
        starts = [0]
        for i, ch in enumerate(text):
            if ch == "\n":
                starts.append(i + 1)
        return cls(text=text, line_starts=tuple(starts))

    # -- offset <-> position -------------------------------------------------

    def position(self, offset: int) -> dict:
        """Return an ``{line, character}`` LSP position for an offset."""
        text = self.text
        if offset < 0:
            offset = 0
        if offset > len(text):
            offset = len(text)
        # Binary search for the line that contains `offset`.
        lo, hi = 0, len(self.line_starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.line_starts[mid] <= offset:
                lo = mid
            else:
                hi = mid - 1
        line = lo
        start = self.line_starts[line]
        # Columns are measured in UTF-16 code units, so even though the
        # prefix is sliced by character offset its length must be encoded.
        character = utf16_len(self.text[start:offset])
        return {"line": line, "character": character}

    def offset(self, position: dict) -> int:
        """Return the absolute offset for an LSP position.

        Clamps to document boundaries. Positions inside a multi-byte
        character are rounded back to that character's start.
        """
        line = position.get("line", 0)
        character = position.get("character", 0)
        if line < 0:
            return 0
        if line >= len(self.line_starts):
            return len(self.text)
        start = self.line_starts[line]
        if line + 1 < len(self.line_starts):
            end = self.line_starts[line + 1] - 1  # exclude the '\n'
        else:
            end = len(self.text)
        line_text = self.text[start:end]
        off = _utf16_units_to_char_offset(line_text, character)
        return start + off

    def range_for(self, start: int, end: int) -> dict:
        return {"start": self.position(start), "end": self.position(end)}


def utf16_len(text: str) -> int:
    """Length of *text* measured in UTF-16 code units."""
    return sum(2 if ord(ch) > 0xFFFF else 1 for ch in text)


def _utf16_units_to_char_offset(text: str, units: int) -> int:
    """Convert a UTF-16 column count into an offset inside *text*.

    Clamps to the end of the line; if the column falls in the middle of a
    surrogate pair the start of that character is returned.
    """
    if units <= 0:
        return 0
    count = 0
    for i, ch in enumerate(text):
        count += 2 if ord(ch) > 0xFFFF else 1
        if count >= units:
            return i + 1
    return len(text)
