"""Section-scoped verification + strict mode (D-018).

Scoped mode verifies each fact against the section of the answer's SUBJECT,
not the whole corpus — so a real value attributed to the wrong subject
(cross-attribution, the D-013 hole) is caught. Strict mode additionally
removes unverifiable atoms so only grounded facts reach the user. Both are
opt-in; default behavior is unchanged (covered by the rest of the suite).
"""

from __future__ import annotations

import pytest

from verigate import Gate
from verigate.canonical import canonical_entity
from verigate.corpus import CorpusDB
from verigate.types import AtomStatus, AtomType, Verdict
from verigate.verify.engine import Verifier, VerifyConfig

CATALOG = """# Catalog

All products carry a 24 month warranty.

## Alpha Widget

Submersible widget. Price: €10.00. Reference REF-ALPHA-1.

## Bravo Widget

Industrial widget. Price: €999.00. Reference REF-BRAVO-2.
"""


@pytest.fixture
def corpus_db(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / "catalog.md").write_text(CATALOG, encoding="utf-8")
    db = tmp_path / "corpus.db"
    Gate.ingest(folder, db)
    return db


def _verify(db, answer, *, scoped=False, strict=False, context=None):
    with CorpusDB(db) as corpus:
        v = Verifier(corpus, VerifyConfig(scoped=scoped, strict=strict))
        return v.verify(answer, context=context)


def _statuses(report, atom_type):
    return [r.status for r in report.atoms if r.atom.type is atom_type]


class TestCrossAttribution:
    def test_wrong_price_for_subject_caught_in_scoped_mode(self, corpus_db):
        # €999 is real — but it is Bravo's price, attributed here to Alpha.
        r = _verify(corpus_db, "The Alpha Widget costs €999.00.", scoped=True)
        num = [x for x in r.atoms if x.atom.type is AtomType.NUMBER]
        assert num and num[0].status is AtomStatus.MISMATCHED
        assert "cross-attribution" in num[0].detail
        assert r.verdict is not Verdict.VERIFIED
        assert "⟨" in r.corrected_answer  # the false figure was removed

    def test_same_answer_passes_in_global_mode(self, corpus_db):
        # Proves we fixed a REAL hole: membership-only (default) still blesses it.
        r = _verify(corpus_db, "The Alpha Widget costs €999.00.", scoped=False)
        num = [x for x in r.atoms if x.atom.type is AtomType.NUMBER]
        assert num and num[0].status is AtomStatus.VERIFIED
        assert r.verdict is Verdict.VERIFIED

    def test_right_price_for_subject_verifies_scoped(self, corpus_db):
        r = _verify(corpus_db, "The Alpha Widget costs €10.00.", scoped=True)
        assert r.verdict is Verdict.VERIFIED
        assert r.is_grounded

    def test_cross_attributed_reference_caught(self, corpus_db):
        # REF-BRAVO-2 is real but belongs to Bravo, not Alpha.
        r = _verify(corpus_db, "The Alpha Widget ships under REF-BRAVO-2.", scoped=True)
        refs = [x for x in r.atoms if x.atom.type is AtomType.REFERENCE]
        assert refs and refs[0].status is AtomStatus.MISMATCHED

    def test_right_reference_verifies_scoped(self, corpus_db):
        r = _verify(corpus_db, "The Alpha Widget ships under REF-ALPHA-1.", scoped=True)
        refs = [x for x in r.atoms if x.atom.type is AtomType.REFERENCE]
        assert refs and refs[0].status is AtomStatus.VERIFIED


class TestSharedFacts:
    def test_preamble_fact_applies_to_any_subject(self, corpus_db):
        # The 24-month warranty lives in the shared preamble section.
        r = _verify(corpus_db, "The Alpha Widget has a 24 month warranty.", scoped=True)
        nums = _statuses(r, AtomType.NUMBER)
        assert AtomStatus.VERIFIED in nums
        assert AtomStatus.MISMATCHED not in nums

    def test_shared_fact_for_the_other_subject_too(self, corpus_db):
        r = _verify(corpus_db, "The Bravo Widget has a 24 month warranty.", scoped=True)
        assert AtomStatus.MISMATCHED not in _statuses(r, AtomType.NUMBER)


class TestMultiSubject:
    def test_both_values_verify_when_both_subjects_present(self, corpus_db):
        r = _verify(
            corpus_db,
            "The Alpha Widget costs €10.00 and the Bravo Widget costs €999.00.",
            scoped=True,
        )
        assert r.verdict is Verdict.VERIFIED
        assert AtomStatus.MISMATCHED not in _statuses(r, AtomType.NUMBER)


class TestNoSubjectFallback:
    def test_no_subject_falls_back_to_global_with_warning(self, corpus_db):
        # No entity named -> cannot scope -> global membership + a warning.
        r = _verify(corpus_db, "It costs €999.00.", scoped=True)
        assert any("scoped verification skipped" in w for w in r.warnings)
        num = [x for x in r.atoms if x.atom.type is AtomType.NUMBER]
        assert num and num[0].status is AtomStatus.VERIFIED  # 999 exists globally

    def test_global_mode_emits_no_scope_warning(self, corpus_db):
        r = _verify(corpus_db, "It costs €999.00.", scoped=False)
        assert not any("scoped" in w for w in r.warnings)


