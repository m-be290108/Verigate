"""Quote extractor — quoted passages become QUOTE atoms.

Three double-quote styles are recognized: straight ("…"), typographic
(“…”) and French guillemets (« … », with or without inner regular or
non-breaking spaces). Single quotes are deliberately NOT supported in this
MVP: in French prose the apostrophe (l'eau, d'abord) is indistinguishable
from a closing single quote, which makes pairing ambiguous.

Pairing is per-style: a « only closes with », a “ only with ”, and a
straight " pairs with the *next* straight ". Quotes with fewer than
`min_words` words are ignored entirely — too short to be a meaningful
citation check (D-003 noise control: scare quotes and emphasis would only
dilute the report). The closer must appear within MAX_QUOTE_CHARS of the
opener; otherwise the opener is treated as unbalanced and yields no atom
(a runaway unbalanced quote must not swallow the document).
"""

from __future__ import annotations

import re

from verigate.canonical import canonical_text
from verigate.types import Atom, AtomType

#: Closing mark required by each supported opening mark (per-style pairing).
_CLOSERS: dict[str, str] = {
    '"': '"',
    "“": "”",  # typographic “ ”
    "«": "»",  # French « »
}

#: Maximum length of a quote's inner text, in characters.
MAX_QUOTE_CHARS = 600

_WORD_RE = re.compile(r"\w+")


class QuoteExtractor:
    """Extracts quoted passages; spans include the quote marks (D-001)."""

    name = "quotes"

    def __init__(self, min_words: int = 3) -> None:
        self.min_words = min_words

    def extract(self, text: str) -> list[Atom]:
        atoms: list[Atom] = []
        i = 0
        n = len(text)
        while i < n:
            closer = _CLOSERS.get(text[i])
            if closer is None:
                i += 1
                continue
            # Inner text capped at MAX_QUOTE_CHARS chars → the closer can sit
            # no further than i + MAX_QUOTE_CHARS + 1 (endpos is exclusive).
            j = text.find(closer, i + 1, i + MAX_QUOTE_CHARS + 2)
            if j == -1:
                # Unbalanced opener (no closer within the limit): no atom.
                i += 1
                continue
            inner = text[i + 1 : j]
            if len(_WORD_RE.findall(inner)) >= self.min_words:
                atoms.append(
                    Atom(
                        type=AtomType.QUOTE,
                        raw=text[i : j + 1],
                        canonical=canonical_text(inner),
                        start=i,
                        end=j + 1,
                        pack="quotes",
                    )
                )
            # Consume the pair even when too short, so its closer cannot be
            # re-read as the opener of a phantom quote.
            i = j + 1
        return atoms
