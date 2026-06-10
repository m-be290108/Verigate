"""Tests for the trusted-corpus store (CorpusDB).

Includes the finding-11 regression: FTS5 external-content index must stay
ghost-free across upserts and deletes, proven by the extended
('integrity-check', 1) form which compares index against content.
"""

from __future__ import annotations

import sqlite3

import pytest

from verigate.canonical import canonical_text
from verigate.corpus import CorpusDB


@pytest.fixture
def db(tmp_path):
    with CorpusDB(tmp_path / "corpus.sqlite", create=True) as corpus:
        yield corpus


def _integrity_check(corpus: CorpusDB) -> None:
    """Extended FTS5 integrity check — raises sqlite3.DatabaseError on a
    ghost-ridden index (the plain form would pass, finding-11 lesson)."""
    corpus._conn.execute(
        "INSERT INTO documents_fts(documents_fts, rank) VALUES('integrity-check', 1)"
    )


def _rowid(corpus: CorpusDB, doc_id: str) -> int:
    return corpus._conn.execute(
        "SELECT rowid FROM documents WHERE id = ?", (doc_id,)
    ).fetchone()[0]


# --------------------------------------------------------------------- #
# Finding-11 regression (upsert + delete paths)
# --------------------------------------------------------------------- #


def test_finding11_upsert_leaves_no_ghost_fts_entry(db):
    db.add_document("doc1", "a.md", "the zebra crosses the savanna", "sha-a")
    rowid_before = _rowid(db, "doc1")

    db.add_document("doc1", "a.md", "the giraffe eats the acacia", "sha-b")
    rowid_after = _rowid(db, "doc1")

    # Upsert preserves the rowid (ON CONFLICT DO UPDATE, never OR REPLACE).
    assert rowid_after == rowid_before
    # Extended integrity check passes: index matches content, no ghosts.
    _integrity_check(db)
    # Token only in the OLD text is gone from the index...
    assert db.search("zebra") == []
    # ...and the NEW text is found.
    assert [doc_id for doc_id, _ in db.search("giraffe")] == ["doc1"]


def test_finding11_repeated_upserts_stay_clean(db):
    for i in range(5):
        db.add_document("doc1", "a.md", f"version {i} unicornword{i}", "sha")
    _integrity_check(db)
    assert db.search("unicornword0") == []
    assert [d for d, _ in db.search("unicornword4")] == ["doc1"]


def test_delete_document_removes_fts_and_keeps_integrity(db):
    db.add_document("doc1", "a.md", "ephemeral pelican content", "sha")
    db.add_reference("REF1", "ref-1", "doc1", "core")
    assert db.search("pelican") != []

    db.delete_document("doc1")

    assert db.search("pelican") == []
    assert db.doc_count() == 0
    assert db.reference_count() == 0
    _integrity_check(db)
    ok, errors = db.verify_corpus()
    assert ok, errors


# --------------------------------------------------------------------- #
# add_document semantics
# --------------------------------------------------------------------- #


def test_add_document_precomputes_canonical_column(db):
    db.add_document("doc1", "a.md", "Héllo, Wörld! 42", "sha")
    stored = db._conn.execute(
        "SELECT canonical FROM documents WHERE id = 'doc1'"
    ).fetchone()[0]
    assert stored == canonical_text("Héllo, Wörld! 42")


def test_add_document_update_refreshes_all_columns(db):
    db.add_document("doc1", "old.md", "old text", "sha-old")
    db.add_document("doc1", "new.md", "new text", "sha-new")
    row = db._conn.execute(
        "SELECT source_path, sha256, text, canonical FROM documents WHERE id = 'doc1'"
    ).fetchone()
    assert row == ("new.md", "sha-new", "new text", canonical_text("new text"))
    assert db.doc_count() == 1


def test_reingest_deletes_registry_rows_for_doc(db):
    db.add_document("doc1", "a.md", "text one", "sha1")
    db.add_reference("REF1", "ref-1", "doc1", "core")
    db.add_number("42", "42", "money", "doc1")
    db.add_entity("acme", "Acme", "doc1")
    db.add_document("doc2", "b.md", "other", "sha2")
    db.add_entity("other", "Other", "doc2")

    db.add_document("doc1", "a.md", "text two", "sha1b")

    assert db.reference_count() == 0
    assert db.number_count() == 0
    assert db.entity_count() == 1  # doc2's entity untouched


# --------------------------------------------------------------------- #
# Registries: INSERT OR IGNORE, lookups, tie-breaks
# --------------------------------------------------------------------- #


def test_registry_duplicates_are_ignored(db):
    db.add_document("doc1", "a.md", "x", "sha")
    for _ in range(3):
        db.add_reference("REF1", "ref-1", "doc1", "core")
        db.add_number("42", "42", "money", "doc1")
        db.add_entity("acme", "Acme", "doc1")
    assert db.reference_count() == 1
    assert db.number_count() == 1
    assert db.entity_count() == 1


