"""Tests for the quote extractor and the glossary-entity extractor.

All tests are deterministic and offline (both extractors are pure functions
of their input; the stray-quote end-to-end regression builds a throwaway
CorpusDB in tmp_path).
"""

from __future__ import annotations

import hashlib

from verigate.canonical import canonical_text
from verigate.corpus import CorpusDB
from verigate.extract.entities import EntityExtractor
from verigate.extract.quotes import QuoteExtractor
from verigate.types import REMOVAL_MARKERS, AtomType, Verdict
from verigate.verify.engine import Verifier

GLOSSARY = [
    ("aquapump 3000", "AquaPump 3000"),
    ("hydrofilter mini", "HydroFilter Mini"),
    ("mega pompe", "Méga Pompe"),
]


# ---------------------------------------------------------------- quotes


def test_straight_double_quotes():
    text = 'He said "the pump is great" today.'
    atoms = QuoteExtractor().extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.type is AtomType.QUOTE
    assert a.pack == "quotes"
    assert a.raw == '"the pump is great"'
    assert a.canonical == canonical_text("the pump is great")


def test_typographic_quotes():
    text = "Il annonce “une garantie de deux ans” fièrement."
    atoms = QuoteExtractor().extract(text)
    assert len(atoms) == 1
    assert atoms[0].raw == "“une garantie de deux ans”"
    assert atoms[0].canonical == canonical_text("une garantie de deux ans")


def test_french_guillemets():
    text = "La fiche dit « garantie de 24 mois » noir sur blanc."
    atoms = QuoteExtractor().extract(text)
    assert len(atoms) == 1
    assert atoms[0].raw == "« garantie de 24 mois »"


def test_french_guillemets_nbsp():
    text = "Voir « garantie 24 mois » ici."
    atoms = QuoteExtractor().extract(text)
    assert len(atoms) == 1
    assert atoms[0].canonical == canonical_text("garantie 24 mois")


def test_two_word_quote_ignored():
    assert QuoteExtractor().extract('He said "two words" here.') == []


def test_three_word_quote_extracted():
    atoms = QuoteExtractor().extract('He said "three little words" here.')
    assert len(atoms) == 1
    assert atoms[0].raw == '"three little words"'


def test_min_words_is_configurable():
    atoms = QuoteExtractor(min_words=2).extract('He said "two words" here.')
    assert len(atoms) == 1


def test_span_includes_quote_marks():
    text = 'prefix "alpha beta gamma" suffix'
    a = QuoteExtractor().extract(text)[0]
    assert text[a.start] == '"'
    assert text[a.end - 1] == '"'
    assert text[a.start : a.end] == a.raw


def test_unbalanced_quote_no_atom():
    assert QuoteExtractor().extract('He said "never closed and went on.') == []


def test_quote_over_600_chars_no_atom():
    inner = "word " * 121  # 605 chars of inner text
    text = f'"{inner}" tail'
    assert QuoteExtractor().extract(text) == []


def test_quote_exactly_600_chars_extracted():
    inner = "word " * 119 + "endee"  # exactly 600 chars
    assert len(inner) == 600
    atoms = QuoteExtractor().extract(f'"{inner}"')
    assert len(atoms) == 1
    assert atoms[0].raw == f'"{inner}"'


def test_canonical_equality_garantie():
    text = "Texte « Garantie : 24 mois ! » fin."
    a = QuoteExtractor().extract(text)[0]
    assert a.canonical == canonical_text("garantie 24 mois")


def test_typographic_not_closed_by_straight():
    text = 'He wrote “alpha beta gamma" and stopped.'
    assert QuoteExtractor().extract(text) == []


def test_straight_not_closed_by_typographic():
    text = "He wrote \"alpha beta gamma” and stopped."
    assert QuoteExtractor().extract(text) == []


def test_guillemet_requires_guillemet_closer():
    text = 'Il écrit « alpha beta gamma" et stop.'
    assert QuoteExtractor().extract(text) == []


