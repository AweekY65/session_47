"""Semantic analysis: workspace index, diagnostics, navigation, completion.

Everything here is purely in-memory. A :class:`Workspace` holds parsed
documents (optionally seeded from a local directory) and answers every
semantic query the language server needs. No network access happens
anywhere in this module.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.request import pathname2url, url2pathname

from .ast_nodes import Entry, Interpolation
from .parser import parse_document

CONFIG_SUFFIX = ".conf"
COMPLETION_KEY = 7          # CompletionItemKind.Property
COMPLETION_REFERENCE = 18   # CompletionItemKind.Reference
COMPLETION_TYPE = 14        # CompletionItemKind.Keyword


# --- diagnostic codes -------------------------------------------------------

SYNTAX_ERROR = 1
DUPLICATE_KEY = 2
DUPLICATE_SECTION = 3
UNKNOWN_REFERENCE = 4
TYPE_MISMATCH = 5
MISSING_IMPORT = 6

_SEVERITY_ERROR = 1
_SEVERITY_WARNING = 2


@dataclass(frozen=True)
class Diagnostic:
    code: int
    message: str
    start: int
    end: int
    severity: int = _SEVERITY_ERROR


# --- URI helpers ------------------------------------------------------------

def path_to_uri(path: str) -> str:
    return "file://" + pathname2url(os.path.abspath(path))


def uri_to_path(uri: str) -> str:
    parsed = urlparse(uri)
    return url2pathname(parsed.path) if parsed.scheme == "file" else uri


def uri_basename(uri: str) -> str:
    return os.path.basename(urlparse(uri).path) if "://" in uri else os.path.basename(uri)


def uri_stem(uri: str) -> str:
    name = uri_basename(uri)
    return name[: -len(CONFIG_SUFFIX)] if name.endswith(CONFIG_SUFFIX) else name


def iter_references(entry: Entry):
    """Yield every :class:`Reference` appearing in *entry*'s value."""
    value = entry.value
    if value is None:
        return
    if value.kind == "ref" and value.reference is not None:
        yield value.reference
    for part in value.string_parts:
        if isinstance(part, Interpolation):
            yield part.reference


# --- workspace --------------------------------------------------------------

