"""Tests for the anchored-numbers extractor.

Covers every mandated case from the build plan: money (symbol before/after,
ISO codes, currency words), percentages, curated FR/EN units (plurals,
compounds, glued symbols), dates (ISO, day-first numeric, dotted, month
names in both orders, standalone years, calendar validation), decimals,
thousands-separated integers, the D-004 bare-small-integer exclusion and
the identifier/version/IP lookaround guards. All deterministic, offline.
"""

from __future__ import annotations

from verigate.extract.base import Extractor
from verigate.extract.numbers import NumberExtractor, extract_numbers
from verigate.types import AtomType

KITCHEN_SINK = (
    "Pump AP-3000-X costs €249.99 (was 300 euros), draws 550 W at 230V, "
    "25% off until 03/04/2025; flow 12 L/min; stock: 25 000 units; "
    "released May 12, 2021, firmware 3.11.2, warranty 24 months, since 1998."
)

EXPECTED_KITCHEN_SINK = [
    "money:EUR:249.99",
    "money:EUR:300",
    "unit:w:550",
    "unit:v:230",
    "percent:25",
    "date:2025-04-03",
    "unit:l/min:12",
    "integer:25000",
    "date:2021-05-12",
    "unit:month:24",
    "date:1998",
]


def _canonicals(text: str) -> list[str]:
    return [a.canonical for a in NumberExtractor().extract(text)]


def _single(text: str):
    atoms = NumberExtractor().extract(text)
    assert len(atoms) == 1, f"expected exactly one atom in {text!r}, got {atoms!r}"
    return atoms[0]


# ---------------------------------------------------------------- protocol


def test_protocol_conformance():
    extractor = NumberExtractor()
    assert isinstance(extractor, Extractor)
    assert extractor.name == "numbers"


# ------------------------------------------------------------------- money


def test_money_symbol_before():
    text = "Total: $1,234.50 due."
    a = _single(text)
    assert a.type is AtomType.NUMBER
    assert a.pack == "number:money"
    assert a.raw == "$1,234.50"
    assert a.canonical == "money:USD:1234.5"
    assert text[a.start : a.end] == a.raw


def test_money_symbol_after_french():
    text = "Price cut to 249,99 € this week."
    a = _single(text)
    assert a.raw == "249,99 €"  # span includes the currency symbol
    assert a.canonical == "money:EUR:249.99"
    assert text[a.start : a.end] == a.raw


def test_money_fr_comma_equals_en_dot():
    assert _canonicals("249,99 €") == _canonicals("€249.99") == ["money:EUR:249.99"]


def test_money_code_before():
    a = _single("Deposit of EUR 200 required.")
    assert a.raw == "EUR 200"
    assert a.canonical == "money:EUR:200"


def test_money_word_after():
    a = _single("It cost 200 euros back then.")
    assert a.raw == "200 euros"
    assert a.canonical == "money:EUR:200"
    assert _canonicals("200 euros") == _canonicals("EUR 200")


def test_money_gbp_and_usd():
    assert _canonicals("Ticket at £99 only.") == ["money:GBP:99"]
    assert _canonicals("Flat fee of USD 50 applies.") == ["money:USD:50"]


def test_money_word_without_amount_is_nothing():
    assert NumberExtractor().extract("Payable in euros.") == []


# ----------------------------------------------------------------- percent


def test_percent_forms():
    for text in ("25%", "25 %", "25 pct"):
        assert _canonicals(text) == ["percent:25"], text


def test_percent_decimal_beats_decimal():
    text = "Margin grew 3.5 % overall."
    a = _single(text)
    assert a.pack == "number:percent"
    assert a.raw == "3.5 %"
    assert a.canonical == "percent:3.5"


def test_percent_glued_to_identifier_is_nothing():
    assert NumberExtractor().extract("AB25%") == []


# ------------------------------------------------------------------- units


def test_unit_volt_spaced_and_glued():
    a = _single("Rated input 230 V exactly.")
    assert a.raw == "230 V"
    assert a.canonical == "unit:v:230"
    b = _single("It runs on 230V mains.")
    assert b.raw == "230V"
    assert b.canonical == "unit:v:230"


def test_unit_aliases_converge_fr_en():
    assert _canonicals("2 mois") == _canonicals("2 months") == ["unit:month:2"]


def test_unit_word_bounded_negative():
    assert NumberExtractor().extract("5 moisson") == []


