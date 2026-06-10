"""Tests for the verification engine (Verifier) and rewrite_answer.

Corpora are built inline via CorpusDB(create=True) + add_* + finalize_manifest
— no dependency on the ingest module. Everything is deterministic and offline.
"""

from __future__ import annotations

import hashlib

import pytest

from verigate.canonical import canonical_entity, canonical_ref
from verigate.corpus import CorpusDB
from verigate.types import (
    FALSE_STATUSES,
    REMOVAL_MARKERS,
    Atom,
    AtomResult,
    AtomStatus,
    AtomType,
    Verdict,
)
from verigate.verify.engine import Verifier, VerifyConfig
from verigate.verify.rewrite import rewrite_answer

WARRANTY = "This product is covered for 24 months from the date of purchase."

CATALOG_TEXT = (
    "AquaPump 3000 (SKU AP-3000-X) submersible pump, 230 V, 550 W. "
    f'Price: €249.99. Warranty: "{WARRANTY}"'
)

CLEAN_ANSWER = f'The AquaPump 3000 (SKU AP-3000-X) costs €249.99. "{WARRANTY}"'


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_catalog_corpus(path) -> CorpusDB:
    """One product document + one ref + one money figure + one entity."""
    db = CorpusDB(path, create=True)
    db.add_document("catalog", "catalog.md", CATALOG_TEXT, _sha(CATALOG_TEXT))
    db.add_reference(canonical_ref("AP-3000-X"), "AP-3000-X", "catalog", "sku_ean:sku")
    db.add_number("money:EUR:249.99", "€249.99", "money", "catalog")
    db.add_entity(canonical_entity("AquaPump 3000"), "AquaPump 3000", "catalog")
    db.finalize_manifest()
    return db


@pytest.fixture
def corpus(tmp_path):
    db = build_catalog_corpus(tmp_path / "corpus.db")
    yield db
    db.close()


@pytest.fixture
def verifier(corpus):
    return Verifier(corpus)


# ------------------------------------------------------------- rewrite_answer


def _result(atom_type, start, end, status, raw="x") -> AtomResult:
    atom = Atom(type=atom_type, raw=raw, canonical="k", start=start, end=end, pack="t")
    return AtomResult(atom, status)


def test_rewrite_no_false_atoms_unchanged():
    answer = "hello world"
    results = [
        _result(AtomType.REFERENCE, 0, 5, AtomStatus.VERIFIED),
        _result(AtomType.ENTITY, 6, 11, AtomStatus.UNVERIFIABLE),
    ]
    assert rewrite_answer(answer, results) == answer


def test_rewrite_single_span_replaced_by_marker():
    answer = "keep BAD keep"
    results = [_result(AtomType.NUMBER, 5, 8, AtomStatus.NOT_FOUND)]
    expected = "keep " + REMOVAL_MARKERS[AtomType.NUMBER] + " keep"
    assert rewrite_answer(answer, results) == expected


def test_rewrite_multiple_spans_any_input_order():
    answer = "AAA mid BBB end CCC"
    results = [  # deliberately not in span order
        _result(AtomType.QUOTE, 16, 19, AtomStatus.NOT_FOUND),
        _result(AtomType.REFERENCE, 0, 3, AtomStatus.NOT_FOUND),
        _result(AtomType.ENTITY, 8, 11, AtomStatus.MISMATCHED),
    ]
    expected = (
        REMOVAL_MARKERS[AtomType.REFERENCE]
        + " mid "
        + REMOVAL_MARKERS[AtomType.ENTITY]
        + " end "
        + REMOVAL_MARKERS[AtomType.QUOTE]
    )
    assert rewrite_answer(answer, results) == expected


def test_rewrite_overlapping_false_spans_raise():
    results = [
        _result(AtomType.REFERENCE, 0, 5, AtomStatus.NOT_FOUND),
        _result(AtomType.QUOTE, 3, 8, AtomStatus.NOT_FOUND),
    ]
    with pytest.raises(ValueError):
        rewrite_answer("abcdefghij", results)


def test_rewrite_identical_false_spans_raise():
    results = [
        _result(AtomType.REFERENCE, 2, 6, AtomStatus.NOT_FOUND),
        _result(AtomType.NUMBER, 2, 6, AtomStatus.NOT_FOUND),
    ]
    with pytest.raises(ValueError):
        rewrite_answer("abcdefghij", results)


# ------------------------------------------------------------ clean answers


def test_clean_answer_verified_score_1(verifier):
    rep = verifier.verify(CLEAN_ANSWER)
    assert rep.verdict is Verdict.VERIFIED
    assert rep.score == 1.0
    assert rep.corrected_answer == CLEAN_ANSWER
    assert rep.warnings == []


