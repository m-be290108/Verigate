"""Folder ingestion — a customer drops a folder of documents, VeriGate
builds ``corpus.db`` from it.

Deterministic by construction: the folder is walked as
``sorted(folder.rglob('*'))``, doc ids are POSIX relative paths, provenance
is the sha256 of the source bytes, and the manifest fingerprint is computed
by :meth:`CorpusDB.finalize_manifest` over sorted content sets. Ingesting
the same folder twice — into a fresh database or over an existing one —
yields the same fingerprint and the same counts (``add_document`` upserts
and clears the per-doc registry rows first).

Files that cannot be loaded are *reported*, never silently dropped
(no-silent-caps rule): every skipped file lands in ``IngestResult.skipped``
as ``(relpath, reason)``. Two paths are consumed rather than skipped: the
database file itself (if it lives inside the folder) and the active
glossary file.

Entity glossary — three sources, in this order:

1. **Explicit YAML glossary** — the ``glossary_path`` argument, else
   ``<folder>/glossary.yaml`` if present. Accepted shapes: a YAML list of
   strings, or a mapping ``{entities: [list of strings]}``. Anything else
   raises :class:`ValueError` — explicit configuration deserves a hard
   error, unlike a corrupt data file. The glossary file is registered as a
   document of its own (provenance + integrity: entity rows must point to
   an existing doc_id for ``verify_corpus``), but reference/number
   extractors do not run on it.
2. **CSV columns** named (case-insensitively) ``name`` / ``title`` /
   ``product`` / ``label``: each cell value of 1..6 words becomes an
   entity of that CSV document.
3. **Markdown headings** of level 2+ (``## `` etc.): the heading text minus
   any parenthesized part, kept if it has 1..6 words.

Entities are stored with ``canonical_entity(value)`` as the canonical key
and ``value.strip()`` as the raw form.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from verigate.canonical import canonical_entity
from verigate.corpus import CorpusDB
from verigate.extract.numbers import extract_numbers
from verigate.extract.references import extract_references
from verigate.ingest.loaders import SUPPORTED_EXTENSIONS, LoaderError, load_file

#: CSV header names whose cells become glossary entities (lowercase).
_ENTITY_CSV_COLUMNS = frozenset({"name", "title", "product", "label"})

#: Markdown headings of level 2+ ('## Title', '### Title', …).
_MD_HEADING_RE = re.compile(r"^#{2,}\s+(?P<text>.+)$", re.MULTILINE)

#: Parenthesized segments removed from markdown headings before the
#: word-count gate ('## AquaPump 3000 (SKU AP-3000-X)' → 'AquaPump 3000').
_PARENTHESIZED_RE = re.compile(r"\([^)]*\)")

#: Inclusive word-count bounds for a cell/heading to qualify as an entity.
_ENTITY_MIN_WORDS = 1
_ENTITY_MAX_WORDS = 6


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one :func:`ingest_folder` run.

    Counts are the database totals after ingestion (registry rows, not
    distinct canonicals). `skipped` lists every input file that was not
    ingested, as ``(relpath, reason)`` pairs in walk (sorted) order —
    explicit, never silent.
    """

    n_docs: int
    n_refs: int
    n_numbers: int
    n_entities: int
    fingerprint: str
    skipped: tuple[tuple[str, str], ...]


