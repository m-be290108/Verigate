"""Trusted-corpus store — SQLite-backed, deterministic, offline.

One database holds the ingested documents (with a precomputed canonical
column for quote matching), an FTS5 external-content index over the raw
text, and three registries (refs / numbers / entities) keyed by canonical
form. The FTS maintenance follows the documented external-content pattern
with 'delete'-command triggers and `INSERT ... ON CONFLICT DO UPDATE`
upserts exclusively — never `INSERT OR REPLACE` (D-007, Beaume finding-11:
ghost rowids corrupted 32% of a production index).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
from pathlib import Path

from verigate.canonical import canonical_text

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    text        TEXT NOT NULL,
    canonical   TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    text,
    content='documents',
    content_rowid='rowid'
);

-- External-content maintenance triggers, exactly as documented by SQLite.
-- The 'delete' command needs the OLD values; a plain DELETE/UPDATE on the
-- fts table leaves ghost entries (finding-11 fix).
CREATE TRIGGER IF NOT EXISTS documents_fts_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, text) VALUES (new.rowid, new.text);
END;

CREATE TRIGGER IF NOT EXISTS documents_fts_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, text)
    VALUES('delete', old.rowid, old.text);
END;

CREATE TRIGGER IF NOT EXISTS documents_fts_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, text)
    VALUES('delete', old.rowid, old.text);
    INSERT INTO documents_fts(rowid, text) VALUES (new.rowid, new.text);
END;

-- Sections (scoped verification, D-018). A section binds atoms to the
-- SUBJECT they describe so a value can be checked for the right subject,
-- not merely for membership anywhere in the corpus. is_shared=1 holds facts
-- that apply to ANY subject (a document preamble before the first heading,
-- or a whole unstructured document). subject_canonical='' = no subject.
CREATE TABLE IF NOT EXISTS sections (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id            TEXT NOT NULL,
    ordinal           INTEGER NOT NULL,
    subject_canonical TEXT NOT NULL DEFAULT '',
    subject_raw       TEXT NOT NULL DEFAULT '',
    is_shared         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS refs (
    canonical  TEXT NOT NULL,
    raw        TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    pack       TEXT NOT NULL,
    section_id INTEGER,
    PRIMARY KEY (canonical, doc_id, pack)
);

CREATE TABLE IF NOT EXISTS numbers (
    canonical  TEXT NOT NULL,
    raw        TEXT NOT NULL,
    kind       TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    section_id INTEGER,
    PRIMARY KEY (canonical, kind, doc_id)
);

CREATE TABLE IF NOT EXISTS entities (
    canonical  TEXT NOT NULL,
    raw        TEXT NOT NULL,
    doc_id     TEXT NOT NULL,
    section_id INTEGER,
    PRIMARY KEY (canonical, doc_id)
);
"""


