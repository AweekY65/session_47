"""Tests for parsing and semantic analysis (no JSON-RPC in this module).

These tests exercise the :mod:`conf_lsp.parser` and :class:`~conf_lsp.workspace.Workspace`
directly with in-memory documents; no process is spawned.
"""

from __future__ import annotations

import unittest

from conf_lsp.ast_nodes import Interpolation
from conf_lsp.parser import parse_document
from conf_lsp.positions import LineIndex
from conf_lsp.workspace import (
    DUPLICATE_KEY,
    DUPLICATE_SECTION,
    MISSING_IMPORT,
    SYNTAX_ERROR,
    TYPE_MISMATCH,
    UNKNOWN_REFERENCE,
    Workspace,
)


def make_workspace(files: dict[str, str]) -> Workspace:
    ws = Workspace()
    for name, text in files.items():
        ws.set_document("file:///workspace/" + name, text)
    return ws


def uri(name: str) -> str:
    return "file:///workspace/" + name


class ParserTests(unittest.TestCase):
    def test_parses_sections_entries_and_literals(self):
        doc = parse_document(uri("a.conf"), """
[server]
host: "localhost"
port: 8080
ratio: 1.5
enabled: true
disabled: false
ref: server.port
""")
        self.assertEqual([s.name for s in doc.sections], ["server"])
        self.assertEqual(len(doc.entries), 6)
        kinds = [e.value.kind for e in doc.entries]
        self.assertEqual(kinds, ["str", "int", "float", "bool", "bool", "ref"])
        self.assertEqual([e.section for e in doc.entries], ["server"] * 6)
        self.assertEqual(doc.errors, [])

    def test_syntax_errors_with_positions(self):
        doc = parse_document(uri("a.conf"), "[server\nport 8080\n")
        self.assertTrue(doc.errors)
        first = doc.errors[0]
        pos = LineIndex.create(doc.text).position(first.range.start)
        self.assertEqual(pos, {"line": 0, "character": 0})
        # The bad entry lives on line 1.
        second = doc.errors[1]
        pos2 = LineIndex.create(doc.text).position(second.range.start)
        self.assertEqual(pos2["line"], 1)

    def test_duplicate_key_detected(self):
        doc = parse_document(uri("a.conf"), "x: 1\nx: 2\n")
        ws = make_workspace({"a.conf": "x: 1\nx: 2\n"})
        codes = [d.code for d in ws.diagnostics(uri("a.conf"))]
        self.assertIn(DUPLICATE_KEY, codes)
        # Parser itself stays neutral: both entries are present for recovery.
        self.assertEqual(len(doc.entries), 2)

    def test_same_key_in_different_sections_is_fine(self):
        text = "[a]\nx: 1\n[b]\nx: 2\n"
        ws = make_workspace({"a.conf": text})
        self.assertEqual(
            [d for d in ws.diagnostics(uri("a.conf"))
             if d.code == DUPLICATE_KEY], [])

    def test_float_numbers(self):
        doc = parse_document(uri("a.conf"),
                             "a: 3.14\nb: -0.5\nc: 42\nd: -1\n")
        self.assertEqual([e.value.kind for e in doc.entries],
                         ["float", "float", "int", "int"])
        self.assertEqual(doc.errors, [])

    def test_string_escapes(self):
        doc = parse_document(uri("a.conf"),
                             r'x: "a\nb\tc\"d\\e"' + "\n")
        self.assertEqual(doc.errors, [])
        # The raw text keeps escapes; ensure the range covers the whole token.
        entry = doc.entries[0]
        self.assertEqual(doc.text[entry.value.range.start], '"')
        self.assertEqual(doc.text[entry.value.range.end - 1], '"')

    def test_bare_reference_and_doc_comment(self):
        text = "# The port\nport: 8080\n"
        doc = parse_document(uri("a.conf"), text)
        entry = doc.entries[0]
        self.assertEqual(entry.value.kind, "int")
        self.assertEqual(entry.doc_comment, "The port")

    def test_interpolation_references_have_ranges(self):
        text = 'url: "http://${host}:${port}"\n'
        doc = parse_document(uri("a.conf"), text)
        entry = doc.entries[0]
        interpolations = [p for p in entry.value.string_parts
                          if isinstance(p, Interpolation)]
        self.assertEqual(len(interpolations), 2)
        for part in interpolations:
            ref = part.reference
            self.assertTrue(ref.interpolated)
            self.assertEqual(
                doc.text[ref.path_range.start:ref.path_range.end],
                ".".join(ref.path))
            # The wrapping range must include the ${} delimiters.
            self.assertEqual(doc.text[ref.range.start:ref.range.start + 2], "${")
            self.assertEqual(doc.text[ref.range.end - 1], "}")

    def test_positions_are_utf16_units(self):
        text = '[🙂]\nx: 1\n'
        doc = parse_document(uri("a.conf"), text)
        index = LineIndex.create(text)
        section = doc.sections[0]
        start = index.position(section.name_range.start)
        # "[" occupies column 0, so the emoji starts at column 1 — and a
        # position after the emoji skips its two UTF-16 code units.
        self.assertEqual(start, {"line": 0, "character": 1})
        after = index.position(section.name_range.end)
        self.assertEqual(after, {"line": 0, "character": 3})
        # Round tripping returns the same offset.
        self.assertEqual(index.offset(start), section.name_range.start)

    def test_import_directive(self):
        text = 'import "other.conf"\nx: 1\n'
        doc = parse_document(uri("a.conf"), text)
        self.assertEqual(len(doc.imports), 1)
        self.assertEqual(doc.imports[0].stem, "other")
        self.assertEqual(doc.errors, [])

    def test_import_interpolation_rejected(self):
        text = 'import "${name}.conf"\n'
        doc = parse_document(uri("a.conf"), text)
        self.assertTrue(
            any(e.message.startswith("syntax error") for e in doc.errors))
        # Interpolation in an import never registers a (semantic) import.
        self.assertEqual(doc.imports, [])


