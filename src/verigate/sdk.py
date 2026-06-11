"""SDK facade — the three-line VeriGate integration (the selling point)::

    from verigate import Gate

    gate = Gate("corpus.db")
    report = gate.verify(answer)

:class:`Gate` wires together the trusted corpus (:class:`CorpusDB`), one
verification engine (:class:`Verifier`, built once — packs and glossary are
not re-parsed per call) and, optionally, the tamper-evident audit trail
(:class:`AuditTrail`). 100% deterministic and offline: no LLM, no network,
no clock in reports (D-006).

Privacy by design — when an audit trail is configured, each ``verify`` call
journals the sha256 of the answer, the verdict, the score and the canonical
keys of the rejected atoms. The raw answer text is NEVER written to the
audit database: the compliance journal must prove what was checked (and
against which corpus fingerprint) without becoming a copy of the customer's
data.
"""

from __future__ import annotations

import copy
from collections import OrderedDict
from pathlib import Path

from verigate.audit.trail import AuditTrail
from verigate.corpus import CorpusDB
from verigate.ingest.ingestor import IngestResult, ingest_folder
from verigate.types import FALSE_STATUSES, Report
from verigate.verify.engine import Verifier, VerifyConfig


class Gate:
    """Verify AI answers against a trusted corpus, in three lines.

    Opens the corpus database at `corpus_db` (a missing file raises
    :class:`FileNotFoundError` — sqlite must never silently create an empty
    corpus) and builds ONE :class:`Verifier`. If `audit_db` is given, every
    ``verify`` call is journaled in a tamper-evident :class:`AuditTrail`.
    """

    def __init__(
        self,
        corpus_db: str | Path,
        audit_db: str | Path | None = None,
        config: VerifyConfig | None = None,
        check_same_thread: bool = True,
        cache_size: int = 0,
    ) -> None:
        # `check_same_thread` is forwarded to :class:`CorpusDB` (and from
        # there to sqlite3.connect). The default keeps sqlite3's own
        # thread-affinity guard; the API layer passes False and serializes
        # every Gate use with one lock (see verigate.api.app). AuditTrail
        # already carries its own internal lock and needs no forwarding.
        self._corpus = CorpusDB(corpus_db, check_same_thread=check_same_thread)
        self._verifier = Verifier(self._corpus, config)
        self._audit: AuditTrail | None = (
            AuditTrail(audit_db) if audit_db is not None else None
        )
        # Opt-in memoization (D-014): determinism makes (answer, context) a
        # complete cache key for a given Gate — the corpus is fixed for the
        # instance's lifetime. cache_size=0 (default) disables it.
        self._cache_size = max(0, int(cache_size))
        self._cache: OrderedDict[tuple[str, tuple[str, ...]], Report] = OrderedDict()

    # ------------------------------------------------------------------ #
    # Verification
    # ------------------------------------------------------------------ #

    def verify(self, answer: str, context: list[str] | None = None) -> Report:
        """Verify `answer` (groundedness, see the engine docstring); return
        the :class:`Report`. `context` is optional trusted text for this
        call only (e.g. the RAG chunks the answer was generated from).

        If an audit trail is configured, exactly one entry is recorded per
        call, with ``action='verify'`` and the verification facts only —
        the sha256 of the answer, never the raw answer (privacy by design,
        see module docstring): verdict, score, number of false atoms, the
        sorted canonical keys of the rejected (false) atoms, and the corpus
        fingerprint the verdict was rendered against.

        With ``cache_size > 0``, repeated (answer, context) pairs are served
        from an LRU cache — byte-identical by determinism (D-014). A cache
        hit is still a verification EVENT: the audit entry is recorded on
        every call, hit or miss (the journal logs events, not computations).
        """
        key = (answer, tuple(context or ()))
        cached = self._cache.get(key) if self._cache_size else None
        if cached is not None:
            self._cache.move_to_end(key)
            # Deep copy so a caller mutating the returned report cannot
            # poison the cache (reports are plain mutable dataclasses).
            report = copy.deepcopy(cached)
        else:
            report = self._verifier.verify(answer, context)
            if self._cache_size:
                self._cache[key] = copy.deepcopy(report)
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
        if self._audit is not None:
            rejected = sorted(
                {r.atom.canonical for r in report.atoms if r.status in FALSE_STATUSES}
            )
            self._audit.record(
                action="verify",
                data={
                    "answer_sha256": report.answer_sha256,
                    "verdict": report.verdict.value,
                    "score": round(report.score, 4),
                    "n_false": report.n_false,
                    "rejected": rejected,
                    "corpus_fingerprint": report.corpus_fingerprint,
                },
            )
        return report

    def verify_corpus(self) -> tuple[bool, list[str]]:
        """Run the corpus integrity lock; returns ``(ok, errors)``
        (delegates to :meth:`CorpusDB.verify_corpus`)."""
        return self._corpus.verify_corpus()

    # ------------------------------------------------------------------ #
    # Ingestion (static — no Gate instance needed to build a corpus)
    # ------------------------------------------------------------------ #

    @staticmethod
    def ingest(
        folder: str | Path,
        db_path: str | Path,
        packs: list[str] | None = None,
        glossary_path: str | Path | None = None,
    ) -> IngestResult:
        """Build (or update) a corpus database from a folder of trusted
        documents — thin delegate to :func:`ingest_folder`."""
        return ingest_folder(folder, db_path, packs=packs, glossary_path=glossary_path)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Close the audit trail (if any) and the corpus database."""
        if self._audit is not None:
            self._audit.close()
        self._corpus.close()

    def __enter__(self) -> Gate:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
