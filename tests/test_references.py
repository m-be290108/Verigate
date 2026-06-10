"""Tests for the pack-driven reference extractor.

All tests are deterministic and offline. Every extraction test asserts span
exactness: ``text[a.start:a.end] == a.raw`` for every atom (D-001 — spans
include delimiters so removal-by-span leaves no dangling syntax).
"""

from __future__ import annotations

import pytest

from verigate.canonical import canonical_ref
from verigate.extract.references import (
    PackError,
    ReferenceExtractor,
    builtin_pack_names,
    extract_references,
    load_pack,
)
from verigate.types import AtomType


def assert_spans(text, atoms):
    """Every atom's raw must be exactly the text slice at its span."""
    for a in atoms:
        assert text[a.start : a.end] == a.raw
        assert a.type is AtomType.REFERENCE


# ------------------------------------------------------------- pack loading


def test_builtin_pack_names_sorted():
    assert builtin_pack_names() == ["doi_iso", "generic", "legal_fr", "sku_ean", "urls"]


def test_load_builtin_pack():
    pack = load_pack("generic")
    assert pack.name == "generic"
    assert [p.id for p in pack.patterns] == ["code"]


def test_unknown_builtin_pack_error_lists_available():
    with pytest.raises(PackError, match="generic"):
        load_pack("klingon")


def test_missing_file_pack_error(tmp_path):
    with pytest.raises(PackError, match="not readable"):
        load_pack(tmp_path / "nope.yaml")


def test_broken_yaml_pack_error(tmp_path):
    p = tmp_path / "broken.yaml"
    p.write_text("name: [unclosed\npatterns: ::\n", encoding="utf-8")
    with pytest.raises(PackError, match="invalid YAML"):
        load_pack(p)


def test_missing_patterns_key_pack_error(tmp_path):
    p = tmp_path / "nopatterns.yaml"
    p.write_text("name: nopatterns\ndescription: oops\n", encoding="utf-8")
    with pytest.raises(PackError, match="patterns"):
        load_pack(p)


def test_bad_regex_pack_error_names_pattern(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "name: bad\npatterns:\n  - id: busted\n    regex: '([unclosed'\n",
        encoding="utf-8",
    )
    with pytest.raises(PackError, match="busted"):
        load_pack(p)


def test_custom_pack_from_tmp_path(tmp_path):
    p = tmp_path / "tickets.yaml"
    p.write_text(
        "name: tickets\n"
        "description: internal ticket ids\n"
        "patterns:\n"
        "  - id: tkt\n"
        "    regex: '\\bTKT-[0-9]{3,6}\\b'\n",
        encoding="utf-8",
    )
    pack = load_pack(p)
    assert pack.name == "tickets"
    text = "Escalated as TKT-4521 yesterday."
    atoms = ReferenceExtractor([pack]).extract(text)
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["TKT-4521"]
    assert atoms[0].pack == "tickets:tkt"
    assert atoms[0].canonical == canonical_ref("TKT-4521")


# --------------------------------------------------------- layer (a): [REF:]


def test_ref_tag_with_no_packs_loaded():
    text = "Voir [REF: AP-3000-X] pour le détail."
    atoms = ReferenceExtractor([]).extract(text)
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].pack == "ref_tag"
    assert atoms[0].canonical == canonical_ref("AP-3000-X")


def test_ref_tag_span_includes_brackets():
    text = "See [REF: DOC_2024_117] above."
    atoms = ReferenceExtractor([]).extract(text)
    assert_spans(text, atoms)
    assert atoms[0].raw == "[REF: DOC_2024_117]"
    assert atoms[0].start == text.index("[")
    assert atoms[0].end == text.index("]") + 1


def test_ref_tag_unmatchable_inner_is_still_an_atom():
    text = "Comme prévu [REF: see appendix B] ici."
    atoms = extract_references(text)
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].pack == "ref_tag"
    assert atoms[0].canonical == canonical_ref("see appendix B")


def test_ref_tag_dedupe_yields_exactly_one_atom():
    # The bare generic code inside the tag must not survive dedupe.
    text = "[REF: AP-3000-X]"
    atoms = extract_references(text)
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].pack == "ref_tag"
    assert atoms[0].raw == "[REF: AP-3000-X]"


# ------------------------------------------- layer (b): [inner] and (inner)


def test_bracketed_code_matches_pack():
    text = "Référence produit [AP-3000-X] au catalogue."
    atoms = extract_references(text)
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].raw == "[AP-3000-X]"  # delimiters included
    assert atoms[0].pack == "generic:code"  # first pack in sorted order wins
    assert atoms[0].canonical == canonical_ref("AP-3000-X")