def test_unit_compound_flow():
    a = _single("Max flow 12 L/min sustained.")
    assert a.raw == "12 L/min"
    assert a.canonical == "unit:l/min:12"


def test_unit_temperature():
    a = _single("Store at 21°C max.")
    assert a.raw == "21°C"
    assert a.canonical == "unit:°c:21"
    b = _single("Heat to 70 °F first.")
    assert b.raw == "70 °F"
    assert b.canonical == "unit:°f:70"


def test_unit_beats_integer_on_thousands():
    text = "Usage: 25 000 kWh per period."
    a = _single(text)
    assert a.raw == "25 000 kWh"
    assert a.canonical == "unit:kwh:25000"


# ------------------------------------------------------------------- dates


def test_date_iso():
    a = _single("Effective 2024-05-01 onward.")
    assert a.raw == "2024-05-01"
    assert a.canonical == "date:2024-05-01"
    assert a.pack == "number:date"


def test_date_numeric_day_first():
    a = _single("valid until 03/04/2025 inclusive")
    assert a.raw == "03/04/2025"
    assert a.canonical == "date:2025-04-03"  # day-first, not April 3rd US-style


def test_date_dotted_day_first():
    a = _single("Deadline 31.12.2024 firm.")
    assert a.raw == "31.12.2024"
    assert a.canonical == "date:2024-12-31"


def test_invalid_calendar_date_rejected():
    assert NumberExtractor().extract("31/02/2024") == []


def test_date_french_month():
    a = _single("Publié le 12 mai 2021 ici.")
    assert a.raw == "12 mai 2021"
    assert a.canonical == "date:2021-05-12"


def test_date_french_month_case_insensitive():
    assert _canonicals("12 MAI 2021") == ["date:2021-05-12"]


def test_date_english_month_both_orders():
    assert (
        _canonicals("May 12, 2021")
        == _canonicals("12 May 2021")
        == ["date:2021-05-12"]
    )


def test_standalone_year_beats_integer():
    text = "Launched in 1995."
    a = _single(text)
    assert a.raw == "1995"
    assert a.canonical == "date:1995"
    assert a.pack == "number:date"  # precedence over the equal-span integer


def test_year_out_of_range_is_integer():
    a = _single("2150")
    assert a.canonical == "integer:2150"
    assert a.pack == "number:integer"


# -------------------------------------------------------- decimal/integer


def test_decimal_dot_and_comma():
    a = _single("Filter price 49.99 before tax.")
    assert a.pack == "number:decimal"
    assert a.canonical == "decimal:49.99"
    b = _single("Pi vaut 3,14 environ.")
    assert b.canonical == "decimal:3.14"


def test_decimal_zero_fraction():
    a = _single("Weight 200.0 listed.")
    assert a.raw == "200.0"
    assert a.canonical == "decimal:200"


def test_thousands_integers_converge():
    assert (
        _canonicals("25 000")
        == _canonicals("25,000")
        == _canonicals("25 000")
        == ["integer:25000"]
    )


def test_bare_small_integers_not_extracted():
    # D-004: a bare "42" matches everywhere in any corpus.
    assert NumberExtractor().extract("There are 42 items in 7 boxes.") == []


# ------------------------------------------------------------------ guards


def test_identifier_guard():
    assert NumberExtractor().extract("SKU AP-3000-X in stock") == []


def test_version_and_ip_not_decimals():
    assert NumberExtractor().extract("Python 3.11.2") == []
    assert NumberExtractor().extract("192.168.1.1") == []


# ------------------------------------------------------------ kitchen sink


def test_kitchen_sink_spans_exact():
    atoms = NumberExtractor().extract(KITCHEN_SINK)
    assert atoms
    for a in atoms:
        assert KITCHEN_SINK[a.start : a.end] == a.raw


def test_kitchen_sink_deterministic():
    extractor = NumberExtractor()
    assert extractor.extract(KITCHEN_SINK) == extractor.extract(KITCHEN_SINK)


def test_kitchen_sink_canonicals_and_packs():
    atoms = NumberExtractor().extract(KITCHEN_SINK)
    assert [a.canonical for a in atoms] == EXPECTED_KITCHEN_SINK
    assert all(a.type is AtomType.NUMBER for a in atoms)
    assert all(a.pack.startswith("number:") for a in atoms)


def test_extract_numbers_convenience():
    assert extract_numbers(KITCHEN_SINK) == NumberExtractor().extract(KITCHEN_SINK)
