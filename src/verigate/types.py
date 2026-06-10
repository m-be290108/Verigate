"""Core data types for VeriGate.

Everything downstream (extractors, engine, API, audit) depends on these.
Reports must be byte-identical for identical inputs (D-006): no wall-clock,
no randomness, deterministic JSON serialization (sort_keys, fixed separators).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum


class AtomType(str, Enum):
    """The kinds of verifiable atoms VeriGate extracts from an answer."""

    REFERENCE = "reference"  # identifiers: SKU, legal refs, DOI, ISO, internal URLs…
    NUMBER = "number"        # anchored numbers: money, percent, unit, date, decimal
    QUOTE = "quote"          # text between quotation marks
    ENTITY = "entity"        # glossary entities (product names, people…)


class AtomStatus(str, Enum):
    VERIFIED = "verified"          # found in the corpus
    MISMATCHED = "mismatched"      # close to a corpus value but contradicts it
    NOT_FOUND = "not_found"        # absent from the corpus
    UNVERIFIABLE = "unverifiable"  # nothing meaningful to check it against


#: Statuses that count as "false" — the atom is removed from the answer.
FALSE_STATUSES = frozenset({AtomStatus.MISMATCHED, AtomStatus.NOT_FOUND})


class Verdict(str, Enum):
    """Graduated verdicts — same gradation as the production-proven Beaume
    verifier (VALIDÉ / CORRIGÉ / INSUFFISANT / NON VÉRIFIABLE), see D-002."""

    VERIFIED = "VERIFIED"          # every checkable atom verified (and ≥ 1 atom)
    CORRECTED = "CORRECTED"        # false atoms removed, score ≥ 0.5
    INSUFFICIENT = "INSUFFICIENT"  # false atoms removed, score < 0.5
    UNVERIFIABLE = "UNVERIFIABLE"  # zero checkable atoms → score 0.0, never 100%


@dataclass(frozen=True)
class Atom:
    """A verifiable claim extracted from an answer.

    `start`/`end` are offsets into the *original* answer string and include
    any surrounding delimiters captured by the extractor (brackets, quotes),
    so that removal-by-span leaves no dangling syntax (D-001).
    """

    type: AtomType
    raw: str         # exact text as it appears in the answer (answer[start:end])
    canonical: str   # canonical matching key (see canonical.py)
    start: int
    end: int
    pack: str = ""   # name of the extractor/pack that produced it

    def to_dict(self) -> dict:
        return {
            "type": self.type.value,
            "raw": self.raw,
            "canonical": self.canonical,
            "start": self.start,
            "end": self.end,
            "pack": self.pack,
        }


@dataclass
class AtomResult:
    """Verification outcome for a single atom."""

    atom: Atom
    status: AtomStatus
    matched_source: str = ""  # corpus document that matched ("" if none)
    detail: str = ""          # human-readable explanation, deterministic wording

    def to_dict(self) -> dict:
        return {
            "atom": self.atom.to_dict(),
            "status": self.status.value,
            "matched_source": self.matched_source,
            "detail": self.detail,
        }


@dataclass
class Report:
    """Full verification report for one answer.

    `score` is verified / (verified + false); unverifiable atoms are excluded
    from the denominator (D-003). Zero checkable atoms → score 0.0 and
    verdict UNVERIFIABLE (D-002).
    """

    verdict: Verdict
    score: float
    atoms: list[AtomResult] = field(default_factory=list)
    corrected_answer: str = ""
    answer_sha256: str = ""
    corpus_fingerprint: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def n_verified(self) -> int:
        return sum(1 for r in self.atoms if r.status == AtomStatus.VERIFIED)

    @property
    def n_false(self) -> int:
        return sum(1 for r in self.atoms if r.status in FALSE_STATUSES)

    @property
    def n_unverifiable(self) -> int:
        return sum(1 for r in self.atoms if r.status == AtomStatus.UNVERIFIABLE)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "score": round(self.score, 4),
            "counts": {
                "total": len(self.atoms),
                "verified": self.n_verified,
                "false": self.n_false,
                "unverifiable": self.n_unverifiable,
            },
            "atoms": [r.to_dict() for r in self.atoms],
            "corrected_answer": self.corrected_answer,
            "answer_sha256": self.answer_sha256,
            "corpus_fingerprint": self.corpus_fingerprint,
            "warnings": self.warnings,
        }

    def to_json(self) -> str:
        """Deterministic serialization — byte-identical for identical inputs."""
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )


#: Visible markers substituted for removed atoms (D-005). Never silent.
REMOVAL_MARKERS: dict[AtomType, str] = {
    AtomType.REFERENCE: "⟨unverified reference, removed⟩",
    AtomType.NUMBER: "⟨unverified figure, removed⟩",
    AtomType.QUOTE: "⟨unverified quote, removed⟩",
    AtomType.ENTITY: "⟨unverified entity, removed⟩",
}