def test_paren_legal_ref_matches_pack():
    text = "Le licenciement économique (L.1233-3) est encadré."
    atoms = extract_references(text)
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].raw == "(L.1233-3)"
    assert atoms[0].pack == "legal_fr:prefixed_article"
    assert atoms[0].canonical == canonical_ref("L.1233-3")


def test_bracketed_prose_not_extracted():
    assert extract_references("As noted [hello world] before.") == []


def test_footnote_marker_not_extracted():
    assert extract_references("As shown in [12], the result holds.") == []


def test_paren_prose_not_extracted():
    assert extract_references("This point (see above) is settled.") == []


def test_url_in_parens_keeps_delimiters():
    text = "Le support (https://intranet.example/support) répond vite."
    atoms = extract_references(text)
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].raw == "(https://intranet.example/support)"
    assert atoms[0].pack == "urls:http"


# ----------------------------------------------------- layer (c): bare prose


def test_bare_code_in_prose():
    text = "Commandez la AP-3000-X aujourd'hui."
    atoms = extract_references(text, ["generic"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["AP-3000-X"]
    assert atoms[0].pack == "generic:code"


def test_f2_plural_enumeration_yields_both_refs():
    # Beaume audit F2: enumerated articles must ALL be extracted.
    text = "Les articles L.8888-1 et L.7777-2 s'appliquent."
    atoms = extract_references(text)
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["L.8888-1", "L.7777-2"]
    assert [a.canonical for a in atoms] == [
        canonical_ref("L.8888-1"),
        canonical_ref("L.7777-2"),
    ]


def test_cass_case_law_keyed_on_pourvoi_number():
    text = "Cass. soc., 12 mai 2021, n°19-12.345 a cassé l'arrêt."
    atoms = extract_references(text, ["legal_fr"])
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].raw == "Cass. soc., 12 mai 2021, n°19-12.345"
    assert atoms[0].canonical == canonical_ref("19-12.345")
    assert atoms[0].pack == "legal_fr:cass_case_law"


def test_cour_de_cassation_variant():
    text = "La Cour de cassation, n° 21-10.625, a confirmé."
    atoms = extract_references(text, ["legal_fr"])
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].raw == "Cour de cassation, n° 21-10.625"
    assert atoms[0].canonical == canonical_ref("21-10.625")


def test_prose_article_du_code_keys_on_number_spans_phrase():
    text = "La responsabilité découle de l'article 1240 du code civil, c'est acquis."
    atoms = extract_references(text, ["legal_fr"])
    assert_spans(text, atoms)
    assert len(atoms) == 1
    assert atoms[0].raw == "article 1240 du code civil"
    assert atoms[0].canonical == canonical_ref("1240")
    assert atoms[0].pack == "legal_fr:prose_article_du_code"


def test_midword_code_not_extracted():
    assert extract_references("Le module CAROUSEL.1233-3 est interne.") == []


def test_french_prose_noise_not_extracted():
    assert extract_references("Il y a 1234 raisons d'y croire.") == []


def test_generic_rejects_hyphenated_prose():
    assert extract_references("A well-known, state-of-the-art approach.") == []


# ------------------------------------------------------------ sku_ean pack


