"""Glossary-entity extractor — known entities and near-miss candidates.

Two packs, adjudicated later by the engine (the extractor only labels them):

* pack='glossary' — occurrences of a known glossary entity, matched
  accent/case-insensitively, with spaces or hyphens tolerated *between*
  canonical tokens but never inside one ('AquaPump  3000' and
  'aquapump 3000' match the entry 'AquaPump 3000'; 'Aqua-Pump 3000' does
  not — its first token differs).
* pack='glossary_candidate' — capitalized product-like phrases that are not
  a known-entity span, kept only if plausibly related to the glossary
  (shared canonical token, or SequenceMatcher ratio ≥ 0.72 with some
  entry). Unrelated capitalized phrases ('Best Regards', 'New York') are
  not extracted.

Accent-insensitive matching runs on a length-preserving ASCII "shadow" of
the haystack so spans index the ORIGINAL text. NFKD on the whole haystack
would break offsets ('é' → 'e' + combining acute grows the string), so the
shadow is built per character: each char is replaced by the first
non-combining char of its own NFKD decomposition — exactly one output char
per input char. Multi-char compatibility expansions ('ﬁ' → 'fi') are thus
truncated to their first base char, an acceptable loss for glossary
matching.
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from verigate.canonical import canonical_entity
from verigate.extract.base import dedupe_overlapping
from verigate.types import Atom, AtomType

#: Capitalized product-like phrase: 2+ tokens, first starting [A-Z], the
#: following ones [A-Z0-9], separated by a single space or hyphen.
_CANDIDATE_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*(?:[\s\-][A-Z0-9][A-Za-z0-9]*)+\b")

#: Leading capitalized stopwords stripped from candidates, so a
#: sentence-initial bigram like 'The AquaPump' is tested as 'AquaPump'.
_LEADING_STOPWORDS = frozenset(
    {"the", "a", "an", "le", "la", "les", "un", "une", "des", "our", "votre", "notre"}
)

#: Minimum SequenceMatcher ratio for a candidate sharing no canonical token.
_RATIO_THRESHOLD = 0.72

_SEPARATOR_RE = re.compile(r"[\s\-]+")


def _ascii_shadow(text: str) -> str:
    """Length-preserving accent fold: 'Méga' → 'Mega', identical offsets."""
    out: list[str] = []
    for ch in text:
        decomposed = unicodedata.normalize("NFKD", ch)
        base = next((c for c in decomposed if not unicodedata.combining(c)), ch)
        out.append(base)
    return "".join(out)


class EntityExtractor:
    """Extracts glossary entities and plausible near-miss candidates."""

    name = "entities"

    def __init__(self, glossary: list[tuple[str, str]]) -> None:
        # (canonical, display) pairs as returned by CorpusDB.entities();
        # `canonical` was produced by canonical_entity.
        self.glossary = list(glossary)
        self._entries: list[tuple[str, frozenset[str], re.Pattern[str]]] = []
        for canonical, _display in self.glossary:
            tokens = canonical.split()
            if not tokens:
                continue
            pattern = re.compile(
                r"\b" + r"[\s\-]+".join(re.escape(t) for t in tokens) + r"\b",
                re.IGNORECASE,
            )
            self._entries.append((canonical, frozenset(tokens), pattern))

    def extract(self, text: str) -> list[Atom]:
        # Empty glossary: nothing checkable, no candidates either.
        if not self._entries:
            return []
        shadow = _ascii_shadow(text)
        atoms: list[Atom] = []
        known_spans: set[tuple[int, int]] = set()
        # Glossary matches first: on equal spans dedupe_overlapping keeps
        # the earliest-registered atom, so exact matches win ties.
        for _canonical, _tokens, pattern in self._entries:
            for m in pattern.finditer(shadow):
                start, end = m.span()
                raw = text[start:end]
                atoms.append(
                    Atom(
                        type=AtomType.ENTITY,
                        raw=raw,
                        canonical=canonical_entity(raw),
                        start=start,
                        end=end,
                        pack="glossary",
                    )
                )
                known_spans.add((start, end))
        for m in _CANDIDATE_RE.finditer(shadow):
            start, end = m.span()
            first_token = _SEPARATOR_RE.split(m.group())[0]
            if first_token.lower() in _LEADING_STOPWORDS:
                # Candidates have ≥ 2 tokens, so a separator always follows
                # the stopword inside the match: search cannot return None.
                separator = _SEPARATOR_RE.search(shadow, start, end)
                start = separator.end()
            if (start, end) in known_spans:
                continue
            raw = text[start:end]
            cand_canonical = canonical_entity(raw)
            cand_tokens = set(cand_canonical.split())
            related = any(
                cand_tokens & entry_tokens
                or SequenceMatcher(None, cand_canonical, entry_canonical).ratio()
                >= _RATIO_THRESHOLD
                for entry_canonical, entry_tokens, _pattern in self._entries
            )
            if not related:
                continue
            atoms.append(
                Atom(
                    type=AtomType.ENTITY,
                    raw=raw,
                    canonical=cand_canonical,
                    start=start,
                    end=end,
                    pack="glossary_candidate",
                )
            )
        return dedupe_overlapping(atoms)
