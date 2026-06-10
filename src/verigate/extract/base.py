"""Extractor protocol and span utilities shared by all atom extractors.

An extractor is 100% deterministic: pure regex/string logic, no LLM, no
network, no clock. Each extractor returns `Atom`s whose (start, end) spans
point into the original answer text, delimiters included (D-001).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from verigate.types import Atom


@runtime_checkable
class Extractor(Protocol):
    """Anything that can pull atoms out of an answer text."""

    name: str

    def extract(self, text: str) -> list[Atom]:
        """Return all atoms found in `text`, spans into `text`."""
        ...


def dedupe_overlapping(atoms: list[Atom]) -> list[Atom]:
    """Resolve overlapping spans: keep the longest span; on equal length the
    earliest extractor registration order (list order) wins. Two atoms at
    identical (start, end) with the same canonical key are duplicates.

    Deterministic by construction — sorting keys never tie on identity.
    """
    # Longest-first so a [REF: X] span beats the bare X inside it.
    ranked = sorted(
        enumerate(atoms),
        key=lambda p: (-(p[1].end - p[1].start), p[1].start, p[0]),
    )
    kept: list[Atom] = []
    occupied: list[tuple[int, int]] = []
    for _, atom in ranked:
        if any(atom.start < e and s < atom.end for s, e in occupied):
            continue
        kept.append(atom)
        occupied.append((atom.start, atom.end))
    kept.sort(key=lambda a: (a.start, a.end))
    return kept