class DiagnosticsTests(unittest.TestCase):
    def _codes(self, ws, name="a.conf"):
        return [d.code for d in ws.diagnostics(uri(name))]

    def test_cross_file_reference_resolves(self):
        ws = make_workspace({
            "a.conf": 'import "b.conf"\nx: b.name\n',
            "b.conf": 'name: "server"\n',
        })
        self.assertNotIn(UNKNOWN_REFERENCE, self._codes(ws))

    def test_duplicate_key_and_section_diagnostics(self):
        ws = make_workspace({
            "a.conf": "[s]\nx: 1\n[s]\nx: 2\nx: 3\n",
        })
        codes = self._codes(ws)
        self.assertIn(DUPLICATE_KEY, codes)
        self.assertIn(DUPLICATE_SECTION, codes)

    def test_inferred_type_follows_references(self):
        ws = make_workspace({
            "a.conf": ("port: int 8080\n"
                       "alias: int port\n"
                       "wrong: str port\n"),
        })
        mismatches = [d for d in ws.diagnostics(uri("a.conf"))
                      if d.code == TYPE_MISMATCH]
        self.assertEqual(len(mismatches), 1)
        # The mismatch diagnostic range sits on the "wrong" entry's value.
        text = ws.get_document(uri("a.conf")).text
        span = text[mismatches[0].start:mismatches[0].end]
        self.assertEqual(span, "port")

    def test_missing_import_warns(self):
        ws = make_workspace({"a.conf": 'import "ghost.conf"\n'})
        diags = ws.diagnostics(uri("a.conf"))
        missing = [d for d in diags if d.code == MISSING_IMPORT]
        self.assertEqual(len(missing), 1)
        # Missing imports are warnings, not errors.
        self.assertEqual(missing[0].severity, 2)

    def test_root_key_single_segment_resolves(self):
        ws = make_workspace({"a.conf": "name: \"x\"\nalias: name\n"})
        self.assertNotIn(UNKNOWN_REFERENCE, self._codes(ws))

    def test_same_section_single_segment_resolves(self):
        ws = make_workspace({
            "a.conf": "[s]\nname: \"x\"\nalias: name\n",
        })
        self.assertNotIn(UNKNOWN_REFERENCE, self._codes(ws))

    def test_type_mismatch_variants(self):
        ws = make_workspace({
            "a.conf": ("a: int \"hello\"\n"
                       "b: bool 3\n"
                       "c: float true\n"
                       "d: str 9\n"),
        })
        mismatches = [d for d in ws.diagnostics(uri("a.conf"))
                      if d.code == TYPE_MISMATCH]
        self.assertEqual(len(mismatches), 4)

    def test_unknown_reference_range_points_at_path(self):
        text = 'x: missing.target\n'
        ws = make_workspace({"a.conf": text})
        unknown = [d for d in ws.diagnostics(uri("a.conf"))
                   if d.code == UNKNOWN_REFERENCE]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(
            text[unknown[0].start:unknown[0].end], "missing.target")

    def test_unresolved_reference_is_not_also_type_mismatch(self):
        ws = make_workspace({"a.conf": "x: int ghost.ref\n"})
        codes = self._codes(ws)
        self.assertIn(UNKNOWN_REFERENCE, codes)
        self.assertNotIn(TYPE_MISMATCH, codes)

    def test_diagnostic_codes(self):
        ws = make_workspace({
            "a.conf": "[s\nx: int \"bad\"\ny: ghost\nz: 1\nz: 2\n",
        })
        codes = set(self._codes(ws))
        self.assertIn(SYNTAX_ERROR, codes)
        self.assertIn(TYPE_MISMATCH, codes)
        self.assertIn(UNKNOWN_REFERENCE, codes)
        self.assertIn(DUPLICATE_KEY, codes)