def _escape_like(fragment: str) -> str:
    """Escape LIKE metacharacters so the fragment matches literally."""
    return fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class CorpusDB:
    """The trusted corpus: documents + FTS index + canonical registries.

    Single sqlite3 connection per instance, autocommit mode with explicit
    BEGIN/COMMIT around multi-statement writes. All SQL is parameterized.
    Every query that picks "a" row orders deterministically (smallest
    doc_id wins) so the same database always yields the same answers.
    """

    def __init__(
        self, path: str | Path, create: bool = False, check_same_thread: bool = True
    ):
        """Open (or create) the corpus database at `path`.

        With ``create=True`` the schema is applied (idempotent). With
        ``create=False`` a missing file raises FileNotFoundError instead of
        letting sqlite3 silently create an empty database.

        `check_same_thread` is forwarded verbatim to :func:`sqlite3.connect`.
        The default (True) keeps sqlite3's own thread-affinity guard; a
        caller passing False takes responsibility for serializing EVERY use
        of this instance externally (the API layer does exactly that with
        one app-wide lock — see :mod:`verigate.api.app`).
        """
        path = Path(path)
        if not create and not path.exists():
            raise FileNotFoundError(f"corpus database not found: {path}")
        # isolation_level=None -> autocommit; transactions are explicit.
        self._conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=check_same_thread
        )
        if create:
            self._conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #

    def add_document(self, doc_id: str, source_path: str, text: str, sha256: str) -> None:
        """Insert or update a document (re-ingest semantics).

        UPSERT via ``INSERT ... ON CONFLICT(id) DO UPDATE`` — preserves the
        rowid so the FTS UPDATE trigger fires with correct OLD values
        (D-007; INSERT OR REPLACE would change the rowid and leave a ghost
        FTS entry). The `canonical` column is recomputed from `text`, and
        any refs/numbers/entities rows for this doc_id are deleted first so
        the caller can re-register them from the fresh text.
        """
        canonical = canonical_text(text)
        # COMMIT lives INSIDE the protected block (mirrors AuditTrail.record):
        # a failed COMMIT (e.g. 'database is locked' past the busy timeout)
        # leaves the transaction OPEN; without the guarded ROLLBACK below the
        # connection is poisoned — later autocommit writes silently join the
        # zombie transaction and are discarded on close().
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM refs WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM numbers WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM entities WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM sections WHERE doc_id = ?", (doc_id,))
            self._conn.execute(
                """
                INSERT INTO documents (id, source_path, sha256, text, canonical)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    source_path = excluded.source_path,
                    sha256      = excluded.sha256,
                    text        = excluded.text,
                    canonical   = excluded.canonical
                """,
                (doc_id, source_path, sha256, text, canonical),
            )
            self._conn.execute("COMMIT")
        finally:
            # Reached with an open transaction only when something above
            # raised (BaseException included — the exception keeps
            # propagating past this cleanup, restoring autocommit).
            if self._conn.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")

    def delete_document(self, doc_id: str) -> None:
        """Remove a document and its registry rows. The AFTER DELETE trigger
        removes the FTS entry with the OLD text values ('delete' command)."""
        # Same COMMIT-inside-try + guarded-ROLLBACK pattern as add_document:
        # a failed COMMIT must never leave a zombie transaction behind.
        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM refs WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM numbers WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM entities WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM sections WHERE doc_id = ?", (doc_id,))
            self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
            self._conn.execute("COMMIT")
        finally:
            if self._conn.in_transaction:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.execute("ROLLBACK")

    def add_section(
        self,
        doc_id: str,
        ordinal: int,
        subject_canonical: str,
        subject_raw: str,
        is_shared: bool,
    ) -> int:
        """Register a section and return its autoincrement id.

        A section scopes the atoms registered with its id (scoped lookups,
        D-018). ``is_shared`` true marks facts that apply to ANY subject (a
        document preamble, or a whole unstructured document); the safe
        degrade for content that has no per-subject structure.
        """
        cursor = self._conn.execute(
            "INSERT INTO sections (doc_id, ordinal, subject_canonical, subject_raw, is_shared)"
            " VALUES (?, ?, ?, ?, ?)",
            (doc_id, ordinal, subject_canonical, subject_raw, 1 if is_shared else 0),
        )
        return int(cursor.lastrowid)

    def add_reference(
        self,
        canonical: str,
        raw: str,
        doc_id: str,
        pack: str,
        section_id: int | None = None,
    ) -> None:
        """Register a reference occurrence. INSERT OR IGNORE — registry rows
        are immutable facts, duplicate registrations are a no-op.

        ``section_id`` (kept last, default None so existing callers are
        unaffected) binds the atom to a section for scoped lookups; global
        lookups ignore it.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO refs (canonical, raw, doc_id, pack, section_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (canonical, raw, doc_id, pack, section_id),
        )

    def add_number(
        self,
        canonical: str,
        raw: str,
        kind: str,
        doc_id: str,
        section_id: int | None = None,
    ) -> None:
        """Register an anchored-number occurrence (INSERT OR IGNORE).

        ``section_id`` (kept last, default None) binds the atom to a section
        for scoped lookups; global lookups ignore it.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO numbers (canonical, raw, kind, doc_id, section_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (canonical, raw, kind, doc_id, section_id),
        )

    def add_entity(
        self, canonical: str, raw: str, doc_id: str, section_id: int | None = None
    ) -> None:
        """Register a glossary-entity occurrence (INSERT OR IGNORE).

        ``section_id`` (kept last, default None) binds the entity to its own
        section where natural; not used by scoped value lookups (entities
        ARE the subjects).
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO entities (canonical, raw, doc_id, section_id)"
            " VALUES (?, ?, ?, ?)",
            (canonical, raw, doc_id, section_id),
        )

    # ------------------------------------------------------------------ #
    # Meta / fingerprint
    # ------------------------------------------------------------------ #

    def set_meta(self, key: str, value: str) -> None:
        """Set a metadata key (upsert)."""
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        """Return the metadata value for `key`, or None if absent."""
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _compute_fingerprint(self) -> str:
        """sha256 over a deterministic JSON serialization of the corpus
        content sets: (doc_id, sha256, sha256(canonical)) triples, distinct
        ref (canonical, pack), distinct number (canonical, kind), distinct
        entity (canonical, raw) pairs — all sorted in Python (code-point
        order, collation-independent).

        The canonical-text hash binds the extracted text that quote
        adjudication actually matches against (contains_text), and the
        entity raw form surfaces in report details ('closest known: ...') —
        without both, two corpora with identical fingerprints could yield
        different report bytes for the same answer (loader-drift hole).

        Sectioning is also bound (D-018): the sorted section identity tuples
        (doc_id, ordinal, subject_canonical, is_shared) AND the atom→section
        linkage (each atom's section identity, not its opaque autoincrement
        id). Without this, two corpora with identical flat atoms but
        different sectioning — which adjudicate DIFFERENTLY in scoped mode —
        would share a fingerprint, breaking the report-bytes guarantee."""
        docs = sorted(
            (doc_id, sha256, hashlib.sha256(canonical.encode("utf-8")).hexdigest())
            for doc_id, sha256, canonical in self._conn.execute(
                "SELECT id, sha256, canonical FROM documents"
            )
        )
        refs = sorted(
            tuple(r) for r in self._conn.execute("SELECT DISTINCT canonical, pack FROM refs")
        )
        numbers = sorted(
            tuple(r) for r in self._conn.execute("SELECT DISTINCT canonical, kind FROM numbers")
        )
        ents = sorted(
            tuple(r) for r in self._conn.execute("SELECT DISTINCT canonical, raw FROM entities")
        )
        # Section identity (stable across rebuilds — the autoincrement id is
        # NOT used, only the natural key) and the atom→section linkage.
        sections = sorted(
            tuple(r)
            for r in self._conn.execute(
                "SELECT doc_id, ordinal, subject_canonical, is_shared FROM sections"
            )
        )
        ref_links = sorted(
            tuple(r)
            for r in self._conn.execute(
                "SELECT DISTINCT r.canonical, r.pack,"
                " s.doc_id, s.ordinal, s.subject_canonical, s.is_shared"
                " FROM refs AS r JOIN sections AS s ON s.id = r.section_id"
            )
        )
        number_links = sorted(
            tuple(r)
            for r in self._conn.execute(
                "SELECT DISTINCT n.canonical, n.kind,"
                " s.doc_id, s.ordinal, s.subject_canonical, s.is_shared"
                " FROM numbers AS n JOIN sections AS s ON s.id = n.section_id"
            )
        )
        payload = {
            "documents": [list(p) for p in docs],
            "refs": [list(p) for p in refs],
            "numbers": [list(p) for p in numbers],
            "entities": [list(p) for p in ents],
            "sections": [list(p) for p in sections],
            "ref_links": [list(p) for p in ref_links],
            "number_links": [list(p) for p in number_links],
        }
        blob = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()

    def finalize_manifest(self) -> str:
        """Compute the corpus fingerprint, store it in meta['fingerprint'],
        and return it. Call once at the end of ingestion."""
        fp = self._compute_fingerprint()
        self.set_meta("fingerprint", fp)
        return fp

    def fingerprint(self) -> str:
        """The stored corpus fingerprint, or '' if not finalized."""
        return self.get_meta("fingerprint") or ""

    # ------------------------------------------------------------------ #
    # Lookups (all deterministic: smallest doc_id wins on ties)
    # ------------------------------------------------------------------ #

    def has_reference(self, canonical: str) -> str | None:
        """A doc_id containing this reference canonical, or None."""
        row = self._conn.execute(
            "SELECT doc_id FROM refs WHERE canonical = ? ORDER BY doc_id LIMIT 1",
            (canonical,),
        ).fetchone()
        return row[0] if row else None

    def has_number(self, canonical: str, kind: str | None = None) -> str | None:
        """A doc_id containing this number canonical (optionally restricted
        to `kind`), or None."""
        if kind is None:
            row = self._conn.execute(
                "SELECT doc_id FROM numbers WHERE canonical = ? ORDER BY doc_id LIMIT 1",
                (canonical,),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT doc_id FROM numbers WHERE canonical = ? AND kind = ?"
                " ORDER BY doc_id LIMIT 1",
                (canonical, kind),
            ).fetchone()
        return row[0] if row else None

    def has_entity(self, canonical: str) -> str | None:
        """A doc_id containing this entity canonical, or None."""
        row = self._conn.execute(
            "SELECT doc_id FROM entities WHERE canonical = ? ORDER BY doc_id LIMIT 1",
            (canonical,),
        ).fetchone()
        return row[0] if row else None

    def has_reference_scoped(self, canonical: str, subjects: frozenset[str]) -> str | None:
        """A doc_id where this reference canonical appears in a section whose
        ``subject_canonical`` is in `subjects` OR is ``is_shared=1`` (scoped
        lookup, D-018), smallest doc_id for determinism; else None.

        Shared sections are always in scope (preamble/whole-document facts).
        An empty `subjects` set therefore still matches shared-section atoms.
        """
        return self._scoped_lookup(
            "SELECT r.doc_id AS match_doc FROM refs AS r JOIN sections AS s ON s.id = r.section_id"
            " WHERE r.canonical = ?",
            (canonical,),
            subjects,
        )

    def has_number_scoped(
        self, canonical: str, kind: str | None, subjects: frozenset[str]
    ) -> str | None:
        """A doc_id where this number canonical (optionally restricted to
        `kind`) appears in a section whose ``subject_canonical`` is in
        `subjects` OR is ``is_shared=1`` (scoped lookup, D-018), smallest
        doc_id for determinism; else None. ``kind=None`` matches any kind."""
        if kind is None:
            return self._scoped_lookup(
                "SELECT n.doc_id AS match_doc FROM numbers AS n"
                " JOIN sections AS s ON s.id = n.section_id WHERE n.canonical = ?",
                (canonical,),
                subjects,
            )
        return self._scoped_lookup(
            "SELECT n.doc_id AS match_doc FROM numbers AS n"
            " JOIN sections AS s ON s.id = n.section_id"
            " WHERE n.canonical = ? AND n.kind = ?",
            (canonical, kind),
            subjects,
        )

    def _scoped_lookup(
        self, base_sql: str, base_params: tuple, subjects: frozenset[str]
    ) -> str | None:
        """Shared body of the scoped lookups: add the in-scope section filter
        (subject_canonical IN sorted(subjects) OR is_shared=1) to `base_sql`
        and return the smallest doc_id, or None. Subjects are bound sorted so
        the parameter order is deterministic (it does not affect the result,
        but keeps the emitted SQL reproducible)."""
        ordered = sorted(subjects)
        if ordered:
            placeholders = ",".join("?" for _ in ordered)
            sql = (
                f"{base_sql} AND (s.is_shared = 1 OR s.subject_canonical IN ({placeholders}))"
                " ORDER BY match_doc LIMIT 1"
            )
            params: tuple = base_params + tuple(ordered)
        else:
            sql = f"{base_sql} AND s.is_shared = 1 ORDER BY match_doc LIMIT 1"
            params = base_params
        row = self._conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def entities(self) -> list[tuple[str, str]]:
        """All glossary entities as sorted (canonical, raw) pairs, deduped by
        canonical — the first raw in sort order represents each canonical."""
        rows = self._conn.execute("SELECT canonical, raw FROM entities").fetchall()
        out: dict[str, str] = {}
        for canonical, raw in sorted(rows):
            out.setdefault(canonical, raw)
        return sorted(out.items())

    def contains_text(self, canonical_fragment: str) -> str | None:
        """A doc_id whose precomputed canonical column contains the fragment
        as a literal substring (LIKE metacharacters escaped — canonical_text
        output is alnum-only, but escape anyway), or None. An empty fragment
        matches nothing."""
        if not canonical_fragment:
            return None
        pattern = "%" + _escape_like(canonical_fragment) + "%"
        row = self._conn.execute(
            "SELECT id FROM documents WHERE canonical LIKE ? ESCAPE '\\'"
            " ORDER BY id LIMIT 1",
            (pattern,),
        ).fetchone()
        return row[0] if row else None

    def search(self, query: str, limit: int = 5) -> list[tuple[str, str]]:
        """FTS5 full-text search for demo/debug: returns (doc_id, snippet)
        pairs. Each whitespace token is wrapped in double quotes (inner
        quotes doubled) so FTS query syntax in the input is inert; tokens
        with no alphanumeric content are dropped (they cannot match)."""
        tokens = [t for t in query.split() if any(c.isalnum() for c in t)]
        if not tokens:
            return []
        match = " ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        rows = self._conn.execute(
            """
            SELECT d.id, snippet(documents_fts, 0, '[', ']', '…', 10)
            FROM documents_fts
            JOIN documents AS d ON d.rowid = documents_fts.rowid
            WHERE documents_fts MATCH ?
            ORDER BY rank, d.id
            LIMIT ?
            """,
            (match, limit),
        ).fetchall()
        return [(doc_id, snip) for doc_id, snip in rows]

    # ------------------------------------------------------------------ #
    # Counts
    # ------------------------------------------------------------------ #

    def doc_count(self) -> int:
        """Number of documents."""
        return self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def doc_ids(self) -> list[str]:
        """All document ids, sorted (used by the ingestor's prune pass)."""
        return [
            row[0] for row in self._conn.execute("SELECT id FROM documents ORDER BY id")
        ]

    def reference_count(self) -> int:
        """Number of reference registry rows."""
        return self._conn.execute("SELECT COUNT(*) FROM refs").fetchone()[0]

    def number_count(self) -> int:
        """Number of number registry rows."""
        return self._conn.execute("SELECT COUNT(*) FROM numbers").fetchone()[0]

    def entity_count(self) -> int:
        """Number of entity registry rows."""
        return self._conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]

    # ------------------------------------------------------------------ #
    # Integrity lock
    # ------------------------------------------------------------------ #

    def verify_corpus(self) -> tuple[bool, list[str]]:
        """Integrity lock (modeled on Beaume verify_kb_curated). Checks:

        1. extended FTS integrity check — the ('integrity-check', 1) form,
           which compares the index against the content table (the plain
           form passes on a ghost-ridden index, finding-11 lesson);
        2. every documents.canonical equals canonical_text(text) recomputed;
        3. no refs/numbers/entities row points to a missing doc_id;
        4. no refs/numbers/entities row points to a missing section_id (a
           non-NULL section_id with no matching sections.id — dangling
           scope link, D-018);
        5. the stored fingerprint (if set) equals the recomputed one.

        Returns (ok, messages). Content problems are reported, never raised.
        The FTS check (1) needs a write transaction even though it only
        verifies; on a read-only database file it is reported as *skipped*
        (a distinct message in `messages`) without flipping `ok` — a
        read-only deployment is hardening, not corruption, and every other
        check still runs read-only.
        """
        errors: list[str] = []
        skipped: list[str] = []

        try:
            self._conn.execute(
                "INSERT INTO documents_fts(documents_fts, rank) VALUES('integrity-check', 1)"
            )
        except sqlite3.DatabaseError as exc:
            if exc.sqlite_errorcode == sqlite3.SQLITE_READONLY:
                # Stepping the integrity-check INSERT requires a write
                # transaction, so it cannot run on a read-only file. That is
                # a property of the deployment (chmod 444, read-only mount —
                # plausible hardening for an immutable corpus), NOT index
                # corruption: report honestly, do not fail the lock.
                skipped.append(
                    "fts integrity-check skipped: database file is read-only;"
                    " re-run on a writable copy to check the index"
                )
            else:
                errors.append(f"fts integrity-check failed: {exc}")

        for doc_id, text, canonical in self._conn.execute(
            "SELECT id, text, canonical FROM documents ORDER BY id"
        ):
            if canonical != canonical_text(text):
                errors.append(f"document {doc_id!r}: canonical column does not match text")

        for table in ("refs", "numbers", "entities"):
            orphans = self._conn.execute(
                f"SELECT DISTINCT doc_id FROM {table}"
                " WHERE doc_id NOT IN (SELECT id FROM documents) ORDER BY doc_id"
            ).fetchall()
            for (doc_id,) in orphans:
                errors.append(f"{table}: row(s) point to missing document {doc_id!r}")

        # Dangling scope links (D-018): a non-NULL section_id with no matching
        # sections row. NULL section_id is the global-only atom and is fine.
        for table in ("refs", "numbers", "entities"):
            dangling = self._conn.execute(
                f"SELECT DISTINCT section_id FROM {table}"
                " WHERE section_id IS NOT NULL"
                " AND section_id NOT IN (SELECT id FROM sections) ORDER BY section_id"
            ).fetchall()
            for (section_id,) in dangling:
                errors.append(f"{table}: row(s) point to missing section {section_id!r}")

        stored = self.fingerprint()
        if stored and stored != self._compute_fingerprint():
            errors.append("fingerprint: stored value does not match recomputed corpus content")

        # `ok` is decided by errors alone; skipped checks are reported in the
        # returned messages but never fail the lock.
        return (not errors, errors + skipped)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    def __enter__(self) -> CorpusDB:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
