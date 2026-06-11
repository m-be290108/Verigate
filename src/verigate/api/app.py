"""Local HTTP API for VeriGate — a FastAPI surface over the SDK facade.

ON-PREMISE TRUST MODEL — read before deploying. This app is designed to
serve on **localhost only** (the CLI ``serve`` command refuses any
non-loopback bind, D-010). Whoever can reach the socket is the operator of
the machine, with the same powers a local shell already grants; therefore
there is deliberately **no authentication**, and ``POST /ingest`` accepts a
**server-local folder path** (the API ingests files that live on the
machine running VeriGate, not uploads). Exposing this app beyond loopback
is a deliberate customer deployment decision — their reverse proxy, their
access control — never something VeriGate does by itself.

The app holds exactly ONE :class:`~verigate.sdk.Gate` for its lifetime,
closed by the lifespan handler on shutdown. ``POST /ingest`` writes into
the SAME corpus database the Gate was opened on and then REOPENS the Gate:
the verifier's glossary snapshot and reference/number lookups must reflect
the new corpus immediately (the snapshot taken at construction would
otherwise go stale — proven by the 'ingest-then-verify sees new documents'
test). The swap happens under the app-wide lock, so no request ever
observes a half-built corpus or a closed Gate.

THREADING MODEL — one Gate + ``check_same_thread=False`` + one lock.
FastAPI runs the sync ``def`` endpoints below in a threadpool, so requests
(and the lifespan shutdown) touch the Gate from threads other than the one
``create_app()`` ran on; sqlite3's default thread-affinity guard would
reject that (``sqlite3.ProgrammingError``). The Gate is therefore opened
with ``check_same_thread=False`` and EVERY use of it — verify, ingest's
reopen-and-swap, health reads, audit export, the lifespan close — is
serialized by the single ``app.state.lock``. The lock is not optional
decoration: without it, concurrent requests would interleave statements on
one shared sqlite connection (undefined behaviour), and a verify could
observe the half-swapped Gate mid-ingest. Chosen over a fresh Gate per
request because the Verifier's pack parsing and glossary snapshot are
built once, the swap point for ingest stays atomic, and a localhost
single-operator API has no concurrency to win back. (AuditTrail already
opens its connection with ``check_same_thread=False`` behind its own
internal lock — no change needed there.)

100% deterministic and offline: no LLM, no outbound network call anywhere.
The endpoints are thin translations between HTTP and the SDK — every
verification rule lives in :mod:`verigate.verify.engine`.
"""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import verigate
from verigate.audit.trail import AuditIntegrityError
from verigate.ingest.ingestor import ingest_folder
from verigate.sdk import Gate
from verigate.verify.engine import VerifyConfig


class VerifyRequest(BaseModel):
    """Body of ``POST /verify``. `answer` is required (422 when missing);
    `context` is optional trusted text for this call only (RAG chunks)."""

    answer: str
    context: list[str] | None = None


class IngestRequest(BaseModel):
    """Body of ``POST /ingest``. Both paths are SERVER-LOCAL (see the
    module docstring for the localhost trust model)."""

    folder: str
    glossary: str | None = None


def create_app(
    corpus_db: str | Path,
    audit_db: str | Path | None = None,
    config: VerifyConfig | None = None,
    cache_size: int = 0,
) -> FastAPI:
    """Build the FastAPI app over one :class:`Gate` opened on `corpus_db`.

    The Gate is created eagerly so a missing corpus database raises
    :class:`FileNotFoundError` at ``create_app()`` time (the CLI ``serve``
    command relies on this), and closed by the lifespan handler on
    shutdown. With `audit_db` set, every ``POST /verify`` is journaled in
    the tamper-evident audit trail and ``GET /audit/export`` serves the
    verified CSV export. `cache_size` enables the Gate's opt-in LRU report
    cache (D-014) — repeats of the same (answer, context) skip the engine;
    audit entries are still recorded per call.
    """
    corpus_path = Path(corpus_db)
    audit_path = Path(audit_db) if audit_db is not None else None

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            with app_.state.lock:
                app_.state.gate.close()

    app = FastAPI(
        title="VeriGate",
        version=verigate.__version__,
        description="Deterministic verification layer for AI answers — "
        "no LLM, offline, localhost only (on-premise trust model).",
        lifespan=lifespan,
    )
    # One Gate for the app lifetime, opened with check_same_thread=False —
    # sync endpoints run in a threadpool (module docstring). The lock
    # serializes EVERY Gate use: it is what makes the relaxed thread guard
    # safe, and it makes the ingest-time reopen-and-swap atomic.
    app.state.gate = Gate(
        corpus_path,
        audit_db=audit_path,
        config=config,
        check_same_thread=False,
        cache_size=cache_size,
    )
    app.state.lock = threading.Lock()

    # NOTE: the handlers below read the Gate's corpus/audit handles
    # (`_corpus`, `_audit`). The API is a same-package facade over the SDK;
    # those attributes are private to the package boundary, not to this file.

    @app.post("/verify")
    def verify(req: VerifyRequest) -> dict:
        """Verify one answer against the trusted corpus; returns the full
        deterministic report (``Report.to_dict()``). When the app was
        created with an audit database, exactly one audit entry is recorded
        per call (sha256 of the answer, never the raw text)."""
        with app.state.lock:
            report = app.state.gate.verify(req.answer, req.context)
        return report.to_dict()

    @app.post("/ingest")
    def ingest(req: IngestRequest) -> dict:
        """Ingest a SERVER-LOCAL folder into the app's corpus database.

        `folder` (and the optional `glossary`) are paths on the machine
        running VeriGate — this is an on-premise localhost API and that is
        the trust model (module docstring): a caller who can reach the
        loopback socket can already read those files. A bad folder or a
        malformed glossary is a 400 with the precise message. On success
        the Gate is reopened so verification immediately sees the new
        corpus content, and the :class:`IngestResult` is returned as JSON
        (skipped files listed with their reasons — never silent).
        """
        with app.state.lock:
            try:
                result = ingest_folder(
                    req.folder, corpus_path, glossary_path=req.glossary
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # Reopen: the Verifier's glossary snapshot is taken at
            # construction; lookups must see the documents just ingested.
            # Same check_same_thread=False + lock discipline as create_app.
            # The fresh Gate also restarts the report cache: cached reports
            # were rendered against the pre-ingest corpus fingerprint.
            new_gate = Gate(
                corpus_path,
                audit_db=audit_path,
                config=config,
                check_same_thread=False,
                cache_size=cache_size,
            )
            app.state.gate.close()
            app.state.gate = new_gate
        return asdict(result)

    @app.get("/health")
    def health() -> dict:
        """Liveness + corpus identity: version, fingerprint, document count."""
        with app.state.lock:
            corpus = app.state.gate._corpus
            fingerprint = corpus.fingerprint()
            documents = corpus.doc_count()
        return {
            "status": "ok",
            "version": verigate.__version__,
            "corpus_fingerprint": fingerprint,
            "documents": documents,
        }

    @app.get("/audit/export")
    def audit_export() -> Response:
        """Export the audit trail as CSV (chain verified first).

        404 when the app was created without an audit database; 409 with
        the integrity-error list when the chain fails verification — a
        broken chain is a conflict surfaced loudly, never an empty 200.
        """
        with app.state.lock:
            trail = app.state.gate._audit
            if trail is None:
                raise HTTPException(status_code=404, detail="audit trail not configured")
            try:
                content = trail.export_csv()
            except AuditIntegrityError as exc:
                return JSONResponse(status_code=409, content={"detail": exc.errors})
        return Response(content=content, media_type="text/csv")

    return app
