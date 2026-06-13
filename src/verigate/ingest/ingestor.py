"""Folder ingestion — a customer drops a folder of documents, VeriGate
builds ``corpus.db`` from it.

Deterministic by construction: the folder is walked as
``sorted(folder.rglob('*'))``, doc ids are POSIX relative paths, provenance
is the sha256 of the source bytes, and the manifest fingerprint is computed
by :meth:`CorpusDB.finalize_manifest` over sorted content sets. Ingesting
the same folder twice — into a fresh database or over an existing one —
yields the same fingerprint and the same counts (``add_document`` upserts
and clears the per-doc registry rows first).

The folder IS the corpus (D-011): after the walk, every document whose
doc_id was not (re-)ingested this run is deleted from the database and
reported in ``IngestResult.pruned`` — so updating an existing database and
building a fresh one from the identical folder yield the same content and
the same fingerprint, and a file removed from the trusted folder stops
verifying answers at the very next ingest.

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

Money-semantic CSV columns (D-016) — a column whose header contains
``price``/``prix``/``cost``/``montant`` as a substring, or ``eur``/``euro``/
``euros`` as a whole token (substring would catch 'couleur'/'valeur'), ALSO
indexes each parseable cell as a ``money:EUR`` atom: ERP exports routinely
carry bare decimal prices with no € sign, which would otherwise index as
plain decimals and never match the ``money:EUR:…`` atoms extracted from
answers. Unparseable cells (empty, 'N/A', free text) are skipped, never
indexed as garbage.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from verigate.canonical import canonical_entity, canonical_number
from verigate.corpus import CorpusDB
from verigate.extract.numbers import extract_numbers
from verigate.extract.references import extract_references
from verigate.ingest.loaders import SUPPORTED_EXTENSIONS, LoaderError, load_file

#: CSV header names whose cells become glossary entities (lowercase).
_ENTITY_CSV_COLUMNS = frozenset({"name", "title", "product", "label"})

#: Substrings of a (lowercased) CSV header marking a money-semantic column.
_MONEY_CSV_SUBSTRINGS = ("price", "prix", "cost", "montant")

#: Whole-token money markers — substring matching would catch the French
#: words 'couleur' and 'valeur'.
_MONEY_CSV_TOKENS = frozenset({"eur", "euro", "euros"})

_HEADER_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: A money-cell amount once the € sign is stripped: digits with optional
#: space/nbsp/narrow-nbsp/comma/dot/apostrophe separators. $/£ cells do NOT
#: qualify — they carry an explicit non-EUR currency and are left to the
#: rendered-text money extractor.
# Digits plus thousand/decimal separators (space, nbsp, narrow-nbsp, comma,
# dot, apostrophe), written with escapes so the separators stay legible.
_MONEY_CELL_RE = re.compile("\\d[\\d \u00a0\u202f.,']*")

#: What canonical_number must yield for the cell to be indexed — anything
#: else means the cell did not parse cleanly and is skipped, not indexed.
_CANONICAL_DECIMAL_RE = re.compile(r"\d+(?:\.\d+)?")

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
    explicit, never silent. `pruned` lists every doc_id deleted because it
    was not (re-)ingested from the folder this run (D-011: the folder IS
    the corpus), sorted — explicit, never silent.
    """

    n_docs: int
    n_refs: int
    n_numbers: int
    n_entities: int
    fingerprint: str
    skipped: tuple[tuple[str, str], ...]
    pruned: tuple[str, ...] = ()


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


def _is_money_header(header: str) -> bool:
    """True iff `header` names a money-semantic column: price/prix/cost/
    montant as a substring ('PrixTTC', 'unit_price'), or eur/euro/euros as
    a whole token ('prix_eur' yes, 'couleur' no)."""
    lowered = header.strip().lower()
    if any(marker in lowered for marker in _MONEY_CSV_SUBSTRINGS):
        return True
    return bool(_MONEY_CSV_TOKENS & set(_HEADER_TOKEN_RE.findall(lowered)))


def _money_cell_value(cell: str) -> str | None:
    """The canonical_number value of a money-semantic CSV cell, or None
    when the cell is not a cleanly parseable EUR amount ('N/A', free text,
    a $/£ amount, or a separator layout canonical_number cannot resolve)."""
    stripped = cell.strip().strip("€").strip()
    lowered = stripped.lower()
    for suffix in ("euros", "euro", "eur"):
        if lowered.endswith(suffix):
            stripped = stripped[: -len(suffix)].strip()
            break
    if not _MONEY_CELL_RE.fullmatch(stripped):
        return None
    value = canonical_number(stripped)
    return value if _CANONICAL_DECIMAL_RE.fullmatch(value) else None


