"""Unit tests for the parser and the in-memory analyzer."""

import unittest

from scls.analyzer import Workspace
from scls.parser import parse


class ParserTest(unittest.TestCase):
    def test_all_value_kinds(self):
        doc = parse(
            '[server]\n'
            'host = "localhost"\n'
            'port = 8080\n'
            'ratio = 0.5\n'
            'enabled = true\n'
            'mirror = ${server.host}\n'
        )
        section = doc.sections[0]
        self.assertEqual(section.name, "server")
        kinds = [
            (e.key, e.value.kind, e.value.data) for e in section.entries
        ]
        self.assertEqual(kinds, [
            ("host", "string", "localhost"),
            ("port", "number", 8080),
            ("ratio", "number", 0.5),
            ("enabled", "bool", True),
            ("mirror", "ref", None),
        ])
        ref = section.entries[4].value
        self.assertEqual((ref.ref_section, ref.ref_key),
                         ("server", "host"))

    def test_annotations_and_comments(self):
        doc = parse(
            '[s]\n'
            '# leading comment\n'
            'name: string = "x"  # trailing\n'
            '; semicolon comment\n'
            'count : number = 1\n'
        )
        entries = doc.sections[0].entries
        self.assertEqual(entries[0].annotation, "string")
        self.assertEqual(entries[0].doc, "leading comment")
        self.assertEqual(entries[1].annotation, "number")

    def test_positions_are_accurate(self):
        doc = parse('[s]\nk = 12\n')
        entry = doc.sections[0].entries[0]
        # 'k' at line 1 col 0, value '12' at line 1 col 4..6
        self.assertEqual((entry.key_range.start_line,
                          entry.key_range.start_col), (1, 0))
        self.assertEqual((entry.value.range.start_line,
                          entry.value.range.start_col), (1, 4))
        self.assertEqual(entry.value.range.end_col, 6)

    def test_reference_token_ranges(self):
        doc = parse('[s]\nk = ${sec.name}\n')
        ref = doc.sections[0].entries[0].value
        self.assertTrue(ref.ref_section_range.contains(1, 6))
        self.assertFalse(ref.ref_section_range.contains(1, 10))
        self.assertTrue(ref.ref_key_range.contains(1, 10))

    def test_syntax_errors_keep_parsing(self):
        doc = parse(
            '[s\n'
            'name = "unterminated\n'
            'oops 12\n'
            'good = 4\n'
        )
        messages = [d.message for d in doc.errors]
        self.assertTrue(any("section header" in m for m in messages),
                        messages)
        self.assertTrue(any("unterminated" in m for m in messages),
                        messages)
        self.assertTrue(any("expected '='" in m for m in messages),
                        messages)
        # Recovery: the good line still parsed (the unterminated-string
        # line also produced an entry, with an error value).
        good = [e for e in doc.entries() if e.key == "good"]
        self.assertEqual(len(good), 1)
        self.assertEqual(good[0].value.data, 4)
        # Error on the bad header is at the name token column.
        header_err = doc.errors[0]
        self.assertEqual((header_err.range.start_line,
                          header_err.range.start_col), (0, 1))

    def test_entry_outside_section(self):
        doc = parse('k = 1\n')
        self.assertTrue(
            any("outside of any section" in d.message for d in doc.errors)
        )

    def test_string_escapes(self):
        doc = parse('[s]\nk = "a\\"b\\\\\\n"\n')
        self.assertEqual(doc.sections[0].entries[0].value.data,
                         'a"b\\\n')

    def test_negative_number_boundary(self):
        doc = parse('[s]\nk = -12\n')
        self.assertEqual(doc.sections[0].entries[0].value.data, -12)


class AnalyzerTest(unittest.TestCase):
    def test_unknown_reference(self):
        ws = Workspace()
        ws.update("file:///a.scl", "[s]\nk = ${other.x}\n")
        messages = [d.message for d in ws.diagnostics("file:///a.scl")]
        self.assertTrue(any("unknown reference" in m for m in messages))

    def test_duplicate_key_single_file(self):
        ws = Workspace()
        ws.update("file:///a.scl", "[s]\nk = 1\nk = 2\n")
        messages = [d.message for d in ws.diagnostics("file:///a.scl")]
        self.assertTrue(any("duplicate key" in m for m in messages),
                        messages)

    def test_duplicate_key_across_files(self):
        ws = Workspace()
        ws.update("file:///a.scl", "[s]\nk = 1\n")
        ws.update("file:///b.scl", "[s]\nk = 2\n")
        messages_a = [d.message for d in ws.diagnostics("file:///a.scl")]
        messages_b = [d.message for d in ws.diagnostics("file:///b.scl")]
        self.assertFalse(any("duplicate" in m for m in messages_a))
        self.assertTrue(any("duplicate key" in m for m in messages_b))

    def test_type_mismatch_literal_and_reference(self):
        ws = Workspace()
        ws.update(
            "file:///a.scl",
            "[s]\n"
            "port: number = 80\n"
            "name: string = 1\n"
            "alias: string = ${s.port}\n"
        )
        messages = [d.message for d in ws.diagnostics("file:///a.scl")]
        self.assertEqual(
            sum(1 for m in messages if "type mismatch" in m), 2, messages
        )

    def test_type_follows_reference_chain(self):
        ws = Workspace()
        ws.update(
            "file:///a.scl",
            "[s]\n"
            "a = 5\n"
            "b = ${s.a}\n"
            "c: number = ${s.b}\n"
            "d: string = ${s.b}\n"
        )
        messages = [d.message for d in ws.diagnostics("file:///a.scl")]
        self.assertEqual(
            sum(1 for m in messages if "type mismatch" in m), 1, messages
        )

    def test_reference_cycle_is_safe(self):
        ws = Workspace()
        ws.update(
            "file:///a.scl",
            "[s]\na = ${s.b}\nb = ${s.a}\nc: number = ${s.a}\n"
        )
        # No crash, and the cycle must not be reported as a mismatch.
        messages = [d.message for d in ws.diagnostics("file:///a.scl")]
        self.assertFalse(any("type mismatch" in m for m in messages))

    def test_removing_document_clears_errors(self):
        ws = Workspace()
        ws.update("file:///a.scl", "[s]\nk = 1\nk = 2\n")
        self.assertTrue(ws.diagnostics("file:///a.scl"))
        ws.remove("file:///a.scl")
        self.assertEqual(ws.diagnostics("file:///a.scl"), [])

    def test_symbol_at(self):
        ws = Workspace()
        ws.update(
            "file:///a.scl",
            '[s]\nhost = "x"\n[t]\nk = ${s.host}\n'
        )
        # Position inside the referenced 'host' token (line 3).
        self.assertEqual(
            ws.symbol_at("file:///a.scl", 3, 11), ("s", "host")
        )
        # Position on a defining key.
        self.assertEqual(
            ws.symbol_at("file:///a.scl", 1, 1), ("s", "host")
        )
        self.assertIsNone(ws.symbol_at("file:///a.scl", 0, 0))


if __name__ == "__main__":
    unittest.main()