def _parse_glossary(path: Path) -> tuple[list[str], bytes]:
    """Parse an explicit YAML glossary; returns (entries, source_bytes).

    Malformed input — unreadable file, broken encoding, invalid YAML, or a
    shape other than a list of non-empty strings / ``{entities: [...]}`` —
    raises :class:`ValueError`.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"glossary {path}: unreadable ({exc})") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"glossary {path}: not valid UTF-8 ({exc})") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"glossary {path}: invalid YAML ({exc})") from exc
    entries = data.get("entities") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError(
            f"glossary {path}: expected a list of strings or a mapping with an"
            " 'entities' list"
        )
    out: list[str] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"glossary {path}: entry #{i} must be a non-empty string")
        out.append(entry)
    return out, raw


def _csv_entities(decoded: str) -> list[str]:
    """Cell values of name/title/product/label columns with 1..6 words."""
    rows = list(csv.reader(io.StringIO(decoded)))
    if not rows:
        return []
    indexes = [
        i for i, head in enumerate(rows[0]) if head.strip().lower() in _ENTITY_CSV_COLUMNS
    ]
    values: list[str] = []
    for row in rows[1:]:
        for i in indexes:
            if i >= len(row):
                continue
            cell = row[i].strip()
            if _ENTITY_MIN_WORDS <= len(cell.split()) <= _ENTITY_MAX_WORDS:
                values.append(cell)
    return values


def _md_entities(text: str) -> list[str]:
    """Level-2+ heading texts, parenthesized parts removed, 1..6 words."""
    values: list[str] = []
    for m in _MD_HEADING_RE.finditer(text):
        heading = _PARENTHESIZED_RE.sub("", m.group("text")).strip()
        if _ENTITY_MIN_WORDS <= len(heading.split()) <= _ENTITY_MAX_WORDS:
            values.append(heading)
    return values


def _add_entities(db: CorpusDB, values: list[str], doc_id: str) -> None:
    """Register entity values for `doc_id`; empty canonicals are dropped."""
    for value in values:
        canonical = canonical_entity(value)
        if canonical:
            db.add_entity(canonical, value.strip(), doc_id)


def _glossary_doc_id(glossary_file: Path, folder: Path) -> str:
    """Doc id for the glossary document: POSIX relpath when the file lives
    inside the folder, ``glossary:<name>`` otherwise."""
    try:
        return glossary_file.resolve().relative_to(folder.resolve()).as_posix()
    except ValueError:
        return f"glossary:{glossary_file.name}"


def ingest_folder(
    folder: str | Path,
    db_path: str | Path,
    packs: list[str] | None = None,
    glossary_path: str | Path | None = None,
) -> IngestResult:
    """Build (or update) the corpus database at `db_path` from `folder`.

    Walks ``sorted(folder.rglob('*'))``; hidden files/dirs (any path part
    starting with '.') are ignored, the database file itself is skipped if
    it lives inside the folder. Supported files are ingested with their
    source-bytes sha256 as provenance; per document, references
    (`packs` forwarded to :func:`extract_references`), anchored numbers and
    entities (see module docstring) are registered. A file that fails to
    load is recorded in ``skipped`` and ingestion continues — one corrupt
    PDF must not block the rest of the folder.

    Raises :class:`ValueError` if `folder` is not an existing directory or
    if an explicit glossary is malformed.
    """
    folder = Path(folder)
    if not folder.is_dir():
        raise ValueError(f"not an existing folder: {folder}")
    db_path = Path(db_path)
    db_resolved = db_path.resolve()

    glossary_file: Path | None = None
    if glossary_path is not None:
        glossary_file = Path(glossary_path)
        if not glossary_file.is_file():
            raise ValueError(f"glossary file not found: {glossary_file}")
    elif (folder / "glossary.yaml").is_file():
        glossary_file = folder / "glossary.yaml"
    glossary_entries: list[str] = []
    glossary_bytes = b""
    glossary_resolved: Path | None = None
    if glossary_file is not None:
        glossary_entries, glossary_bytes = _parse_glossary(glossary_file)
        glossary_resolved = glossary_file.resolve()

    # Materialized before the database is opened, so the db file and its
    # sqlite journal can never appear mid-walk.
    paths = sorted(folder.rglob("*"))
    skipped: list[tuple[str, str]] = []

    with CorpusDB(db_path, create=True) as db:
        for path in paths:
            if not path.is_file():
                continue
            rel = path.relative_to(folder)
            if any(part.startswith(".") for part in rel.parts):
                continue  # hidden file or inside a hidden directory
            resolved = path.resolve()
            if resolved == db_resolved or resolved == glossary_resolved:
                continue  # consumed by VeriGate itself, not corpus data
            relpath = rel.as_posix()
            suffix = path.suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                skipped.append((relpath, f"unsupported extension {suffix}"))
                continue
            try:
                source_bytes = path.read_bytes()
            except OSError as exc:
                skipped.append((relpath, f"unreadable: {exc}"))
                continue
            try:
                text = load_file(path)
            except LoaderError as exc:
                skipped.append((relpath, str(exc)))
                continue
            sha256 = hashlib.sha256(source_bytes).hexdigest()
            db.add_document(relpath, str(path), text, sha256)
            for atom in extract_references(text, packs):
                db.add_reference(atom.canonical, atom.raw, relpath, atom.pack)
            for atom in extract_numbers(text):
                kind = atom.pack.removeprefix("number:")
                db.add_number(atom.canonical, atom.raw, kind, relpath)
            if suffix == ".csv":
                _add_entities(db, _csv_entities(source_bytes.decode("utf-8")), relpath)
            elif suffix == ".md":
                _add_entities(db, _md_entities(text), relpath)

        if glossary_file is not None:
            doc_id = _glossary_doc_id(glossary_file, folder)
            db.add_document(
                doc_id,
                str(glossary_file),
                glossary_bytes.decode("utf-8"),
                hashlib.sha256(glossary_bytes).hexdigest(),
            )
            _add_entities(db, glossary_entries, doc_id)

        fingerprint = db.finalize_manifest()
        return IngestResult(
            n_docs=db.doc_count(),
            n_refs=db.reference_count(),
            n_numbers=db.number_count(),
            n_entities=db.entity_count(),
            fingerprint=fingerprint,
            skipped=tuple(skipped),
        )
