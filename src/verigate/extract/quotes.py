"""Quote extractor — quoted passages become QUOTE atoms.

Three double-quote styles are recognized: straight ("…"), typographic
(“…”) and French guillemets (« … », with or without inner regular or
non-breaking spaces). Single quotes are deliberately NOT supported in this
MVP: in French prose the apostrophe (l'eau, d'abord) is indistinguishable
from a closing single quote, which makes pairing ambiguous.

Pairing is per-style: a « only closes with », a “ only with ”, and a
straight " pairs with the next *plausible* straight " (see below). Quotes
with fewer than `min_words` words are ignored entirely — too short to be a
meaningful citation check (D-003 noise control: scare quotes and emphasis
would only dilute the report). The closer must appear within
MAX_QUOTE_CHARS of the opener; otherwise the opener is treated as
unbalanced and yields no atom (a runaway unbalanced quote must not swallow
the document).

Straight-quote disambiguation. The straight " doubles as the ASCII
inch/second/ditto mark (6" pipe), so naive pair-with-the-next-straight-"
can mispair a stray mark with the opening " of a genuine quote: the
innocent PROSE between them becomes the atom (fails the corpus lookup, gets
spliced out) while the genuinely quoted claim loses its opener and escapes
verification entirely. Two conservative, deterministic layers prevent this:

1. Boundary constraints — a straight " may only OPEN a quote when it is
   preceded by start-of-text, whitespace or an opening bracket AND followed
   by a non-space character; it may only CLOSE a quote when it is preceded
   by a non-space character AND followed by end-of-text, whitespace or
   punctuation. An inch mark such as 6" (preceded by a digit, followed by a
   space) can therefore never open a quote.
2. Ambiguity guard — if, after the constraint filtering, the count of
   plausible straight-quote delimiters in the text is ODD, one of them is a
   stray mark and any pairing would be a guess: NO straight-quote atom is
   extracted at all from that text.

Rationale (D-003 spirit): deleting innocent prose is strictly worse than
leaving a quote unchecked. An unextracted quote is merely unverified —
never corrupted, never removed. Typographic “…” and guillemet «…» pairing
is unaffected by either layer: those marks are directional and unambiguous.
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

#: Characters that may precede a straight-quote OPENER (besides whitespace
#: or start-of-text): opening brackets, including a French guillemet.
_OPENER_PRECEDERS = frozenset("([{«")

#: Punctuation that may follow a straight-quote CLOSER (besides whitespace
#: or end-of-text).
_CLOSER_FOLLOWERS = frozenset(".,;:!?)]}»…")


def _can_open_straight(text: str, i: int) -> bool:
    """True iff the straight " at `i` may OPEN a quote: preceded by
    start-of-text, whitespace or an opening bracket, and followed by a
    non-space character (an inch mark like 6" can never open)."""
    before_ok = i == 0 or text[i - 1].isspace() or text[i - 1] in _OPENER_PRECEDERS
    after_ok = i + 1 < len(text) and not text[i + 1].isspace()
    return before_ok and after_ok


def _can_close_straight(text: str, i: int) -> bool:
    """True iff the straight " at `i` may CLOSE a quote: preceded by a
    non-space character, and followed by end-of-text, whitespace or
    punctuation."""
    before_ok = i > 0 and not text[i - 1].isspace()
    after_ok = (
        i + 1 == len(text) or text[i + 1].isspace() or text[i + 1] in _CLOSER_FOLLOWERS
    )
    return before_ok and after_ok


def _count_plausible_straight(text: str) -> int:
    """Number of straight " marks that could plausibly delimit a quote
    (opener or closer) under the boundary constraints. Marks that satisfy
    neither role (e.g. a " surrounded by spaces) are not delimiters and do
    not count toward the parity check."""
    return sum(
        1
        for i, ch in enumerate(text)
        if ch == '"' and (_can_open_straight(text, i) or _can_close_straight(text, i))
    )


def _find_straight_closer(text: str, opener: int) -> int:
    """Index of the first straight " after `opener` that may CLOSE (within
    the MAX_QUOTE_CHARS window), or -1. Straight marks that cannot close
    (e.g. a quote glued to a following word) stay part of the inner text."""
    limit = opener + MAX_QUOTE_CHARS + 2  # endpos is exclusive
    j = text.find('"', opener + 1, limit)
    while j != -1:
        if _can_close_straight(text, j):
            return j
        j = text.find('"', j + 1, limit)
    return -1


class QuoteExtractor:
    """Extracts quoted passages; spans include the quote marks (D-001)."""

    name = "quotes"

    def __init__(self, min_words: int = 3) -> None:
        self.min_words = min_words

    def extract(self, text: str) -> list[Atom]:
        # Ambiguity guard (module docstring, layer 2): an odd number of
        # plausible straight delimiters means a stray inch/second/ditto
        # mark is present and pairing would be a guess — extract no
        # straight-quote atom at all. “…” and «…» are unaffected.
        straight_enabled = _count_plausible_straight(text) % 2 == 0

        atoms: list[Atom] = []
        i = 0
        n = len(text)
        while i < n:
            closer = _CLOSERS.get(text[i])
            if closer is None:
                i += 1
                continue
            if text[i] == '"':
                if not straight_enabled or not _can_open_straight(text, i):
                    i += 1
                    continue
                j = _find_straight_closer(text, i)
            else:
                # Inner text capped at MAX_QUOTE_CHARS chars → the closer can
                # sit no further than i + MAX_QUOTE_CHARS + 1 (endpos is
                # exclusive).
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