def test_straight_pairs_with_next_straight():
    text = '"one two three" mid "four five six"'
    atoms = QuoteExtractor().extract(text)
    assert [a.raw for a in atoms] == ['"one two three"', '"four five six"']


def test_single_quotes_not_supported():
    assert QuoteExtractor().extract("He said 'three little words' here.") == []


def test_short_quote_consumed_not_repaired():
    # The closer of an ignored short quote must not reopen a phantom span.
    text = '"hi" and "three little words"'
    atoms = QuoteExtractor().extract(text)
    assert len(atoms) == 1
    assert atoms[0].raw == '"three little words"'


def test_quotes_deterministic():
    text = 'a "one two three" et « quatre cinq six » b'
    extractor = QuoteExtractor()
    assert extractor.extract(text) == extractor.extract(text)


# ------------------------------------------- stray straight-quote regression
#
# A straight " is also the ASCII inch/second/ditto mark. Regression for the
# review finding: a stray mark (6") mispaired with a genuine quote's opener,
# so the innocent PROSE between them became the QUOTE atom (removed as
# unverified) while the real quoted claim escaped verification. Conservative
# contract: a mark glued to an alphanumeric can never OPEN, and an ODD count
# of plausible straight delimiters disables straight-quote extraction
# entirely (deleting innocent prose is strictly worse than leaving a quote
# unchecked — D-003 spirit).

STRAY_INCH_ANSWER = (
    'The pipe is 6" wide. The manual says '
    '"install the pump vertically for best results".'
)


def test_stray_inch_mark_extracts_no_straight_quote():
    # 3 plausible straight delimiters (one stray closer + one real pair):
    # odd count -> no straight-quote atom at all, never mispaired prose.
    assert QuoteExtractor().extract(STRAY_INCH_ANSWER) == []


def test_inch_mark_alone_cannot_open_a_quote():
    # Glued to a digit, the mark can never OPEN: the prose after it must not
    # become a quote even though a whitespace-preceded " would.
    text = 'The pipe is 6" wide and very robust today.'
    assert QuoteExtractor().extract(text) == []


def test_lone_unattached_quote_is_not_a_delimiter():
    # A " surrounded by spaces can neither open nor close: it does not count
    # toward parity and must not poison a well-formed pair.
    text = 'odd mark " here, then "three little words" follow.'
    atoms = QuoteExtractor().extract(text)
    assert [a.raw for a in atoms] == ['"three little words"']


def test_even_inch_marks_do_not_block_a_real_quote():
    # Two inch marks (both closer-plausible) keep the count even: the real
    # pair is still extracted and the marks are never treated as openers.
    text = 'Pipes of 6" and 8" exist. He said "the pump is great" today.'
    atoms = QuoteExtractor().extract(text)
    assert [a.raw for a in atoms] == ['"the pump is great"']


def test_typographic_and_guillemets_unaffected_by_stray_straight():
    # Layer (b) only disables STRAIGHT quotes: “…” and «…» keep extracting.
    text = 'Tube 6" requis. Il dit « garantie de 24 mois » et “alpha beta gamma”.'
    atoms = QuoteExtractor().extract(text)
    assert [a.raw for a in atoms] == [
        "« garantie de 24 mois »",
        "“alpha beta gamma”",
    ]