def test_clean_answer_covers_all_atom_types(verifier):
    rep = verifier.verify(CLEAN_ANSWER)
    assert len(rep.atoms) == 4
    types = {r.atom.type for r in rep.atoms}
    assert types == {AtomType.REFERENCE, AtomType.NUMBER, AtomType.QUOTE, AtomType.ENTITY}
    assert all(r.status is AtomStatus.VERIFIED for r in rep.atoms)


def test_clean_answer_matched_source_is_doc_id(verifier):
    rep = verifier.verify(CLEAN_ANSWER)
    assert {r.matched_source for r in rep.atoms} == {"catalog"}


# ------------------------------------------------------------- false atoms


def test_invented_sku_removed_and_marked(verifier):
    rep = verifier.verify("The pump ZZ-9999-Q costs €249.99.")
    assert rep.verdict is Verdict.CORRECTED  # 1 verified / 2 checkable = 0.5
    assert rep.score == 0.5
    assert REMOVAL_MARKERS[AtomType.REFERENCE] in rep.corrected_answer
    assert "ZZ-9999-Q" not in rep.corrected_answer


def test_wrong_price_not_found_and_marked(verifier):
    rep = verifier.verify("The AquaPump 3000 costs €299.99.")
    numbers = [r for r in rep.atoms if r.atom.type is AtomType.NUMBER]
    assert len(numbers) == 1
    assert numbers[0].status is AtomStatus.NOT_FOUND
    assert REMOVAL_MARKERS[AtomType.NUMBER] in rep.corrected_answer
    assert "299.99" not in rep.corrected_answer
    assert rep.verdict is Verdict.CORRECTED  # entity verified, price false


def test_exact_quote_verified_via_contains_text(verifier):
    rep = verifier.verify(f'The label states "{WARRANTY}" clearly.')
    quotes = [r for r in rep.atoms if r.atom.type is AtomType.QUOTE]
    assert len(quotes) == 1
    assert quotes[0].status is AtomStatus.VERIFIED
    assert quotes[0].matched_source == "catalog"


def test_distorted_quote_not_found(verifier):
    distorted = WARRANTY.replace("24 months", "36 months")
    rep = verifier.verify(f'The label states "{distorted}" clearly.')
    quotes = [r for r in rep.atoms if r.atom.type is AtomType.QUOTE]
    assert len(quotes) == 1
    assert quotes[0].status is AtomStatus.NOT_FOUND
    assert quotes[0].detail == "quote not found verbatim in trusted corpus"
    assert REMOVAL_MARKERS[AtomType.QUOTE] in rep.corrected_answer
    assert "36 months" not in rep.corrected_answer


def test_quote_containing_sku_is_checked_as_a_whole(verifier):
    # Cross-extractor dedupe: the quote span swallows the SKU inside it; the
    # quote is false, so the WHOLE span (SKU included) is removed.
    answer = 'The doc says "install the AP-3000-X pump now" carefully.'
    rep = verifier.verify(answer)
    assert [r.atom.type for r in rep.atoms] == [AtomType.QUOTE]
    assert rep.atoms[0].status is AtomStatus.NOT_FOUND
    assert "AP-3000-X" not in rep.corrected_answer
    assert REMOVAL_MARKERS[AtomType.QUOTE] in rep.corrected_answer


# ----------------------------------------------------------------- entities


def test_entity_near_miss_mismatched_with_closest(verifier):
    rep = verifier.verify("The AquaPump 3500 is unbeatable.")
    entities = [r for r in rep.atoms if r.atom.type is AtomType.ENTITY]
    assert len(entities) == 1
    assert entities[0].status is AtomStatus.MISMATCHED
    assert entities[0].detail == "no such entity in glossary; closest known: 'AquaPump 3000'"
    assert REMOVAL_MARKERS[AtomType.ENTITY] in rep.corrected_answer
    assert "AquaPump 3500" not in rep.corrected_answer
    assert rep.verdict is Verdict.INSUFFICIENT


def test_entity_near_miss_tie_breaks_on_smaller_canonical(tmp_path):
    db = CorpusDB(tmp_path / "tie.db", create=True)
    text = "Pump 1000 and Pump 2000 are our two models."
    db.add_document("models", "models.md", text, _sha(text))
    db.add_entity(canonical_entity("Pump 2000"), "Pump 2000", "models")
    db.add_entity(canonical_entity("Pump 1000"), "Pump 1000", "models")
    db.finalize_manifest()
    rep = Verifier(db).verify("The Pump 3000 looks new.")
    entities = [r for r in rep.atoms if r.atom.type is AtomType.ENTITY]
    assert len(entities) == 1
    assert entities[0].status is AtomStatus.MISMATCHED
    # Equal ratios against both entries: lexicographically smaller wins.
    assert entities[0].detail == "no such entity in glossary; closest known: 'Pump 1000'"
    db.close()