class HoverDefinitionRefsTests(unittest.TestCase):
    def test_hover_entry_declaration(self):
        text = "# The host name\nhost: \"a\"\n"
        ws = make_workspace({"a.conf": text})
        # Point at the declaration key (second line), not the same word
        # appearing inside the doc comment.
        markup = ws.hover(uri("a.conf"), text.index("host", text.index("\n")))
        self.assertIsNotNone(markup)
        self.assertIn("host", markup)
        self.assertIn("str", markup)
        self.assertIn("a.conf", markup)
        self.assertIn("The host name", markup)

    def test_hover_unknown_symbol_is_safe(self):
        text = "x: ghost\n"
        ws = make_workspace({"a.conf": text})
        # Hovering the reference must not raise and must say "unknown".
        markup = ws.hover(uri("a.conf"), text.index("ghost") + 1)
        self.assertIsNotNone(markup)
        self.assertIn("unknown", markup.lower())

    def test_goto_definition_cross_file(self):
        ws = make_workspace({
            "a.conf": 'import "b.conf"\nx: b.name\n',
            "b.conf": "name: \"b\"\n",
        })
        text = ws.get_document(uri("a.conf")).text
        found = ws.definition(uri("a.conf"), text.index("b.name") + 3)
        self.assertIsNotNone(found)
        target_uri, target_range = found
        self.assertEqual(target_uri, uri("b.conf"))
        target_doc = ws.get_document(uri("b.conf"))
        self.assertEqual(
            target_doc.text[target_range.start:target_range.end], "name")

    def test_find_references_across_files(self):
        ws = make_workspace({
            "a.conf": 'import "b.conf"\nx: b.name\ny: b.name\n',
            "b.conf": "name: \"b\"\n",
        })
        refs = ws.find_references(uri("b.conf"), 0)
        self.assertEqual(len(refs), 2)
        self.assertTrue(all(r[0] == uri("a.conf") for r in refs))

    def test_find_references_excluding_declaration(self):
        ws = make_workspace({"a.conf": "x: 1\ny: x\nz: x\n"})
        refs = ws.find_references(uri("a.conf"), 0)
        doc = ws.get_document(uri("a.conf"))
        for ref_uri, ref_range in refs:
            span = doc.text[ref_range.start:ref_range.end]
            self.assertEqual(span, "x")
        # Two references, and the declaration "x:" itself is not among them.
        self.assertEqual(len(refs), 2)

    def test_incremental_change_removes_and_adds_errors(self):
        ws = Workspace()
        ws.set_document(uri("a.conf"), "x: 1\n")
        self.assertEqual(ws.diagnostics(uri("a.conf")), [])
        ws.set_document(uri("a.conf"), "x: int \"bad\"\n")
        self.assertIn(TYPE_MISMATCH,
                      [d.code for d in ws.diagnostics(uri("a.conf"))])
        # Fix the document in place; no restart, and the error must vanish.
        ws.set_document(uri("a.conf"), "x: int 2\n")
        self.assertEqual(ws.diagnostics(uri("a.conf")), [])


if __name__ == "__main__":
    unittest.main()
