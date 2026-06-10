"""Anchored-number extractor — money, percentages, units, dates, decimals.

Mandated decisions (DECISIONS.md D-001/D-004 and the build plan):

- D-004: bare integers below 1000 with no unit/currency/percent anchor are
  NOT extracted at all — a bare "10" matches everywhere in any corpus
  (false-positive machine). Integer-shaped literals >= 1000 are kept.
- Kinds and precedence: money > percent > unit > date > decimal > integer.
  Precedence is structural: kinds are registered in that order and
  `dedupe_overlapping` keeps the longest span, breaking equal-span ties by
  registration order (so a standalone year beats the equal-span integer).
- Numeric dates dd/mm/yyyy and dd.mm.yyyy are parsed DAY-FIRST (European
  corpora); calendar validity is enforced via `datetime.date`, so
  31/02/2024 yields no atom at all.
- Negative guards: no match may be glued to ``[A-Za-z0-9-]`` on either side
  (identifiers like AP-3000-X yield nothing); bare numbers additionally
  refuse ``.,/`` gluing so 3.11.2, 192.168.1.1 and the fragments of a
  rejected date never leak out as decimals, integers or years.

Spans include the currency/unit symbols (D-001) so removal-by-span leaves
no dangling syntax. 100% deterministic: pure regex plus `datetime.date` —
no LLM, no network, no clock, no randomness.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date

from verigate.canonical import canonical_number
from verigate.extract.base import dedupe_overlapping
from verigate.types import Atom, AtomType

# ------------------------------------------------------------------ guards

_GL = r"(?<![A-Za-z0-9-])"  # identifier guard, left (kills AP-3000-X)
_GR = r"(?![A-Za-z0-9-])"   # identifier guard, right
_NL = _GL + r"(?<![.,/])"   # bare-number left  (kills 3.|11 and 31/|02 fragments)
_NR = _GR + r"(?![.,/]\d)"  # bare-number right (kills 3.11|.2, 1,234|.50, 31|/02)

#: Separator tolerated inside/around numbers: space, nbsp, narrow nbsp.
_SEP = "[ \u00a0\u202f]"  # space, no-break space, narrow no-break space

#: A numeric literal. `canonical_number` owns separator disambiguation.
_NUM = (
    r"(?:\d{1,3}(?:" + _SEP + r"\d{3})+(?:[.,]\d+)?"  # 25 000 / 1 234,50
    r"|\d{1,3}(?:,\d{3})+(?:\.\d+)?"                  # 25,000 / 1,234.50
    r"|\d+(?:[.,]\d+)?)"                              # 200 / 49.99 / 249,99
)

#: Integer-shaped literal (thousands groups or plain digits, no decimal
#: mark) — splits the integer kind from the decimal kind on `fullmatch`.
_INT_SHAPED = re.compile(r"\d{1,3}(?:" + _SEP + r"\d{3})+|\d{1,3}(?:,\d{3})+|\d+")


def _alternation(aliases: Iterable[str]) -> str:
    """Regex alternation, longest-first then lexicographic: deterministic
    and prefix-safe (`l/min` beats `l`, `kwh` beats `kw`)."""
    return "|".join(re.escape(a) for a in sorted(aliases, key=lambda a: (-len(a), a)))


# ------------------------------------------------------------------- money

#: Curated currency aliases (lookup on ``.lower()``).
_CURRENCIES: dict[str, str] = {
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "$": "USD", "usd": "USD",
    "£": "GBP", "gbp": "GBP",
}
_CUR_ALT = r"[€$£]|EUR|USD|GBP"
_MONEY_PRE_RE = re.compile(
    _GL + r"(?P<cur>" + _CUR_ALT + r")" + _SEP + r"?(?P<num>" + _NUM + r")" + _NR,
    re.IGNORECASE,
)
_MONEY_POST_RE = re.compile(
    _NL + r"(?P<num>" + _NUM + r")" + _SEP + r"?(?P<cur>" + _CUR_ALT + r"|euros?)" + _GR,
    re.IGNORECASE,
)

# ----------------------------------------------------------------- percent

_PERCENT_RE = re.compile(
    _NL + r"(?P<num>" + _NUM + r")" + _SEP + r"?(?:%|pct)" + _GR,
    re.IGNORECASE,
)

# ------------------------------------------------------------------- units

#: Curated unit aliases (lowercase alias -> canonical unit token).
_UNITS: dict[str, str] = {
    # electrical / power / frequency
    "v": "v", "volt": "v", "volts": "v",
    "w": "w", "watt": "w", "watts": "w",
    "kw": "kw", "kwh": "kwh", "a": "a",
    "hz": "hz", "mhz": "mhz", "ghz": "ghz",
    # mass
    "kg": "kg", "g": "g", "mg": "mg", "t": "t",
    # length
    "km": "km", "m": "m", "cm": "cm", "mm": "mm",
    # volume / flow
    "l": "l", "ml": "ml", "l/min": "l/min", "m3": "m3", "m³": "m3",
    # data
    "gb": "gb", "mb": "mb", "tb": "tb",
    # temperature
    "°c": "°c", "°f": "°f",
    # time
    "h": "h", "hour": "h", "hours": "h", "heure": "h", "heures": "h",
    "min": "min", "s": "s", "ms": "ms",
    "day": "day", "days": "day", "jour": "day", "jours": "day",
    "month": "month", "months": "month", "mois": "month",
    "year": "year", "years": "year", "an": "year", "ans": "year",
    "annee": "year", "annees": "year", "année": "year", "années": "year",
}

#: Natural-language aliases: word-bounded with a MANDATORY whitespace
#: separator ("2 mois" yes, "2mois" no). Symbol aliases may glue ("230V").
_WORD_UNIT_ALIASES = frozenset({
    "volt", "volts", "watt", "watts",
    "hour", "hours", "heure", "heures",
    "day", "days", "jour", "jours",
    "month", "months", "mois",
    "year", "years", "an", "ans", "annee", "annees", "année", "années",
})

_SYM_ALT = _alternation(set(_UNITS) - _WORD_UNIT_ALIASES)
_WORD_ALT = _alternation(_WORD_UNIT_ALIASES)
_UNIT_RE = re.compile(
    _NL + r"(?P<num>" + _NUM + r")"
    r"(?:" + _SEP + r"?(?P<sym>" + _SYM_ALT + r")"
    r"|" + _SEP + r"(?P<word>" + _WORD_ALT + r"))" + _GR,
    re.IGNORECASE,
)

# ------------------------------------------------------------------- dates

#: Month names, French (accented and unaccented spellings) and English.
_MONTHS: dict[str, int] = {
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "decembre": 12, "décembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
_MONTH_ALT = _alternation(_MONTHS)

_DATE_ISO_RE = re.compile(_NL + r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})" + _NR)
#: dd/mm/yyyy and dd.mm.yyyy — DAY-FIRST (European corpora).
_DATE_NUM_RE = re.compile(
    _NL + r"(?P<d>\d{1,2})(?P<sep>[./])(?P<mo>\d{1,2})(?P=sep)(?P<y>\d{4})" + _NR
)
_DATE_DMY_RE = re.compile(
    _NL + r"(?P<d>\d{1,2})" + _SEP + r"+(?P<mon>" + _MONTH_ALT + r")"
    + _SEP + r"+(?P<y>\d{4})" + _NR,
    re.IGNORECASE,
)
_DATE_MDY_RE = re.compile(
    _GL + r"(?P<mon>" + _MONTH_ALT + r")" + _SEP + r"+(?P<d>\d{1,2})"
    r"(?:," + _SEP + r"*|" + _SEP + r"+)(?P<y>\d{4})" + _NR,
    re.IGNORECASE,
)
#: Standalone years 1900-2099, year precision (canonical ``date:1995``).
_YEAR_RE = re.compile(_NL + r"(?:19|20)\d{2}" + _NR)

# --------------------------------------------------------- decimal/integer

_BARE_RE = re.compile(_NL + _NUM + _NR)


def _calendar_date(year: int, month: int, day: int) -> date | None:
    """Return the date if it exists on the calendar, else None.

    Only ValueError is caught (ruff BLE): an invalid full date such as
    31/02/2024 must produce no atom at all.
    """
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _atom(text: str, start: int, end: int, kind: str, canonical: str) -> Atom:
    return Atom(
        type=AtomType.NUMBER,
        raw=text[start:end],
        canonical=canonical,
        start=start,
        end=end,
        pack=f"number:{kind}",
    )


class NumberExtractor:
    """Extracts anchored numbers; spans include unit/currency symbols."""

    name = "numbers"

    def extract(self, text: str) -> list[Atom]:
        atoms: list[Atom] = []
        atoms += self._money(text)      # precedence-ordered registration:
        atoms += self._percents(text)   # equal spans dedupe to the earliest
        atoms += self._units(text)      # index = higher-precedence kind
        atoms += self._dates(text)      # (years beat equal-span integers)
        atoms += self._decimals(text)
        atoms += self._integers(text)
        return dedupe_overlapping(atoms)

    def _money(self, text: str) -> list[Atom]:
        atoms: list[Atom] = []
        for rx in (_MONEY_PRE_RE, _MONEY_POST_RE):
            for m in rx.finditer(text):
                code = _CURRENCIES[m.group("cur").lower()]
                canonical = f"money:{code}:{canonical_number(m.group('num'))}"
                atoms.append(_atom(text, m.start(), m.end(), "money", canonical))
        return atoms

    def _percents(self, text: str) -> list[Atom]:
        return [
            _atom(
                text, m.start(), m.end(), "percent",
                f"percent:{canonical_number(m.group('num'))}",
            )
            for m in _PERCENT_RE.finditer(text)
        ]

    def _units(self, text: str) -> list[Atom]:
        atoms: list[Atom] = []
        for m in _UNIT_RE.finditer(text):
            alias = (m.group("sym") or m.group("word")).lower()
            canonical = f"unit:{_UNITS[alias]}:{canonical_number(m.group('num'))}"
            atoms.append(_atom(text, m.start(), m.end(), "unit", canonical))
        return atoms

    def _dates(self, text: str) -> list[Atom]:
        atoms: list[Atom] = []
        for rx in (_DATE_ISO_RE, _DATE_NUM_RE):
            for m in rx.finditer(text):
                d = _calendar_date(int(m.group("y")), int(m.group("mo")), int(m.group("d")))
                if d is not None:
                    atoms.append(_atom(text, m.start(), m.end(), "date", f"date:{d.isoformat()}"))
        for rx in (_DATE_DMY_RE, _DATE_MDY_RE):
            for m in rx.finditer(text):
                month = _MONTHS[m.group("mon").lower()]
                d = _calendar_date(int(m.group("y")), month, int(m.group("d")))
                if d is not None:
                    atoms.append(_atom(text, m.start(), m.end(), "date", f"date:{d.isoformat()}"))
        for m in _YEAR_RE.finditer(text):
            atoms.append(_atom(text, m.start(), m.end(), "date", f"date:{m.group(0)}"))
        return atoms

    def _decimals(self, text: str) -> list[Atom]:
        atoms: list[Atom] = []
        for m in _BARE_RE.finditer(text):
            raw = m.group(0)
            if _INT_SHAPED.fullmatch(raw):
                continue
            canonical = f"decimal:{canonical_number(raw)}"
            atoms.append(_atom(text, m.start(), m.end(), "decimal", canonical))
        return atoms

    def _integers(self, text: str) -> list[Atom]:
        atoms: list[Atom] = []
        for m in _BARE_RE.finditer(text):
            raw = m.group(0)
            if not _INT_SHAPED.fullmatch(raw):
                continue
            value = canonical_number(raw)
            if int(value) < 1000:
                continue  # D-004: bare small integers are a false-positive machine.
            atoms.append(_atom(text, m.start(), m.end(), "integer", f"integer:{value}"))
        return atoms


_DEFAULT = NumberExtractor()


def extract_numbers(text: str) -> list[Atom]:
    """Module-level convenience wrapper around a shared NumberExtractor."""
    return _DEFAULT.extract(text)