class Workspace:
    def __init__(self, root_path: str | None = None):
        self.root_path = root_path
        self.documents: dict[str, "object"] = {}
        self._reindex()

    # -- document management -------------------------------------------------

    def load_from_disk(self, root_path: str | None = None) -> None:
        """Scan a local directory tree for ``*.conf`` files and load them."""
        root = root_path or self.root_path
        if root is None:
            return
        self.root_path = root
        found: dict[str, str] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for filename in sorted(filenames):
                if filename.endswith(CONFIG_SUFFIX):
                    full = os.path.join(dirpath, filename)
                    with open(full, "r", encoding="utf-8", errors="replace") as fh:
                        found[path_to_uri(full)] = fh.read()
        self.documents = {uri: parse_document(uri, text)
                          for uri, text in found.items()}
        self._reindex()

    def set_document(self, uri: str, text: str) -> None:
        """Create or replace a document (used for open / change events)."""
        self.documents[uri] = parse_document(uri, text)
        self._reindex()

    def close_document(self, uri: str) -> None:
        """Revert to the on-disk version of *uri*, or drop it entirely."""
        disk_path = uri_to_path(uri)
        if self.root_path and os.path.isfile(disk_path):
            with open(disk_path, "r", encoding="utf-8", errors="replace") as fh:
                self.documents[uri] = parse_document(uri, fh.read())
        else:
            self.documents.pop(uri, None)
        self._reindex()

    def get_document(self, uri: str):
        return self.documents.get(uri)

    def has_document(self, uri: str) -> bool:
        return uri in self.documents

    # -- indexing -------------------------------------------------------------

    def _reindex(self) -> None:
        self.by_stem: dict[str, list[str]] = defaultdict(list)
        self._entry_index: dict[str, dict[str, list[Entry]]] = {}
        for uri in sorted(self.documents):
            self.by_stem[uri_stem(uri)].append(uri)
            sections: dict[str, list[Entry]] = defaultdict(list)
            for entry in self.documents[uri].entries:
                sections[entry.section].append(entry)
            self._entry_index[uri] = sections

    def _entries(self, uri: str, section: str) -> list[Entry]:
        return self._entry_index.get(uri, {}).get(section, [])

    def _find_local(self, uri: str, section: str, key: str) -> Entry | None:
        for entry in self._entries(uri, section):
            if entry.key == key:
                return entry
        return None

    def _reachable_stems(self, doc) -> set[str]:
        """Stems of every document transitively imported by *doc*."""
        seen: set[str] = set()
        pending = [doc]
        visited_docs = {doc.uri}
        while pending:
            current = pending.pop()
            for directive in current.imports:
                if directive.stem in seen:
                    continue
                uris = self.by_stem.get(directive.stem, [])
                if not uris:
                    continue
                seen.add(directive.stem)
                target_uri = uris[0]
                if target_uri not in visited_docs:
                    visited_docs.add(target_uri)
                    pending.append(self.documents[target_uri])
        return seen

    def _doc_for_stem(self, stem: str) -> str | None:
        uris = self.by_stem.get(stem)
        return uris[0] if uris else None

    # -- reference resolution ------------------------------------------------

    def resolve(self, doc, section: str, path: tuple[str, ...]):
        """Resolve *path* in the context of *doc* / *section*.

        Resolution order:

        1. Single segment: a key in the current section, else a top-level key.
        2. ``section.key`` referring to a local (possibly dotted) section.
        3. ``stem.key`` / ``stem.section.key`` referring to a transitively
           imported file.

        Returns ``(uri, entry)`` or ``None``.
        """
        if not path:
            return None

        if len(path) == 1:
            entry = self._find_local(doc.uri, section, path[0])
            if entry:
                return doc.uri, entry
            entry = self._find_local(doc.uri, "", path[0])
            if entry:
                return doc.uri, entry
            return None

        # Local section path (sections themselves may use dotted names).
        entry = self._find_local(doc.uri, ".".join(path[:-1]), path[-1])
        if entry:
            return doc.uri, entry

        stem = path[0]
        target_uri = self._doc_for_stem(stem)
        if target_uri and stem in self._reachable_stems(doc):
            if len(path) == 2:
                for candidate_section in (section, ""):
                    entry = self._find_local(target_uri, candidate_section, path[1])
                    if entry:
                        return target_uri, entry
            else:
                entry = self._find_local(
                    target_uri, ".".join(path[1:-1]), path[-1])
                if entry:
                    return target_uri, entry
        return None

    # -- types ----------------------------------------------------------------

    def effective_type(self, uri: str, entry: Entry, seen=None) -> str | None:
        """Infer the runtime type of an entry, following references."""
        if seen is None:
            seen = set()
        ident = (uri, entry.section, entry.key, id(entry))
        if ident in seen:
            return None
        seen.add(ident)

        if entry.value is None:
            return entry.declared_type
        value = entry.value
        if value.kind == "ref" and value.reference is not None:
            target = self.resolve(self.documents[uri], entry.section,
                                  value.reference.path)
            if target is None:
                return None
            return self.effective_type(target[0], target[1], seen)
        return value.kind  # literal: str / int / float / bool

    # -- diagnostics ----------------------------------------------------------

    def diagnostics(self, uri: str) -> list[Diagnostic]:
        doc = self.documents.get(uri)
        if doc is None:
            return []
        result: list[Diagnostic] = []

        for error in doc.errors:
            result.append(Diagnostic(
                SYNTAX_ERROR, error.message, error.range.start,
                error.range.end, _SEVERITY_ERROR))

        # Duplicate sections.
        seen_sections: set[str] = set()
        for section in doc.sections:
            if section.name in seen_sections:
                result.append(Diagnostic(
                    DUPLICATE_SECTION,
                    "duplicate section %r" % section.name,
                    section.name_range.start, section.name_range.end))
            seen_sections.add(section.name)

        # Duplicate keys within one section.
        for section_name, entries in self._entry_index.get(uri, {}).items():
            seen_keys: dict[str, Entry] = {}
            for entry in entries:
                if entry.key in seen_keys:
                    label = entry.key if not section_name else section_name + "." + entry.key
                    result.append(Diagnostic(
                        DUPLICATE_KEY, "duplicate key %r" % label,
                        entry.key_range.start, entry.key_range.end))
                else:
                    seen_keys[entry.key] = entry

        # Missing imports (warnings).
        for directive in doc.imports:
            if not self.by_stem.get(directive.stem):
                result.append(Diagnostic(
                    MISSING_IMPORT,
                    "cannot find imported file %r" % (directive.stem + CONFIG_SUFFIX),
                    directive.path_range.start, directive.path_range.end,
                    _SEVERITY_WARNING))

        # Reference and type checking.
        for entry in doc.entries:
            self._diagnose_entry(doc, entry, result)

        result.sort(key=lambda d: (d.start, d.code))
        return result

    def _diagnose_entry(self, doc, entry: Entry,
                        result: list[Diagnostic]) -> None:
        value = entry.value

        # Resolve every reference first; unresolved ones get their own
        # diagnostic and skip type checking.
        resolved_types: list[str | None] = []
        all_resolved = True
        for ref in iter_references(entry):
            target = self.resolve(doc, entry.section, ref.path)
            if target is None:
                all_resolved = False
                result.append(Diagnostic(
                    UNKNOWN_REFERENCE,
                    "unknown reference %r" % ".".join(ref.path),
                    ref.path_range.start, ref.path_range.end))
            else:
                resolved_types.append(
                    self.effective_type(target[0], target[1]))

        if not all_resolved:
            return

        if value is None:
            return

        # Explicit annotation must match the (inferred) value type.
        if entry.declared_type is not None:
            if value.kind == "ref" and value.reference is not None:
                inferred = resolved_types[0] if resolved_types else None
            else:
                inferred = value.kind
            if inferred is not None and inferred != entry.declared_type:
                rng = (value.reference.path_range
                       if value.kind == "ref" and value.reference else value.range)
                result.append(Diagnostic(
                    TYPE_MISMATCH,
                    "type mismatch: %s declared as %s" % (
                        inferred, entry.declared_type),
                    rng.start, rng.end))
            return

        # Unannotated reference entries still expose type conflicts with the
        # symbols they point at (declared annotations on the target side).
        if value.kind == "ref" and value.reference is not None:
            target = self.resolve(doc, entry.section, value.reference.path)
            if target is not None:
                target_uri, target_entry = target
                expected = target_entry.declared_type
                actual = self.effective_type(target_uri, target_entry)
                if expected and actual and expected != actual:
                    result.append(Diagnostic(
                        TYPE_MISMATCH,
                        "type mismatch: %s is used where %s was declared" % (
                            actual, expected),
                        value.reference.path_range.start,
                        value.reference.path_range.end))

    # -- position queries -----------------------------------------------------

    def entry_at(self, uri: str, offset: int) -> Entry | None:
        doc = self.documents.get(uri)
        if doc is None:
            return None
        for entry in doc.entries:
            if entry.key_range.start <= offset < entry.key_range.end:
                return entry
            if entry.annotation_range is not None and \
                    entry.annotation_range.start <= offset < entry.annotation_range.end:
                return entry
        return None

    def reference_at(self, uri: str, offset: int):
        """Return the reference whose *path text* contains *offset*."""
        doc = self.documents.get(uri)
        if doc is None:
            return None
        for entry in doc.entries:
            for ref in iter_references(entry):
                if ref.path_range.start <= offset < ref.path_range.end:
                    return ref
        return None

    def _entry_label(self, entry: Entry) -> str:
        return entry.key if not entry.section else entry.section + "." + entry.key

    def _entry_description(self, uri: str, entry: Entry) -> str:
        lines = ["**%s**" % self._entry_label(entry)]
        inferred = self.effective_type(uri, entry)
        shown = inferred or entry.declared_type or "unknown"
        lines.append("")
        lines.append("type: `%s`" % shown)
        lines.append("source: `%s`" % uri_basename(uri))
        if entry.declared_type:
            lines.append("declared: `%s`" % entry.declared_type)
        if entry.doc_comment:
            lines.append("")
            lines.append(entry.doc_comment)
        return "\n".join(lines)

    def hover(self, uri: str, offset: int) -> str | None:
        doc = self.documents.get(uri)
        if doc is None:
            return None
        entry = self.entry_at(uri, offset)
        if entry is not None:
            return self._entry_description(uri, entry)
        ref = self.reference_at(uri, offset)
        if ref is not None:
            target = self.resolve(doc, entry_section_at(doc, offset), ref.path)
            if target is None:
                # Unknown symbols must be reported safely, never crash.
                return "unknown reference `%s`" % ".".join(ref.path)
            return self._entry_description(target[0], target[1])
        return None

    def definition(self, uri: str, offset: int):
        doc = self.documents.get(uri)
        if doc is None:
            return None
        ref = self.reference_at(uri, offset)
        if ref is not None:
            section = entry_section_at(doc, offset)
            target = self.resolve(doc, section, ref.path)
            if target is None:
                return None
            target_uri, target_entry = target
            return target_uri, target_entry.key_range
        entry = self.entry_at(uri, offset)
        if entry is not None:
            return uri, entry.key_range
        return None

    def find_references(self, uri: str, offset: int) -> list:
        """All reference sites pointing at the symbol under *offset*.

        The declaration itself is never included. Results are sorted by
        (uri, offset).
        """
        doc = self.documents.get(uri)
        if doc is None:
            return []
        target = None
        ref = self.reference_at(uri, offset)
        if ref is not None:
            target = self.resolve(doc, entry_section_at(doc, offset), ref.path)
        if target is None:
            entry = self.entry_at(uri, offset)
            if entry is not None:
                target = (uri, entry)
        if target is None:
            return []
        target_uri, target_entry = target

        results = []
        for other_uri, other_doc in self.documents.items():
            for other_entry in other_doc.entries:
                for other_ref in iter_references(other_entry):
                    resolved = self.resolve(other_doc, other_entry.section,
                                            other_ref.path)
                    if resolved is None:
                        continue
                    res_uri, res_entry = resolved
                    if res_uri == target_uri and res_entry is target_entry:
                        results.append((other_uri, other_ref.path_range))
        results.sort(key=lambda item: (item[0], item[1].start))
        return results

    # -- completion -----------------------------------------------------------

    def completion(self, uri: str, offset: int) -> list[dict]:
        doc = self.documents.get(uri)
        if doc is None:
            return []
        # Completion is typically requested while a line is still being
        # typed: determine the section from the line containing the cursor
        # (the cursor may sit in the middle of a valid entry's key).
        line_start = doc.text.rfind("\n", 0, offset) + 1
        line_end = doc.text.find("\n", offset)
        if line_end == -1:
            line_end = len(doc.text)
        line_prefix = doc.text[line_start:offset]
        section = entry_section_at(doc, line_start)

        # ``${interpolation|`` inside a string.
        dollar = line_prefix.rfind("${")
        if dollar != -1 and line_prefix.find("}", dollar + 2) == -1:
            word = line_prefix[dollar + 2:]
            return self._reference_completions(doc, section, word)

        colon = line_prefix.find(":")
        if colon == -1:
            # Completing a key name: the word being typed is everything from
            # the start of the line (after indentation) up to the cursor.
            word = line_prefix.strip()
            return self._key_completions(doc, section, word, line_start)

        after_colon = line_prefix[colon + 1:]
        word = after_colon.strip()
        if word == "":
            return self._reference_completions(doc, section, "")

        # A word after the colon: offer matching built-in types and symbols.
        items = self._reference_completions(doc, section, word)
        for type_name in ("str", "int", "float", "bool"):
            if type_name.startswith(word.split(".")[-1]):
                items.insert(0, {
                    "label": type_name, "kind": COMPLETION_TYPE,
                    "detail": "type annotation",
                })
        return items

    def _key_completions(self, doc, section: str, word: str,
                         cursor_line_start: int = -1) -> list[dict]:
        # Keys already declared in this document are hidden — except for the
        # entry on the cursor's own line, which the user is still typing.
        used: set[str] = set()
        for entry in self._entries(doc.uri, section):
            if entry.full_range.start == cursor_line_start:
                continue
            used.add(entry.key)

        candidates: dict[str, Entry] = {}
        for other_uri in self.documents:
            for entry in self._entries(other_uri, section):
                if other_uri == doc.uri and entry.key in used:
                    continue
                candidates.setdefault(entry.key, entry)
        items = []
        for key in sorted(candidates):
            if key.startswith(word):
                items.append({"label": key, "kind": COMPLETION_KEY,
                              "detail": "key in [%s]" % (section or "root")})
        return items

    def _reference_completions(self, doc, section: str, word: str) -> list[dict]:
        labels: set[str] = set()

        def add(label: str) -> None:
            if label.startswith(word):
                labels.add(label)

        # Local document: bare keys for same-section / root, dotted for the rest.
        for sec_name, entries in self._entry_index.get(doc.uri, {}).items():
            for entry in entries:
                if sec_name in ("", section):
                    add(entry.key)
                if sec_name:
                    add(sec_name + "." + entry.key)

        # Transitively imported documents: stem-qualified labels.
        for stem in sorted(self._reachable_stems(doc)):
            target_uri = self._doc_for_stem(stem)
            if target_uri is None:
                continue
            for sec_name, entries in self._entry_index.get(target_uri, {}).items():
                for entry in entries:
                    if sec_name in ("", section):
                        add(stem + "." + entry.key)
                    if sec_name:
                        add(stem + "." + sec_name + "." + entry.key)

        return [{"label": label, "kind": COMPLETION_REFERENCE,
                 "detail": "reference"} for label in sorted(labels)]


def entry_section_at(doc, offset: int) -> str:
    """Section containing *offset*.

    Works even when the line at *offset* has not (yet) parsed into an entry,
    because completion is frequently requested mid-typing: the current
    section is derived from the raw text of preceding lines as well as from
    parsed section headers.
    """
    line_start = doc.text.rfind("\n", 0, offset) + 1
    prefix = doc.text[:line_start]
    current = ""
    for raw_line in prefix.splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped[1:-1].strip(" \t")
    return current
