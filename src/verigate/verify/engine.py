"""Verification engine — the heart of VeriGate: ``verify(answer) -> Report``.

THIS ENGINE ENFORCES *GROUNDEDNESS*, NOT ABSTRACT TRUTH. An atom is VERIFIED
iff it is found in the trusted corpus (or in the optional per-call `context`
chunks, e.g. the RAG chunks the answer was generated from). A figure that is
true in the world but absent from the corpus IS flagged and removed — that is
the contract: VeriGate certifies "this answer is supported by the corpus you
trust", never "this answer is true".

Pipeline (``Verifier.verify``):

1. Extract atoms with the four extractors in FIXED order
   [references, numbers, quotes, entities], then dedupe overlapping spans
   GLOBALLY. Cross-extractor overlaps are real: a quote containing a SKU
   keeps only the (longer) quote span — the quote is checked as a whole and,
   if false, the whole span is removed, SKU included. On equal-length spans
   the earlier extractor in the fixed order wins (a reference beats an
   equal-span entity candidate).
2. Adjudicate each atom against the corpus first, then against the context
   (``matched_source`` is the corpus doc_id, or ``'context'``).
3. Score per D-003: unverifiable atoms are excluded from the denominator.
   Verdict per D-002 (Beaume gradation — never a vacuous 100% on zero
   checkable atoms).
4. Rewrite the answer by span splicing (D-001, see ``rewrite.py``).

100% deterministic and offline: no LLM, no network, no clock, no unseeded
randomness; every iteration order is fixed or sorted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from verigate.canonical import canonical_text
from verigate.corpus import CorpusDB
from verigate.extract.base import dedupe_overlapping
from verigate.extract.entities import EntityExtractor
from verigate.extract.numbers import NumberExtractor
from verigate.extract.quotes import QuoteExtractor
from verigate.extract.references import ReferenceExtractor, builtin_pack_names, load_pack
from verigate.types import (
    FALSE_STATUSES,
    REMOVAL_MARKERS,
    Atom,
    AtomResult,
    AtomStatus,
    AtomType,
    Report,
    Verdict,
)
from verigate.verify.rewrite import rewrite_answer

_WARN_EMPTY = "empty answer"
_WARN_NOTHING_CHECKABLE = (
    "No verifiable atoms found; nothing in this answer could be checked "
    "against the corpus."
)
_WARN_INPUT_MARKER = (
    "input already contained a VeriGate removal marker; "
    "markers in corrected_answer are not all VeriGate-issued"
)
_DETAIL_REF_NOT_FOUND = "reference not found in trusted corpus"
_DETAIL_NUMBER_NOT_FOUND = "number not found in trusted corpus"
_DETAIL_QUOTE_NOT_FOUND = "quote not found verbatim in trusted corpus"
_DETAIL_GLOSSARY_DRIFT = "glossary entity not found in trusted corpus"
_DETAIL_NO_CLOSE_MATCH = "not in glossary, no close match — cannot verify"
_WARN_SCOPE_SKIPPED = (
    "scoped verification skipped: no known subject mentioned in the answer"
)

#: matched_source value for atoms grounded in the per-call context.
_CONTEXT_SOURCE = "context"

#: Digit↔letter boundary inside a canonical token ('25mg' → '25', 'mg').
_DIGIT_LETTER_RE = re.compile(r"(?<=\d)(?=[a-z])|(?<=[a-z])(?=\d)")


def _entity_tokens(canonical: str) -> tuple[str, ...]:
    """Tokens of a canonical entity, additionally split at digit↔letter
    boundaries so '25mg' and '25 mg' compare token-equal (D-015)."""
    return tuple(
        piece for token in canonical.split() for piece in _DIGIT_LETTER_RE.split(token)
    )


def _is_contiguous_run(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    """True iff `needle` appears as a contiguous token run inside `haystack`."""
    if not needle:
        return False
    n = len(needle)
    return any(haystack[i : i + n] == needle for i in range(len(haystack) - n + 1))


@dataclass
class VerifyConfig:
    """Engine tunables. ``packs=None`` loads every built-in reference pack;
    entries are resolved through :func:`load_pack` (file paths work too)."""

    quote_min_words: int = 3
    entity_near_miss_threshold: float = 0.8
    packs: tuple[str, ...] | None = None
    #: D-018 — verify each fact against the section of the answer's SUBJECT,
    #: not the whole corpus (catches cross-attribution: a real value given to
    #: the wrong subject). Default False = global membership (unchanged).
    scoped: bool = False
    #: D-018 — closed-world: also strip UNVERIFIABLE atoms from the shown
    #: answer (only grounded facts reach the user). Default False (unchanged).
    strict: bool = False


class Verifier:
    """Verifies answers against a trusted :class:`CorpusDB` (groundedness).

    The four extractors are built ONCE at construction (pack YAML parsing
    and the glossary snapshot are not redone per call). The glossary is a
    snapshot: if the corpus drifts afterwards, a 'glossary' atom that no
    longer resolves is defensively NOT_FOUND.
    """

    def __init__(self, corpus: CorpusDB, config: VerifyConfig | None = None) -> None:
        self.corpus = corpus
        self.config = config if config is not None else VerifyConfig()
        pack_names = (
            builtin_pack_names() if self.config.packs is None else list(self.config.packs)
        )
        #: sorted (canonical, raw) snapshot — also the near-miss search space.
        self._glossary = corpus.entities()
        #: (canonical, raw, split tokens) — precomputed for the D-015
        #: abbreviation check, in the same sorted order as _glossary.
        self._glossary_tokens = [
            (canonical, raw, _entity_tokens(canonical)) for canonical, raw in self._glossary
        ]
        self._references = ReferenceExtractor([load_pack(n) for n in pack_names])
        self._numbers = NumberExtractor()
        self._quotes = QuoteExtractor(min_words=self.config.quote_min_words)
        self._entities = EntityExtractor(self._glossary)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def verify(self, answer: str, context: list[str] | None = None) -> Report:
        """Verify `answer`; `context` is optional trusted text for THIS call.

        Returns a :class:`Report` whose atoms are sorted by (start, end) and
        whose serialization is byte-identical for identical inputs (D-006).
        If the input itself already contains a removal marker (D-005), the
        report carries a warning: markers in ``corrected_answer`` are then
        not all VeriGate-issued.
        """
        answer_sha = hashlib.sha256(answer.encode("utf-8")).hexdigest()
        fingerprint = self.corpus.fingerprint()

        if not answer.strip():
            return Report(
                verdict=Verdict.UNVERIFIABLE,
                score=0.0,
                atoms=[],
                corrected_answer=answer,
                answer_sha256=answer_sha,
                corpus_fingerprint=fingerprint,
                warnings=[_WARN_EMPTY],
            )

        # Provenance guard: the removal marker is VeriGate's honesty signal
        # (D-005 — "marker present" must mean "VeriGate removed a false atom
        # here"). If the INPUT already carries one, it flows through to
        # corrected_answer untouched, so downstream consumers scanning for
        # markers could mistake it for a VeriGate redaction. Warn so the
        # report stays honest about marker provenance.
        warnings: list[str] = []
        if any(marker in answer for marker in REMOVAL_MARKERS.values()):
            warnings.append(_WARN_INPUT_MARKER)

        # Fixed extractor order, then GLOBAL dedupe: cross-extractor overlaps
        # collapse to the longest span (a quote swallows the SKU it cites —
        # the quote is checked as a whole; if false the whole span goes);
        # equal spans collapse to the earliest extractor in this list order.
        atoms: list[Atom] = []
        atoms.extend(self._references.extract(answer))
        atoms.extend(self._numbers.extract(answer))
        atoms.extend(self._quotes.extract(answer))
        atoms.extend(self._entities.extract(answer))
        atoms = dedupe_overlapping(atoms)

        # Context precomputation, once per call: canonical sets for refs and
        # numbers, canonical_text per chunk for quote/entity containment.
        ctx_refs: set[str] = set()
        ctx_numbers: set[str] = set()
        ctx_texts: list[str] = []
        for chunk in context or []:
            ctx_refs.update(a.canonical for a in self._references.extract(chunk))
            ctx_numbers.update(a.canonical for a in self._numbers.extract(chunk))
            ctx_texts.append(canonical_text(chunk))

        # D-018 scoped mode: the subjects are the corpus entities the answer is
        # about; each ref/number is then verified against THOSE subjects'
        # sections, not the whole corpus. With no detectable subject we cannot
        # scope, so we fall back to global membership and say so (refusing
        # everything would be wrong). subjects=None means "global path" — both
        # scoped=False and scoped-without-subject take it, so default behavior
        # is byte-identical.
        subjects: frozenset[str] | None = None
        if self.config.scoped:
            detected = self._detect_subjects(atoms)
            if detected:
                subjects = detected
            else:
                warnings.append(_WARN_SCOPE_SKIPPED)

        results = [
            self._adjudicate(atom, ctx_refs, ctx_numbers, ctx_texts, subjects)
            for atom in atoms
        ]
        results.sort(key=lambda r: (r.atom.start, r.atom.end))

        n_verified = sum(1 for r in results if r.status is AtomStatus.VERIFIED)
        n_false = sum(1 for r in results if r.status in FALSE_STATUSES)
        checkable = n_verified + n_false
        # D-003: unverifiable atoms are excluded from the denominator.
        score = n_verified / checkable if checkable else 0.0

        # D-002: the Beaume gradation — zero checkable atoms is UNVERIFIABLE
        # with score 0.0, never a vacuous 100%.
        if checkable == 0:
            verdict = Verdict.UNVERIFIABLE
            warnings.append(_WARN_NOTHING_CHECKABLE)
        elif n_false == 0:
            verdict = Verdict.VERIFIED
        elif score >= 0.5:
            verdict = Verdict.CORRECTED
        else:
            verdict = Verdict.INSUFFICIENT

        return Report(
            verdict=verdict,
            score=score,
            atoms=results,
            corrected_answer=rewrite_answer(
                answer, results, strip_unverifiable=self.config.strict
            ),
            answer_sha256=answer_sha,
            corpus_fingerprint=fingerprint,
            warnings=warnings,
        )

    def _detect_subjects(self, atoms: list[Atom]) -> frozenset[str]:
        """The corpus subjects the answer is about (D-018): for each ENTITY
        atom, its resolved corpus subject canonical — an exact glossary hit,
        or the entry it abbreviates (D-015, first in sorted-glossary order).
        Deterministic; an atom that resolves to nothing contributes nothing."""
        subjects: set[str] = set()
        for atom in atoms:
            if atom.type is not AtomType.ENTITY:
                continue
            if self.corpus.has_entity(atom.canonical) is not None:
                subjects.add(atom.canonical)
                continue
            cand_tokens = _entity_tokens(atom.canonical)
            for entry_canonical, _entry_raw, entry_tokens in self._glossary_tokens:
                if _is_contiguous_run(cand_tokens, entry_tokens) and (
                    self.corpus.has_entity(entry_canonical) is not None
                ):
                    subjects.add(entry_canonical)
                    break
        return frozenset(subjects)

    # ------------------------------------------------------------------ #
    # Adjudication
    # ------------------------------------------------------------------ #

    def _adjudicate(
        self,
        atom: Atom,
        ctx_refs: set[str],
        ctx_numbers: set[str],
        ctx_texts: list[str],
        subjects: frozenset[str] | None,
    ) -> AtomResult:
        """Adjudicate one atom: corpus first, then context, else false.

        ``subjects`` is None on the global path (scoped off, or scoped with no
        detectable subject); then ref/number checks use membership exactly as
        before. When a subject set is given (D-018), ref/number must be
        grounded FOR a subject (or a shared section); a value that exists
        globally but not for the subject is cross-attribution → MISMATCHED.
        """
        if atom.type is AtomType.REFERENCE:
            if subjects is not None:
                return self._scoped_ref_or_number(
                    atom,
                    self.corpus.has_reference_scoped(atom.canonical, subjects),
                    atom.canonical in ctx_refs,
                    self.corpus.has_reference(atom.canonical),
                    _DETAIL_REF_NOT_FOUND,
                    subjects,
                )
            doc = self.corpus.has_reference(atom.canonical)
            if doc is not None:
                return AtomResult(atom, AtomStatus.VERIFIED, matched_source=doc)
            if atom.canonical in ctx_refs:
                return AtomResult(atom, AtomStatus.VERIFIED, matched_source=_CONTEXT_SOURCE)
            return AtomResult(atom, AtomStatus.NOT_FOUND, detail=_DETAIL_REF_NOT_FOUND)

        if atom.type is AtomType.NUMBER:
            if subjects is not None:
                return self._scoped_ref_or_number(
                    atom,
                    self.corpus.has_number_scoped(atom.canonical, None, subjects),
                    atom.canonical in ctx_numbers,
                    self.corpus.has_number(atom.canonical),
                    _DETAIL_NUMBER_NOT_FOUND,
                    subjects,
                )
            doc = self.corpus.has_number(atom.canonical)
            if doc is not None:
                return AtomResult(atom, AtomStatus.VERIFIED, matched_source=doc)
            if atom.canonical in ctx_numbers:
                return AtomResult(atom, AtomStatus.VERIFIED, matched_source=_CONTEXT_SOURCE)
            return AtomResult(atom, AtomStatus.NOT_FOUND, detail=_DETAIL_NUMBER_NOT_FOUND)

        if atom.type is AtomType.QUOTE:
            doc = self.corpus.contains_text(atom.canonical)
            if doc is not None:
                return AtomResult(atom, AtomStatus.VERIFIED, matched_source=doc)
            if atom.canonical and any(atom.canonical in t for t in ctx_texts):
                return AtomResult(atom, AtomStatus.VERIFIED, matched_source=_CONTEXT_SOURCE)
            return AtomResult(atom, AtomStatus.NOT_FOUND, detail=_DETAIL_QUOTE_NOT_FOUND)

        return self._adjudicate_entity(atom, ctx_texts)

    def _scoped_ref_or_number(
        self,
        atom: Atom,
        scoped_doc: str | None,
        in_context: bool,
        global_doc: str | None,
        not_found_detail: str,
        subjects: frozenset[str],
    ) -> AtomResult:
        """D-018 scoped adjudication for a reference or number:
        in-scope hit → VERIFIED; else context → VERIFIED; else exists globally
        but not for the subject → MISMATCHED (cross-attribution, removed); else
        NOT_FOUND."""
        if scoped_doc is not None:
            return AtomResult(atom, AtomStatus.VERIFIED, matched_source=scoped_doc)
        if in_context:
            return AtomResult(atom, AtomStatus.VERIFIED, matched_source=_CONTEXT_SOURCE)
        if global_doc is not None:
            return AtomResult(
                atom,
                AtomStatus.MISMATCHED,
                detail=(
                    "value exists in the corpus but not for "
                    f"{', '.join(sorted(subjects))}: cross-attribution"
                ),
            )
        return AtomResult(atom, AtomStatus.NOT_FOUND, detail=not_found_detail)

    def _adjudicate_entity(self, atom: Atom, ctx_texts: list[str]) -> AtomResult:
        """Entity adjudication (unchanged by scoping — entities ARE the
        subjects): glossary membership, else the candidate path."""
        if atom.pack == "glossary":
            doc = self.corpus.has_entity(atom.canonical)
            if doc is not None:
                return AtomResult(atom, AtomStatus.VERIFIED, matched_source=doc)
            # Defensive: the extractor matched a snapshot entry the corpus no
            # longer has (glossary drift between __init__ and verify).
            return AtomResult(atom, AtomStatus.NOT_FOUND, detail=_DETAIL_GLOSSARY_DRIFT)
        return self._adjudicate_candidate(atom, ctx_texts)

    def _adjudicate_candidate(self, atom: Atom, ctx_texts: list[str]) -> AtomResult:
        """pack='glossary_candidate': corpus, then context containment, then
        D-015 abbreviation (contiguous token run of a glossary entry →
        VERIFIED), then nearest glossary entry — MISMATCHED if close,
        UNVERIFIABLE if not."""
        doc = self.corpus.has_entity(atom.canonical)
        if doc is not None:
            return AtomResult(atom, AtomStatus.VERIFIED, matched_source=doc)
        cand_text = canonical_text(atom.raw)
        if cand_text and any(cand_text in t for t in ctx_texts):
            return AtomResult(atom, AtomStatus.VERIFIED, matched_source=_CONTEXT_SOURCE)
        # D-015: abbreviating a real name is not a lie. A candidate whose
        # split tokens form a contiguous run inside a glossary entry — a
        # strict prefix, an inner segment, or the whole entry modulo
        # spacing — is the user shortening that entry, not inventing an
        # entity. First match in sorted-glossary order wins (deterministic);
        # an entry that no longer resolves in the corpus is skipped, like
        # the glossary-drift path above.
        cand_tokens = _entity_tokens(atom.canonical)
        for entry_canonical, entry_raw, entry_tokens in self._glossary_tokens:
            if not _is_contiguous_run(cand_tokens, entry_tokens):
                continue
            doc = self.corpus.has_entity(entry_canonical)
            if doc is None:
                continue
            return AtomResult(
                atom,
                AtomStatus.VERIFIED,
                matched_source=doc,
                detail=f"abbreviation of glossary entry '{entry_raw}'",
            )
        # Closest glossary entry; self._glossary is sorted by canonical and
        # the strict '>' keeps the first maximum, so ties break by (higher
        # ratio, lexicographically smaller entry canonical) — deterministic.
        best_ratio = -1.0
        best_raw = ""
        for entry_canonical, entry_raw in self._glossary:
            ratio = SequenceMatcher(None, atom.canonical, entry_canonical).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_raw = entry_raw
        if best_ratio >= self.config.entity_near_miss_threshold:
            return AtomResult(
                atom,
                AtomStatus.MISMATCHED,
                detail=f"no such entity in glossary; closest known: '{best_raw}'",
            )
        return AtomResult(atom, AtomStatus.UNVERIFIABLE, detail=_DETAIL_NO_CLOSE_MATCH)