def _md_heading_entity(heading: str) -> str | None:
    """The entity form of a level-2+ heading: parenthesized parts removed,
    kept only when it has 1..6 words. None otherwise (same gate as the
    historical ``_md_entities`` whole-document pass)."""
    stripped = _PARENTHESIZED_RE.sub("", heading).strip()
    if _ENTITY_MIN_WORDS <= len(stripped.split()) <= _ENTITY_MAX_WORDS:
        return stripped
    return None


def _add_entities(
    db: CorpusDB, values: list[str], doc_id: str, section_id: int | None = None
) -> None:
    """Register entity values for `doc_id`; empty canonicals are dropped.

    ``section_id`` (default None) binds each entity to its section where
    natural; it is not used by scoped value lookups (entities ARE subjects).
    """
    for value in values:
        canonical = canonical_entity(value)
        if canonical:
            db.add_entity(canonical, value.strip(), doc_id, section_id)


@dataclass(frozen=True)
class _Section:
    """A document slice that scopes its atoms to a subject (D-018).

    ``subject_canonical`` '' means no subject; ``is_shared`` true means the
    facts apply to ANY subject (preamble or whole unstructured document).
    ``text`` is the slice the reference/number extractors run over.
    """

    subject_canonical: str
    subject_raw: str
    is_shared: bool
    text: str


def _markdown_sections(text: str) -> list[_Section]:
    """Split markdown on ATX headings of level 2+ ('## ', '### ', …).

    Text before the first such heading is one ``is_shared`` preamble section
    (subject ''). Each heading starts a new section: ``subject_raw`` is the
    heading text verbatim (any '(...)' parenthetical kept — the SKU inside is
    useful); ``subject_canonical`` is ``canonical_entity`` of the heading
    WITHOUT the parenthetical, falling back to the full heading when that is
    empty. The sections tile the whole document, so every atom lands in
    exactly one section and the global registries stay fully populated.
    """
    matches = list(_MD_HEADING_RE.finditer(text))
    sections: list[_Section] = []
    preamble = text[: matches[0].start()] if matches else text
    # A preamble is always emitted (even if blank) so an md file with no
    # level-2+ heading still contributes its facts as shared (global) atoms —
    # the safe degrade, identical to an unstructured document.
    if not matches or preamble.strip():
        sections.append(_Section("", "", True, preamble))
    for i, match in enumerate(matches):
        heading = match.group("text").strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        # The section text spans from the heading line itself through to the
        # next heading, so a reference/number written IN the heading (a SKU
        # in '## TurboValve X (SKU TV-900-Z)') is extracted and scoped to its
        # own section — and the global registry stays fully populated.
        body = text[match.start() : end]
        without_paren = _PARENTHESIZED_RE.sub("", heading).strip()
        canonical = canonical_entity(without_paren) or canonical_entity(heading)
        sections.append(_Section(canonical, heading, False, body))
    return sections


def _glossary_doc_id(glossary_file: Path, folder: Path) -> str:
    """Doc id for the glossary document: POSIX relpath when the file lives
    inside the folder, ``glossary:<name>`` otherwise."""
    try:
        return glossary_file.resolve().relative_to(folder.resolve()).as_posix()
    except ValueError:
        return f"glossary:{glossary_file.name}"


def _csv_index_entities_and_money(
    db: CorpusDB, decoded: str, doc_id: str, section_ids: list[int]
) -> None:
    """Register per-row CSV glossary entities (name/title/product/label cells,
    1..6 words) and money-semantic amounts (D-016), each bound to that row's
    section_id (D-018). ``section_ids`` is in data-row order — section i is
    the i-th data row (one section per row, see :func:`_csv_sections`)."""
    rows = list(csv.reader(io.StringIO(decoded)))
    if not rows:
        return
    header = rows[0]
    entity_indexes = [
        i for i, head in enumerate(header) if head.strip().lower() in _ENTITY_CSV_COLUMNS
    ]
    money_indexes = [i for i, head in enumerate(header) if _is_money_header(head)]
    for row_no, row in enumerate(rows[1:]):
        # Defensive: more data rows than sections would only happen if the two
        # CSV passes disagreed on row count (they do not); skip the overflow
        # rather than mis-bind a section_id.
        section_id = section_ids[row_no] if row_no < len(section_ids) else None
        for i in entity_indexes:
            if i >= len(row):
                continue
            cell = row[i].strip()
            if _ENTITY_MIN_WORDS <= len(cell.split()) <= _ENTITY_MAX_WORDS:
                _add_entities(db, [cell], doc_id, section_id)
        for i in money_indexes:
            if i >= len(row):
                continue
            value = _money_cell_value(row[i])
            if value is not None:
                db.add_number(f"money:EUR:{value}", row[i].strip(), "money", doc_id, section_id)