def test_stray_inch_mark_regression_end_to_end(tmp_path):
    """The report's exact repro: corpus-grounded quote + stray inch mark.

    Before the fix the verifier returned corrected_answer
    'The pipe is 6⟨unverified quote, removed⟩install the pump …".' —
    innocent prose deleted, genuine quote left unchecked with a dangling ".
    After the fix the answer must pass through untouched (no quote atom).
    """
    doc_text = "Mounting: install the pump vertically for best results."
    db = CorpusDB(tmp_path / "corpus.db", create=True)
    db.add_document(
        "products",
        "products.md",
        doc_text,
        hashlib.sha256(doc_text.encode("utf-8")).hexdigest(),
    )
    db.finalize_manifest()
    verifier = Verifier(db)

    rep = verifier.verify(STRAY_INCH_ANSWER)
    assert rep.corrected_answer == STRAY_INCH_ANSWER
    assert REMOVAL_MARKERS[AtomType.QUOTE] not in rep.corrected_answer
    assert not any(r.atom.type is AtomType.QUOTE for r in rep.atoms)
    # The innocent prose the bug used to delete is intact.
    assert ' wide. The manual says ' in rep.corrected_answer
    assert "install the pump vertically for best results" in rep.corrected_answer

    # Control: without the stray mark the same quote is extracted and
    # VERIFIED — the guard did not neuter straight-quote checking.
    control = 'The manual says "install the pump vertically for best results".'
    rep2 = verifier.verify(control)
    quotes = [r for r in rep2.atoms if r.atom.type is AtomType.QUOTE]
    assert len(quotes) == 1
    assert rep2.verdict is Verdict.VERIFIED
    db.close()


# --------------------------------------------------------------- entities


def test_glossary_exact_match():
    text = "The AquaPump 3000 ships today."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.type is AtomType.ENTITY
    assert a.pack == "glossary"
    assert a.raw == "AquaPump 3000"
    assert a.canonical == "aquapump 3000"
    assert text[a.start : a.end] == a.raw


def test_accent_drift_text_accented_span_original():
    text = "Commandez la Méga Pompe dès maintenant."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.pack == "glossary"
    assert a.raw == "Méga Pompe"
    assert text[a.start : a.end] == "Méga Pompe"
    assert a.canonical == "mega pompe"


def test_accent_drift_text_unaccented():
    # Glossary display 'Méga Pompe' found in plain-ASCII lowercase prose.
    text = "oui, la mega pompe est en stock."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    assert atoms[0].pack == "glossary"
    assert atoms[0].raw == "mega pompe"
    assert atoms[0].canonical == "mega pompe"


def test_double_space_drift_matches():
    text = "Buy AquaPump  3000 now."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.pack == "glossary"
    assert a.raw == "AquaPump  3000"
    assert text[a.start : a.end] == a.raw


def test_lowercase_text_matches():
    text = "the aquapump 3000 is cheap"
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    assert atoms[0].pack == "glossary"
    assert atoms[0].raw == "aquapump 3000"


def test_hyphen_inside_token_is_candidate_not_glossary():
    # 'Aqua-Pump 3000' splits the first canonical token: not a glossary
    # match, but plausibly related (shared token '3000') → candidate.
    text = "nous vendons la Aqua-Pump 3000 ici."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.pack == "glossary_candidate"
    assert a.raw == "Aqua-Pump 3000"


def test_token_boundary_respected():
    text = "the superaquapump 3000 is fake"
    assert EntityExtractor(GLOSSARY).extract(text) == []


def test_near_miss_candidate():
    text = "nous recommandons la AquaPump 3500 pour ce bassin."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.type is AtomType.ENTITY
    assert a.pack == "glossary_candidate"
    assert a.raw == "AquaPump 3500"
    assert a.canonical == "aquapump 3500"


def test_ratio_only_near_miss():
    # No shared canonical token with 'hydrofilter mini', but the
    # SequenceMatcher ratio clears the 0.72 threshold.
    text = "le HydroFilterMini Pro est arrivé"
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    assert atoms[0].pack == "glossary_candidate"
    assert atoms[0].raw == "HydroFilterMini Pro"


def test_unrelated_capitalized_not_emitted():
    text = "Best Regards from New York"
    assert EntityExtractor(GLOSSARY).extract(text) == []


def test_leading_the_stripped():
    text = "The AquaPump is our best seller."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.pack == "glossary_candidate"
    assert a.raw == "AquaPump"
    assert text[a.start : a.end] == "AquaPump"