class TestStrictMode:
    def test_strict_strips_unverifiable_atoms(self, corpus_db):
        # "Gamma Widget" shares the 'widget' token (so it IS extracted as a
        # candidate) but is too far from any glossary entry to verify →
        # UNVERIFIABLE.
        answer = "The Alpha Widget costs €10.00, unlike the Gamma Widget."
        lenient = _verify(corpus_db, answer, scoped=True, strict=False)
        strict = _verify(corpus_db, answer, scoped=True, strict=True)
        unverifiable = [
            r for r in lenient.atoms if r.status is AtomStatus.UNVERIFIABLE
        ]
        assert unverifiable, "expected at least one unverifiable atom"
        raw = unverifiable[0].atom.raw
        assert raw in lenient.corrected_answer  # lenient keeps it
        assert raw not in strict.corrected_answer  # strict removes it
        assert "⟨" in strict.corrected_answer

    def test_strict_keeps_grounded_atoms(self, corpus_db):
        r = _verify(corpus_db, "The Alpha Widget costs €10.00.", scoped=True, strict=True)
        assert r.verdict is Verdict.VERIFIED
        assert "€10.00" in r.corrected_answer


class TestIsGrounded:
    def test_is_grounded_true_when_verified(self, corpus_db):
        assert _verify(corpus_db, "The Alpha Widget costs €10.00.", scoped=True).is_grounded

    def test_is_grounded_false_when_cross_attributed(self, corpus_db):
        assert not _verify(
            corpus_db, "The Alpha Widget costs €999.00.", scoped=True
        ).is_grounded


class TestContextInScopedMode:
    def test_context_still_grounds(self, corpus_db):
        # A figure absent from the corpus but present in the call context.
        r = _verify(
            corpus_db,
            "The Alpha Widget weighs 42 kg.",
            scoped=True,
            context=["The Alpha Widget weighs 42 kg net."],
        )
        nums = [x for x in r.atoms if x.atom.type is AtomType.NUMBER]
        assert nums and nums[0].status is AtomStatus.VERIFIED
        assert nums[0].matched_source == "context"


class TestDeterminism:
    def test_scoped_report_byte_identical_across_runs(self, corpus_db):
        a = _verify(corpus_db, "The Alpha Widget costs €999.00.", scoped=True)
        b = _verify(corpus_db, "The Alpha Widget costs €999.00.", scoped=True)
        assert a.to_json() == b.to_json()

    def test_fingerprint_binds_sectioning(self, tmp_path):
        # Same flat atoms, different sectioning -> different fingerprints.
        db1 = tmp_path / "c1.db"
        db2 = tmp_path / "c2.db"
        with CorpusDB(db1, create=True) as c1:
            s = c1.add_section("d.md", 0, "", "", is_shared=True)
            c1.add_number("money:EUR:5", "€5", "money", "d.md", section_id=s)
            fp1 = c1.finalize_manifest()
        with CorpusDB(db2, create=True) as c2:
            s_alpha = c2.add_section("d.md", 0, canonical_entity("Alpha"), "Alpha", is_shared=False)
            c2.add_number("money:EUR:5", "€5", "money", "d.md", section_id=s_alpha)
            fp2 = c2.finalize_manifest()
        assert fp1 != fp2

    def test_fingerprint_stable_for_same_corpus(self, tmp_path):
        def build(p):
            with CorpusDB(p, create=True) as c:
                s = c.add_section("d.md", 0, canonical_entity("Alpha"), "Alpha", is_shared=False)
                c.add_number("money:EUR:5", "€5", "money", "d.md", section_id=s)
                return c.finalize_manifest()
        assert build(tmp_path / "a.db") == build(tmp_path / "b.db")


class TestIngestSections:
    def test_ingest_creates_sections_and_verify_corpus_ok(self, corpus_db):
        with CorpusDB(corpus_db) as c:
            ok, errors = c.verify_corpus()
            assert ok, errors
            n_sections = c._conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
            # preamble + Alpha + Bravo
            assert n_sections >= 3

    def test_csv_row_becomes_a_section(self, tmp_path):
        folder = tmp_path / "data"
        folder.mkdir()
        (folder / "products.csv").write_text(
            "name,price_eur\nAlpha Widget,10.00\nBravo Widget,999.00\n",
            encoding="utf-8",
        )
        db = tmp_path / "c.db"
        Gate.ingest(folder, db)
        # Cross-attribution across CSV rows is caught in scoped mode.
        r = _verify(db, "The Alpha Widget costs €999.00.", scoped=True)
        assert AtomStatus.MISMATCHED in _statuses(r, AtomType.NUMBER) or (
            # if the model-free answer didn't detect subject, at least no false VERIFIED
            r.verdict is not Verdict.VERIFIED
        )


class TestBackwardCompatibility:
    def test_default_config_unchanged_behavior(self, corpus_db):
        # Default (scoped=False, strict=False) must match the membership engine.
        r = _verify(corpus_db, "The Alpha Widget costs €10.00 and €999.00.")
        # both prices exist globally -> both verified, no scope warning
        assert r.verdict is Verdict.VERIFIED
        assert not any("scoped" in w for w in r.warnings)
