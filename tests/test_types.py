"""Core types: deterministic serialization, span dedup, verdict invariants."""

from __future__ import annotations

from verigate.canonical import (
    canonical_entity,
    canonical_number,
    canonical_ref,
    canonical_text,
)
from verigate.extract.base import dedupe_overlapping
from verigate.types import (
    Atom,
    AtomResult,
    AtomStatus,
    AtomType,
    Report,
    Verdict,
)


def _atom(start: int, end: int, raw: str = "X") -> Atom:
    return Atom(
        type=AtomType.REFERENCE, raw=raw, canonical=raw.upper(), start=start, end=end
    )


class TestReportSerialization:
    def test_to_json_is_deterministic(self):
        r1 = Report(
            verdict=Verdict.CORRECTED,
            score=0.5,
            atoms=[AtomResult(atom=_atom(0, 3), status=AtomStatus.VERIFIED)],
            corrected_answer="ok",
            answer_sha256="ab" * 32,
            corpus_fingerprint="cd" * 32,
        )
        r2 = Report(
            verdict=Verdict.CORRECTED,
            score=0.5,
            atoms=[AtomResult(atom=_atom(0, 3), status=AtomStatus.VERIFIED)],
            corrected_answer="ok",
            answer_sha256="ab" * 32,
            corpus_fingerprint="cd" * 32,
        )
        assert r1.to_json() == r2.to_json()
        assert r1.to_json().encode("utf-8") == r2.to_json().encode("utf-8")

    def test_counts(self):
        r = Report(
            verdict=Verdict.CORRECTED,
            score=0.5,
            atoms=[
                AtomResult(atom=_atom(0, 1), status=AtomStatus.VERIFIED),
                AtomResult(atom=_atom(2, 3), status=AtomStatus.NOT_FOUND),
                AtomResult(atom=_atom(4, 5), status=AtomStatus.MISMATCHED),
                AtomResult(atom=_atom(6, 7), status=AtomStatus.UNVERIFIABLE),
            ],
        )
        assert r.n_verified == 1
        assert r.n_false == 2
        assert r.n_unverifiable == 1
        assert r.to_dict()["counts"]["total"] == 4


class TestCanonical:
    def test_ref_variants_share_a_key(self):
        assert canonical_ref("L. 1233-3") == canonical_ref("L1233-3")
        assert canonical_ref("ap-3000-x") == canonical_ref("AP 3000 X")

    def test_text_is_punctuation_and_accent_insensitive(self):
        assert canonical_text("Garantie : 24 mois !") == canonical_text(
            "garantie 24 mois"
        )
        assert canonical_text("élève") == canonical_text("eleve")

    def test_entity_keeps_word_boundaries(self):
        assert canonical_entity("AquaPump  3000") == "aquapump 3000"
        assert canonical_entity("Aqua-Pump 3000") == "aqua pump 3000"

    def test_number_formats_converge(self):
        assert canonical_number("1 234,50") == "1234.5"
        assert canonical_number("1,234.50") == "1234.5"
        assert canonical_number("49,99") == "49.99"
        assert canonical_number("12,345") == "12345"
        assert canonical_number("200.0") == "200"
        assert canonical_number("249.99") == "249.99"

    def test_french_all_comma_amounts(self):
        # BDPM-style ≥ 1000 € amounts: thousands commas plus a decimal
        # comma — the 2-digit last group marks the decimal.
        assert canonical_number("3,284,71") == "3284.71"
        assert canonical_number("12,345,67") == "12345.67"
        assert canonical_number("1,234,50") == "1234.5"
        # A 3-digit last group stays all-thousands.
        assert canonical_number("1,234,567") == "1234567"


class TestDedupeOverlapping:
    def test_longest_span_wins(self):
        inner = _atom(6, 9, raw="X-1")
        outer = _atom(0, 10, raw="[REF: X-1]")
        kept = dedupe_overlapping([inner, outer])
        assert kept == [outer]

    def test_disjoint_spans_all_kept_in_order(self):
        a, b = _atom(10, 12), _atom(0, 2)
        assert dedupe_overlapping([a, b]) == [b, a]

    def test_equal_length_first_registered_wins(self):
        a = Atom(AtomType.REFERENCE, "AB", "AB", 0, 2)
        b = Atom(AtomType.ENTITY, "AB", "ab", 0, 2)
        assert dedupe_overlapping([a, b]) == [a]
        assert dedupe_overlapping([b, a]) == [b]