def test_has_reference_hit_and_miss(db):
    db.add_document("doc1", "a.md", "x", "sha")
    db.add_reference("L12333", "L. 1233-3", "doc1", "legal")
    assert db.has_reference("L12333") == "doc1"
    assert db.has_reference("NOPE") is None


def test_has_number_hit_miss_and_kind_filter(db):
    db.add_document("doc1", "a.md", "x", "sha")
    db.add_number("249.99", "249,99", "money", "doc1")
    assert db.has_number("249.99") == "doc1"
    assert db.has_number("249.99", kind="money") == "doc1"
    assert db.has_number("249.99", kind="percent") is None
    assert db.has_number("1.5") is None


def test_has_entity_hit_and_miss(db):
    db.add_document("doc1", "a.md", "x", "sha")
    db.add_entity("aquapump 3000", "AquaPump 3000", "doc1")
    assert db.has_entity("aquapump 3000") == "doc1"
    assert db.has_entity("hydrofilter") is None


def test_lookup_tie_break_smallest_doc_id(db):
    # Insert the larger doc_id first to prove the order is not insertion order.
    db.add_document("zz", "z.md", "x", "sha-z")
    db.add_document("aa", "a.md", "x", "sha-a")
    db.add_reference("REF", "ref", "zz", "core")
    db.add_reference("REF", "ref", "aa", "core")
    db.add_number("7.5", "7,5", "percent", "zz")
    db.add_number("7.5", "7,5", "percent", "aa")
    db.add_entity("acme", "Acme", "zz")
    db.add_entity("acme", "Acme", "aa")
    assert db.has_reference("REF") == "aa"
    assert db.has_number("7.5") == "aa"
    assert db.has_entity("acme") == "aa"


def test_entities_sorted_and_deduped_by_canonical(db):
    db.add_document("d1", "a.md", "x", "s1")
    db.add_document("d2", "b.md", "x", "s2")
    db.add_entity("zeta", "Zeta", "d1")
    db.add_entity("alpha", "alpha corp", "d2")
    db.add_entity("alpha", "Alpha", "d1")
    # 'Alpha' < 'alpha corp' in code-point order -> first raw wins.
    assert db.entities() == [("alpha", "Alpha"), ("zeta", "Zeta")]


# --------------------------------------------------------------------- #
# contains_text
# --------------------------------------------------------------------- #


def test_contains_text_positive_negative_empty(db):
    db.add_document("doc1", "a.md", 'Warranty: "covered for 24 months".', "sha")
    assert db.contains_text(canonical_text("covered for 24 months")) == "doc1"
    assert db.contains_text(canonical_text("covered for 36 months")) is None
    assert db.contains_text("") is None


def test_contains_text_like_wildcards_are_literal(db):
    db.add_document("doc1", "a.md", "axyzb abc", "sha")
    # canonical column is "axyzbabc"; unescaped LIKE would match both.
    assert db.contains_text("a%b") is None
    assert db.contains_text("a_c") is None
    assert db.contains_text("axyzb") == "doc1"


def test_contains_text_tie_break_smallest_doc_id(db):
    db.add_document("zz", "z.md", "shared fragment here", "s1")
    db.add_document("aa", "a.md", "shared fragment here", "s2")
    assert db.contains_text(canonical_text("shared fragment")) == "aa"


# --------------------------------------------------------------------- #
# search
# --------------------------------------------------------------------- #


def test_search_returns_doc_and_snippet(db):
    db.add_document("doc1", "a.md", "the pump is submersible and quiet", "sha")
    results = db.search("submersible")
    assert len(results) == 1
    doc_id, snippet = results[0]
    assert doc_id == "doc1"
    assert "[submersible]" in snippet


def test_search_respects_limit(db):
    for i in range(8):
        db.add_document(f"doc{i}", "a.md", "common keyword everywhere", f"sha{i}")
    assert len(db.search("keyword", limit=3)) == 3
    assert len(db.search("keyword", limit=10)) == 8


def test_search_query_syntax_is_inert(db):
    db.add_document("doc1", "a.md", "alpha only", "sha1")
    db.add_document("doc2", "b.md", "bravo only", "sha2")
    # Unescaped, 'alpha OR bravo' would match both docs; quoted tokens make
    # OR a literal term that no document contains -> no match.
    assert db.search("alpha OR bravo") == []
    # FTS operators/specials must not raise.
    assert db.search('alpha"') == [("doc1", "[alpha] only")]
    assert db.search("alpha NEAR(bravo)") == []
    assert db.search("col:alpha*") == []


def test_search_empty_or_punctuation_query(db):
    db.add_document("doc1", "a.md", "something", "sha")
    assert db.search("") == []
    assert db.search('   "  -- ') == []


# --------------------------------------------------------------------- #
# meta + fingerprint
# --------------------------------------------------------------------- #


def test_meta_roundtrip_and_overwrite(db):
    assert db.get_meta("k") is None
    db.set_meta("k", "v1")
    assert db.get_meta("k") == "v1"
    db.set_meta("k", "v2")
    assert db.get_meta("k") == "v2"