def test_unrelated_capitalized_phrase_is_never_false(verifier):
    answer = "Best Regards from the support team."
    rep = verifier.verify(answer)
    assert all(r.status not in FALSE_STATUSES for r in rep.atoms)
    assert rep.corrected_answer == answer
    assert "Best Regards" in rep.corrected_answer


def test_entity_candidate_below_threshold_is_unverifiable(verifier):
    rep = verifier.verify("Our Pump 3000 Kit ships everywhere.")
    entities = [r for r in rep.atoms if r.atom.type is AtomType.ENTITY]
    assert len(entities) == 1
    assert entities[0].status is AtomStatus.UNVERIFIABLE
    assert entities[0].detail == "not in glossary, no close match — cannot verify"
    assert "Pump 3000 Kit" in rep.corrected_answer  # never removed


def test_glossary_drift_is_not_found_defensively(tmp_path):
    db = CorpusDB(tmp_path / "drift.db", create=True)
    text = "AquaPump 3000 maintenance notes."
    db.add_document("g1", "g.md", text, _sha(text))
    db.add_entity(canonical_entity("AquaPump 3000"), "AquaPump 3000", "g1")
    db.finalize_manifest()
    v = Verifier(db)
    db.delete_document("g1")  # corpus drifts AFTER the Verifier snapshot
    rep = v.verify("The AquaPump 3000 still exists.")
    entities = [r for r in rep.atoms if r.atom.type is AtomType.ENTITY]
    assert len(entities) == 1
    assert entities[0].atom.pack == "glossary"
    assert entities[0].status is AtomStatus.NOT_FOUND
    assert REMOVAL_MARKERS[AtomType.ENTITY] in rep.corrected_answer
    db.close()


# ----------------------------------------------- F3 regression / repetition


def test_f3_multi_format_regression_every_occurrence_replaced(verifier):
    answer = "See [REF: X-99] then [X-99] then (X-99) then bare X-99 here."
    rep = verifier.verify(answer)
    refs = [r for r in rep.atoms if r.atom.type is AtomType.REFERENCE]
    assert len(refs) == 4
    assert all(r.status is AtomStatus.NOT_FOUND for r in refs)
    corrected = rep.corrected_answer
    assert "X-99" not in corrected
    assert "[REF:" not in corrected
    assert corrected.count(REMOVAL_MARKERS[AtomType.REFERENCE]) == 4
    assert rep.verdict is Verdict.INSUFFICIENT


def test_repeated_false_atom_all_occurrences_replaced(verifier):
    rep = verifier.verify("Check ZZ-9999-Q, then ZZ-9999-Q, then ZZ-9999-Q.")
    assert len(rep.atoms) == 3
    assert all(r.status is AtomStatus.NOT_FOUND for r in rep.atoms)
    assert "ZZ-9999-Q" not in rep.corrected_answer
    assert rep.corrected_answer.count(REMOVAL_MARKERS[AtomType.REFERENCE]) == 3
    assert rep.score == 0.0


# -------------------------------------------------------- score and verdict


def test_pure_prose_is_unverifiable_never_vacuous_100(verifier):
    answer = "The pump works well and customers are happy with it."
    rep = verifier.verify(answer)
    assert rep.atoms == []
    assert rep.verdict is Verdict.UNVERIFIABLE
    assert rep.score == 0.0
    assert rep.warnings == [
        "No verifiable atoms found; nothing in this answer could be checked "
        "against the corpus."
    ]
    assert rep.corrected_answer == answer


def test_score_excludes_unverifiable_atoms(verifier):
    rep = verifier.verify("Order the AP-3000-X with the Pump 3000 Kit.")
    assert rep.n_verified == 1
    assert rep.n_unverifiable == 1
    assert rep.n_false == 0
    assert rep.score == 1.0  # not diluted by the unverifiable entity (D-003)
    assert rep.verdict is Verdict.VERIFIED


def test_verdict_corrected_at_exactly_half(verifier):
    rep = verifier.verify("Order the AP-3000-X alongside ZZ-9999-Q parts.")
    assert rep.n_verified == 1
    assert rep.n_false == 1
    assert rep.score == 0.5
    assert rep.verdict is Verdict.CORRECTED