def _csv_row_text(header: list[str], row: list[str]) -> str:
    """Render one CSV data row exactly as :func:`loaders._load_csv` does
    ('label: value | label: value', cells past the header labeled colN), so
    the per-section extraction sees byte-identical text to the global pass."""
    parts = []
    for i, cell in enumerate(row):
        label = header[i] if i < len(header) else f"col{i + 1}"
        parts.append(f"{label}: {cell}")
    return " | ".join(parts)


def _csv_sections(decoded: str) -> list[_Section]:
    """One section per CSV data row (D-018). The subject is the
    name/title/product/label cell if present (``canonical_entity`` of it,
    reusing the entity column detection), else the row is ``is_shared`` (it
    has no subject — its facts apply globally, the safe degrade)."""
    rows = list(csv.reader(io.StringIO(decoded)))
    if not rows:
        return []
    header = rows[0]
    subject_indexes = [
        i for i, head in enumerate(header) if head.strip().lower() in _ENTITY_CSV_COLUMNS
    ]
    sections: list[_Section] = []
    for row in rows[1:]:
        subject_raw = ""
        for i in subject_indexes:
            if i < len(row) and row[i].strip():
                subject_raw = row[i].strip()
                break
        canonical = canonical_entity(subject_raw)
        sections.append(
            _Section(canonical, subject_raw, not canonical, _csv_row_text(header, row))
        )
    return sections


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

    After the walk, documents that were NOT (re-)ingested this run — files
    deleted from the folder, plus stale versions of files now skipped — are
    deleted from the database and reported in ``pruned`` (D-011: the corpus
    mirrors the folder; retention is never silent, and an update over an
    existing database matches a fresh build of the same folder).

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
    #: doc_ids upserted this run — everything else is pruned after the walk.
    seen: set[str] = set()

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
            seen.add(relpath)
            # Sectioning (D-018): markdown splits on level-2+ headings, CSV is
            # one section per row, everything else is ONE shared (global)
            # section spanning the whole document — the safe degrade for
            # unstructured content (its facts are always in scope). The
            # sections tile the document, so every reference/number atom lands
            # in exactly one section and the GLOBAL registries stay fully
            # populated (section_id is an extra column global lookups ignore).
            if suffix == ".md":
                sections = _markdown_sections(text)
            elif suffix == ".csv":
                sections = _csv_sections(source_bytes.decode("utf-8"))
            else:
                sections = [_Section("", "", True, text)]
            section_ids: list[int] = []
            for ordinal, section in enumerate(sections):
                section_id = db.add_section(
                    relpath,
                    ordinal,
                    section.subject_canonical,
                    section.subject_raw,
                    section.is_shared,
                )
                section_ids.append(section_id)
                for atom in extract_references(section.text, packs):
                    db.add_reference(
                        atom.canonical, atom.raw, relpath, atom.pack, section_id
                    )
                for atom in extract_numbers(section.text):
                    kind = atom.pack.removeprefix("number:")
                    db.add_number(atom.canonical, atom.raw, kind, relpath, section_id)
                # A markdown heading entity is bound to its own section (the
                # subject IS that section). Same 1..6-word gate as the
                # historical whole-document entity pass, so the entity set is
                # unchanged — only section_id is now populated.
                if suffix == ".md" and not section.is_shared:
                    entity = _md_heading_entity(section.subject_raw)
                    if entity is not None:
                        _add_entities(db, [entity], relpath, section_id)
            if suffix == ".csv":
                _csv_index_entities_and_money(
                    db, source_bytes.decode("utf-8"), relpath, section_ids
                )

        if glossary_file is not None:
            doc_id = _glossary_doc_id(glossary_file, folder)
            db.add_document(
                doc_id,
                str(glossary_file),
                glossary_bytes.decode("utf-8"),
                hashlib.sha256(glossary_bytes).hexdigest(),
            )
            _add_entities(db, glossary_entries, doc_id)
            seen.add(doc_id)

        # Prune (D-011): the folder IS the corpus. Any document not
        # (re-)ingested this run no longer exists in the source folder (or
        # is no longer loadable) and is deleted — the D-007 AFTER DELETE
        # trigger keeps the FTS index consistent — and reported, so the
        # update-vs-fresh-build fingerprints stay identical and stale
        # documents stop verifying answers.
        pruned = tuple(doc_id for doc_id in db.doc_ids() if doc_id not in seen)
        for doc_id in pruned:
            db.delete_document(doc_id)

        fingerprint = db.finalize_manifest()
        return IngestResult(
            n_docs=db.doc_count(),
            n_refs=db.reference_count(),
            n_numbers=db.number_count(),
            n_entities=db.entity_count(),
            fingerprint=fingerprint,
            skipped=tuple(skipped),
            pruned=pruned,
        )