def test_fingerprint_empty_before_finalize_then_stored(db):
    assert db.fingerprint() == ""
    db.add_document("doc1", "a.md", "text", "sha")
    fp = db.finalize_manifest()
    assert fp == db.fingerprint()
    assert len(fp) == 64
    ok, errors = db.verify_corpus()
    assert ok, errors


def test_fingerprint_independent_of_insertion_order(tmp_path):
    def build(name, order):
        corpus = CorpusDB(tmp_path / name, create=True)
        for step in order:
            step(corpus)
        return corpus.finalize_manifest()

    def doc_a(c):
        c.add_document("doc-a", "a.md", "alpha text", "sha-a")

    def doc_b(c):
        c.add_document("doc-b", "b.md", "bravo text", "sha-b")

    def regs(c):
        c.add_reference("REF1", "ref-1", "doc-a", "core")
        c.add_number("42", "42", "money", "doc-b")
        c.add_entity("acme", "Acme", "doc-a")

    fp1 = build("one.sqlite", [doc_a, doc_b, regs])
    fp2 = build("two.sqlite", [doc_b, doc_a, regs])
    assert fp1 == fp2


def test_fingerprint_changes_with_content(tmp_path):
    c1 = CorpusDB(tmp_path / "c1.sqlite", create=True)
    c1.add_document("doc1", "a.md", "alpha", "sha-1")
    c2 = CorpusDB(tmp_path / "c2.sqlite", create=True)
    c2.add_document("doc1", "a.md", "alpha", "sha-DIFFERENT")
    assert c1.finalize_manifest() != c2.finalize_manifest()


# --------------------------------------------------------------------- #
# verify_corpus
# --------------------------------------------------------------------- #


def test_verify_corpus_clean_db(db):
    db.add_document("doc1", "a.md", "healthy text", "sha")
    db.add_reference("REF1", "ref", "doc1", "core")
    ok, errors = db.verify_corpus()
    assert ok
    assert errors == []


def test_verify_corpus_detects_tampered_canonical(db):
    db.add_document("doc1", "a.md", "honest text", "sha")
    # Bypass the FTS triggers' content columns: only `canonical` is touched,
    # but the AFTER UPDATE trigger keeps the FTS index consistent.
    db._conn.execute("UPDATE documents SET canonical = 'x' WHERE id = 'doc1'")
    ok, errors = db.verify_corpus()
    assert not ok
    assert any("canonical" in e and "doc1" in e for e in errors)


def test_verify_corpus_detects_orphan_registry_rows(db):
    db.add_document("doc1", "a.md", "text", "sha")
    db.add_reference("REF1", "ref", "ghost-doc", "core")
    db.add_number("42", "42", "money", "ghost-doc")
    db.add_entity("acme", "Acme", "ghost-doc")
    ok, errors = db.verify_corpus()
    assert not ok
    assert any(e.startswith("refs:") and "ghost-doc" in e for e in errors)
    assert any(e.startswith("numbers:") and "ghost-doc" in e for e in errors)
    assert any(e.startswith("entities:") and "ghost-doc" in e for e in errors)


def test_verify_corpus_detects_stale_fingerprint(db):
    db.add_document("doc1", "a.md", "text", "sha")
    db.finalize_manifest()
    db.add_document("doc2", "b.md", "more text", "sha2")
    ok, errors = db.verify_corpus()
    assert not ok
    assert any("fingerprint" in e for e in errors)
    # Re-finalizing heals it.
    db.finalize_manifest()
    ok, errors = db.verify_corpus()
    assert ok, errors


def test_verify_corpus_never_raises_on_content_problems(db):
    db.add_document("doc1", "a.md", "text", "sha")
    db._conn.execute("UPDATE documents SET canonical = 'bogus'")
    db.add_entity("x", "X", "missing-doc")
    db.set_meta("fingerprint", "deadbeef")
    ok, errors = db.verify_corpus()
    assert not ok
    assert len(errors) >= 3


# --------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------- #


def test_create_false_on_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        CorpusDB(tmp_path / "does-not-exist.sqlite")


def test_context_manager_closes_connection(tmp_path):
    with CorpusDB(tmp_path / "c.sqlite", create=True) as corpus:
        corpus.add_document("doc1", "a.md", "text", "sha")
    with pytest.raises(sqlite3.ProgrammingError):
        corpus.doc_count()


def test_reopen_without_create_sees_data(tmp_path):
    path = tmp_path / "c.sqlite"
    with CorpusDB(path, create=True) as corpus:
        corpus.add_document("doc1", "a.md", "persistent text", "sha")
        corpus.finalize_manifest()
        fp = corpus.fingerprint()
    with CorpusDB(path) as reopened:
        assert reopened.doc_count() == 1
        assert reopened.fingerprint() == fp
        ok, errors = reopened.verify_corpus()
        assert ok, errors


def test_offline_end_to_end(no_network, tmp_path):
    with CorpusDB(tmp_path / "c.sqlite", create=True) as corpus:
        corpus.add_document("doc1", "a.md", "offline corpus text", "sha")
        corpus.finalize_manifest()
        assert corpus.search("offline") != []
        ok, errors = corpus.verify_corpus()
        assert ok, errors
