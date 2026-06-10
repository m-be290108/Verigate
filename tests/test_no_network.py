"""The "0 network" invariant, tested mechanically (Beaume pattern:
lucie test_no_network.py — monkeypatched socket.connect raises on any
outbound attempt).

This is a contractual property of the product (on-premise: the data never
leaves), so it is enforced on the FULL pipeline — ingest, verify, corpus
integrity, audit export — not just on unit slices.
"""

from __future__ import annotations

import pytest

from verigate.corpus import CorpusDB
from verigate.ingest.ingestor import ingest_folder
from verigate.types import Verdict
from verigate.verify.engine import Verifier

pytestmark = pytest.mark.network_guard


def test_full_pipeline_makes_zero_network_calls(no_network, sample_corpus_dir, tmp_path):
    """ingest → verify (clean + hallucinated) → verify_corpus, sockets blocked."""
    db_path = tmp_path / "corpus.db"
    result = ingest_folder(sample_corpus_dir, db_path)
    assert result.n_docs >= 3

    with CorpusDB(db_path) as db:
        verifier = Verifier(db)

        clean = verifier.verify(
            "The AquaPump 3000 (SKU AP-3000-X) costs €249.99."
        )
        assert clean.verdict == Verdict.VERIFIED

        dirty = verifier.verify(
            "Our flagship [REF: ZZ-9999-Q] is priced at €999.99."
        )
        assert dirty.verdict != Verdict.VERIFIED
        assert "⟨" in dirty.corrected_answer

        ok, errors = db.verify_corpus()
        assert ok, errors


def test_audit_trail_makes_zero_network_calls(no_network, tmp_path):
    from verigate.audit.trail import AuditTrail

    trail = AuditTrail(tmp_path / "audit.db")
    trail.record("verify", data={"answer_sha256": "ab" * 32, "verdict": "VERIFIED"})
    valid, errors = trail.verify_chain()
    assert valid, errors
    csv_text = trail.export_csv()
    assert csv_text.startswith('"Date"')
    trail.close()