def test_leading_french_stopword_stripped():
    text = "voici La AquaPump 350 en stock."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.pack == "glossary_candidate"
    assert a.raw == "AquaPump 350"


def test_empty_glossary_returns_nothing():
    assert EntityExtractor([]).extract("The AquaPump 3000 and Best Regards") == []


def test_exact_glossary_match_wins_over_candidate():
    # 'AquaPump 3000' satisfies the candidate regex too; the glossary atom
    # must win the tie.
    text = "order AquaPump 3000 today"
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    assert atoms[0].pack == "glossary"


def test_entity_spans_index_original_text():
    text = "La Méga Pompe, l'AquaPump 3000 et l'AquaPump 3500 — voilà."
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert atoms
    for a in atoms:
        assert text[a.start : a.end] == a.raw


#: BDPM-style glossary: long official forms ('…, gélule') — the model
#: typically writes the name without the pharmaceutical-form tail (D-015).
BDPM_GLOSSARY = [
    (
        "fenofibrate teva sante 200 mg gelule",
        "FENOFIBRATE TEVA SANTE 200 mg, gélule",
    ),
    (
        "pregabaline biogaran 25 mg gelule",
        "PREGABALINE BIOGARAN 25 mg, gélule",
    ),
]


def test_dose_suffix_extends_candidate_span():
    # Real-data eval FP: the candidate used to stop at '… 200', truncating
    # the lowercase dose suffix out of the product name.
    text = "Le FENOFIBRATE TEVA SANTE 200 mg est commercialisé."
    atoms = EntityExtractor(BDPM_GLOSSARY).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.pack == "glossary_candidate"
    assert a.raw == "FENOFIBRATE TEVA SANTE 200 mg"
    assert text[a.start : a.end] == a.raw
    assert a.canonical == "fenofibrate teva sante 200 mg"


def test_glued_dose_token_already_in_span():
    # '25mg' starts with a digit: the candidate regex includes it without
    # any extension.
    text = "La PREGABALINE BIOGARAN 25mg est disponible."
    atoms = EntityExtractor(BDPM_GLOSSARY).extract(text)
    assert len(atoms) == 1
    assert atoms[0].raw == "PREGABALINE BIOGARAN 25mg"


def test_micro_sign_suffix_extends_candidate_span():
    # 'µ' (MICRO SIGN) folds to 'μ' (GREEK MU) in the ASCII shadow; the
    # span still indexes the original text.
    glossary = [("levothyrox ab 200 g comprime", "LEVOTHYROX AB 200 µg, comprimé")]
    text = "le LEVOTHYROX AB 200 µg reste indiqué."
    atoms = EntityExtractor(glossary).extract(text)
    assert len(atoms) == 1
    a = atoms[0]
    assert a.raw == "LEVOTHYROX AB 200 µg"
    assert text[a.start : a.end] == a.raw


def test_dose_suffix_requires_trailing_digit():
    # 'Pro' does not end with a digit: a following lowercase unit-like
    # token is prose, not a posology suffix.
    text = "voici AquaPump Pro mg en stock"
    atoms = EntityExtractor(GLOSSARY).extract(text)
    assert len(atoms) == 1
    assert atoms[0].raw == "AquaPump Pro"


def test_dose_suffix_must_be_whole_token():
    # 'mgx' is not a dose-unit token: no extension.
    text = "Le FENOFIBRATE TEVA SANTE 200 mgx existe."
    atoms = EntityExtractor(BDPM_GLOSSARY).extract(text)
    assert len(atoms) == 1
    assert atoms[0].raw == "FENOFIBRATE TEVA SANTE 200"


def test_entities_deterministic():
    text = "voici La AquaPump 350, la Méga Pompe et un HydroFilter Mini."
    extractor = EntityExtractor(GLOSSARY)
    assert extractor.extract(text) == extractor.extract(text)
