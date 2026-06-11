"""Canonicalization helpers — the shared matching keys between answer atoms
and corpus entries.

Modeled on the Beaume verifier's `_canonicalize` (strip/dots/upper) and
`verify_kb_curated._norm` (NFKD + alnum-only), both production-proven.
All functions are pure and deterministic.
"""

from __future__ import annotations

import re
import unicodedata

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]")


def canonical_ref(s: str) -> str:
    """Canonical key for references/identifiers.

    Tolerates the usual drift between how an ID is written in prose and how
    it is stored: spaces, dots, dashes, case. ``"L. 1233-3"`` and ``"L1233-3"``
    and ``"l 1233 3"`` share one key.
    """
    s = _WS_RE.sub("", s)
    return s.replace(".", "").replace("-", "").replace("_", "").upper()


def canonical_text(s: str) -> str:
    """Canonical form for quote matching: NFKD, accents stripped, lowercase,
    everything non-alphanumeric removed. Robust to punctuation, casing and
    typographic quotes — a quote matches iff its letters match in order."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return _NON_ALNUM_RE.sub("", s.lower())


def canonical_entity(s: str) -> str:
    """Canonical form for glossary entities: accent-insensitive, lowercase,
    single-spaced (word boundaries preserved, unlike canonical_text)."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return s.strip()


def canonical_number(s: str) -> str:
    """Canonical form for a numeric literal: strip thousands separators
    (space, narrow nbsp, comma-as-thousands, apostrophe), normalize the
    decimal comma to a dot, drop a trailing ``.0``-style zero fraction.

    ``"1 234,50"`` → ``"1234.5"``;  ``"1,234.50"`` → ``"1234.5"``;
    ``"49.99"`` → ``"49.99"``;  ``"200.0"`` → ``"200"``;
    ``"3,284,71"`` → ``"3284.71"`` (French all-comma: thousands commas plus
    a decimal comma, systematic in BDPM exports for amounts ≥ 1000 €).
    """
    s = s.strip().replace(" ", "").replace(" ", "")
    # Both separators present: the last one is the decimal mark.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        groups = s.split(",")
        # Several commas with a 2-digit last group ('3,284,71'): commas are
        # thousands separators except the last one, which is the decimal
        # mark — a 3-digit last group ('1,234,567') stays all-thousands.
        if len(groups) > 2 and all(g.isdigit() for g in groups) and len(groups[-1]) == 2:
            s = "".join(groups[:-1]) + "." + groups[-1]
        # Lone comma: decimal if followed by 1-2 digits at the end (49,99),
        # thousands otherwise (1,234 / 12,345,678).
        elif re.fullmatch(r"\d{1,3}(,\d{3})+", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    s = s.replace(" ", "").replace("'", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s