def test_verdict_insufficient_below_half(verifier):
    rep = verifier.verify("Order the AP-3000-X alongside ZZ-9999-Q and ZZ-9999-Q parts.")
    assert rep.n_verified == 1
    assert rep.n_false == 2
    assert rep.verdict is Verdict.INSUFFICIENT


# ----------------------------------------------------------------- context

CONTEXT_CHUNK = (
    "Heater HM-7700-Z costs €119.00; the manual says you may return it "
    "within 14 days of delivery."
)
CONTEXT_ANSWER = (
    'Model HM-7700-Z costs €119.00 and "you may return it within 14 days" applies.'
)


def test_context_grounds_ref_number_and_quote(verifier):
    rep = verifier.verify(CONTEXT_ANSWER, context=[CONTEXT_CHUNK])
    assert rep.verdict is Verdict.VERIFIED
    assert rep.score == 1.0
    by_type = {r.atom.type: r for r in rep.atoms}
    assert set(by_type) == {AtomType.REFERENCE, AtomType.NUMBER, AtomType.QUOTE}
    for r in rep.atoms:
        assert r.status is AtomStatus.VERIFIED
        assert r.matched_source == "context"


def test_same_answer_without_context_is_false(verifier):
    rep = verifier.verify(CONTEXT_ANSWER)
    assert rep.n_false == 3
    assert rep.score == 0.0
    assert rep.verdict is Verdict.INSUFFICIENT


# ------------------------------------------------------------- determinism


def test_two_calls_are_byte_identical(verifier):
    answer = "Order the AP-3000-X alongside ZZ-9999-Q parts."
    assert verifier.verify(answer).to_json() == verifier.verify(answer).to_json()


def test_two_identical_corpora_are_byte_identical(tmp_path):
    db1 = build_catalog_corpus(tmp_path / "a.db")
    db2 = build_catalog_corpus(tmp_path / "b.db")
    answer = CLEAN_ANSWER + " But ZZ-9999-Q is fake."
    assert Verifier(db1).verify(answer).to_json() == Verifier(db2).verify(answer).to_json()
    db1.close()
    db2.close()


def test_unicode_before_false_atom_marker_lands_exactly(verifier):
    answer = "Voici un café ☕ avec des émojis 🚀🎉 puis ZZ-9999-Q à la fin."
    rep = verifier.verify(answer)
    expected = answer.replace("ZZ-9999-Q", REMOVAL_MARKERS[AtomType.REFERENCE])
    assert rep.corrected_answer == expected


# ----------------------------------------------------------- empty answers


def test_empty_answer_unverifiable(verifier):
    rep = verifier.verify("")
    assert rep.verdict is Verdict.UNVERIFIABLE
    assert rep.score == 0.0
    assert rep.atoms == []
    assert rep.warnings == ["empty answer"]
    assert rep.corrected_answer == ""


def test_whitespace_only_answer_unverifiable(verifier):
    rep = verifier.verify("   \n\t  ")
    assert rep.verdict is Verdict.UNVERIFIABLE
    assert rep.warnings == ["empty answer"]
    assert rep.corrected_answer == "   \n\t  "


# ------------------------------------------------------- report provenance


def test_report_sha256_and_corpus_fingerprint(corpus, verifier):
    rep = verifier.verify(CLEAN_ANSWER)
    assert rep.answer_sha256 == hashlib.sha256(CLEAN_ANSWER.encode("utf-8")).hexdigest()
    assert rep.corpus_fingerprint == corpus.fingerprint()
    assert rep.corpus_fingerprint != ""


# ------------------------------------------------------------------ config


def test_quote_min_words_config(corpus):
    answer = '"24 months" is the warranty period offered.'
    default_rep = Verifier(corpus).verify(answer)
    assert not any(r.atom.type is AtomType.QUOTE for r in default_rep.atoms)
    rep = Verifier(corpus, VerifyConfig(quote_min_words=2)).verify(answer)
    quotes = [r for r in rep.atoms if r.atom.type is AtomType.QUOTE]
    assert len(quotes) == 1
    assert quotes[0].status is AtomStatus.VERIFIED


def test_packs_config_limits_reference_extraction(corpus):
    rep = Verifier(corpus, VerifyConfig(packs=("urls",))).verify("Order ZZ-9999-Q today.")
    assert not any(r.atom.type is AtomType.REFERENCE for r in rep.atoms)


# -------------------------------------------------------------------- misc


def test_full_pipeline_offline(no_network, tmp_path):
    db = build_catalog_corpus(tmp_path / "net.db")
    rep = Verifier(db).verify(CLEAN_ANSWER + " But ZZ-9999-Q is fake.")
    assert rep.verdict is Verdict.CORRECTED  # 4 verified / 5 checkable
    assert rep.score == 0.8
    db.close()