def test_ean13_wrong_checksum_still_extracted():
    # Checksum is deliberately not validated at extraction (corpus decides).
    text = "Code-barres 4006381333931 en stock."  # checksum digit is wrong
    atoms = extract_references(text, ["sku_ean"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["4006381333931"]
    assert atoms[0].pack == "sku_ean:ean13"


def test_twelve_digit_run_not_extracted():
    assert extract_references("Code 400638133393 en stock.", ["sku_ean"]) == []


def test_ean8_extracted():
    text = "EAN 96385074 indiqué sur l'emballage."
    atoms = extract_references(text, ["sku_ean"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["96385074"]
    assert atoms[0].pack == "sku_ean:ean8"


def test_sku_prefix_optional_with_equal_canonical():
    t1 = "Réapprovisionner SKU AP-3000-X au dépôt."
    t2 = "Réapprovisionner AP-3000-X au dépôt."
    a1 = extract_references(t1, ["sku_ean"])
    a2 = extract_references(t2, ["sku_ean"])
    assert_spans(t1, a1)
    assert_spans(t2, a2)
    assert len(a1) == len(a2) == 1
    assert a1[0].raw == "SKU AP-3000-X"  # span covers the prefix
    assert a2[0].raw == "AP-3000-X"
    assert a1[0].canonical == a2[0].canonical == canonical_ref("AP-3000-X")


# ------------------------------------------------------------ doi_iso pack


def test_doi_trailing_paren_not_swallowed():
    text = "(voir 10.1000/182) ensuite."
    atoms = extract_references(text, ["doi_iso"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["10.1000/182"]
    assert atoms[0].pack == "doi_iso:doi"


def test_doi_trailing_dot_not_swallowed():
    text = "Cité : 10.1016/j.cell.2023.01.001."
    atoms = extract_references(text, ["doi_iso"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["10.1016/j.cell.2023.01.001"]


def test_doi_internal_parens_kept():
    text = "Étude 10.1016/S0140-6736(20)31142-9 dans le Lancet."
    atoms = extract_references(text, ["doi_iso"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["10.1016/S0140-6736(20)31142-9"]


def test_iso_and_iso_iec():
    text = "Conforme à ISO/IEC 27001 et ISO 9001:2015 depuis 2019."
    atoms = extract_references(text, ["doi_iso"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["ISO/IEC 27001", "ISO 9001:2015"]
    assert all(a.pack == "doi_iso:iso" for a in atoms)


def test_rfc_numbers():
    text = "Voir RFC 7231 et RFC-9110 pour HTTP."
    atoms = extract_references(text)
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["RFC 7231", "RFC-9110"]
    # RFC-9110 also matches generic:code at the same span; doi_iso is
    # registered first (sorted order), so the rfc pattern wins the tie.
    assert [a.pack for a in atoms] == ["doi_iso:rfc", "doi_iso:rfc"]


# --------------------------------------------------------------- urls pack


def test_url_trailing_dot_not_swallowed():
    text = "Voir https://intranet.example/support. Merci."
    atoms = extract_references(text, ["urls"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["https://intranet.example/support"]
    assert atoms[0].pack == "urls:http"


# ------------------------------------------------------------- integration


def test_canonical_ref_drift_equality():
    assert canonical_ref("AP 3000 X") == canonical_ref("ap-3000-x") == "AP3000X"


def test_mixed_answer_spans_and_canonicals(no_network):
    text = (
        "Order the AquaPump 3000 (SKU AP-3000-X) today: see [REF: DOC_2024_117] "
        "and https://intranet.example/support. Compliance: ISO 9001:2015, "
        "per article 1240 du code civil."
    )
    atoms = extract_references(text)
    assert_spans(text, atoms)
    assert [(a.raw, a.canonical, a.pack) for a in atoms] == [
        ("(SKU AP-3000-X)", canonical_ref("AP-3000-X"), "sku_ean:sku"),
        ("[REF: DOC_2024_117]", canonical_ref("DOC_2024_117"), "ref_tag"),
        (
            "https://intranet.example/support",
            canonical_ref("https://intranet.example/support"),
            "urls:http",
        ),
        ("ISO 9001:2015", canonical_ref("ISO 9001:2015"), "doi_iso:iso"),
        ("article 1240 du code civil", canonical_ref("1240"), "legal_fr:prose_article_du_code"),
    ]


def test_determinism_extract_twice_identical():
    text = (
        "Les articles L.8888-1 et L.7777-2, le SKU AP-3000-X (EAN 4006381333931), "
        "RFC 7231, 10.1000/182 et https://intranet.example/a."
    )
    first = extract_references(text)
    second = extract_references(text)
    assert first == second
    assert_spans(text, first)


def test_empty_text():
    assert extract_references("") == []


def test_pack_names_subset_filters_extraction():
    text = "AP-3000-X et https://x.example/a sont listés."
    atoms = extract_references(text, ["urls"])
    assert_spans(text, atoms)
    assert [a.raw for a in atoms] == ["https://x.example/a"]
    assert atoms[0].pack == "urls:http"


def test_span_exactness_sweep():
    texts = [
        "Voir [REF: AP-3000-X] et [REF: see appendix B].",
        "Réf [AP-3000-X] et (L.1233-3) et (https://intranet.example/s).",
        "Cass. soc., 12 mai 2021, n°19-12.345 ; article 1240 du code civil.",
        "EAN 4006381333931 / 96385074, ISO/IEC 27001, RFC-9110, 10.1000/182.",
        "Les articles L.8888-1 et L.7777-2 du dossier DOC_2024_117.",
    ]
    for text in texts:
        atoms = extract_references(text)
        assert atoms, f"expected atoms in: {text!r}"
        assert_spans(text, atoms)
