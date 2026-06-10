"""Tests for the SDK facade (Gate) and the CLI (verigate.cli).

Everything is deterministic and offline: real files in tmp_path, real
sqlite, the in-repo sample corpus fixture — no network, no LLM. The CLI is
exercised through ``main(argv)`` (in-process, exit code returned); the only
exception is the encoding-robustness test, which MUST run a subprocess to
observe the interpreter's own exit behavior under a hostile
``PYTHONIOENCODING``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from verigate import Gate  # exercises the lazy __getattr__ export
from verigate.audit.trail import AuditTrail
from verigate.cli import main
from verigate.corpus import CorpusDB
from verigate.ingest.ingestor import IngestResult
from verigate.types import Verdict

# ------------------------------------------------------------- test data
#
# Answers crafted against the sample_corpus_dir fixture (catalog.md +
# policy.txt + products.csv): AquaPump 3000 / SKU AP-3000-X / €249.99 are
# in the corpus; €999.99 and ZZ-9999-Q are not.

VERIFIED_ANSWER = "The AquaPump 3000 (SKU AP-3000-X) costs €249.99."
CORRECTED_ANSWER = "The AquaPump 3000 costs €999.99."
INSUFFICIENT_ANSWER = "It costs €999.99 and uses part ZZ-9999-Q."
UNVERIFIABLE_ANSWER = "Hello there, nothing checkable here."

#: One false reference, groundable by a context chunk.
CTX_ANSWER = "Use spare part GR-77-X9 for the repair."
CTX_CHUNK = "Approved spare parts list: GR-77-X9 is certified."

EXPECTED_AUDIT_KEYS = {
    "answer_sha256",
    "verdict",
    "score",
    "n_false",
    "rejected",
    "corpus_fingerprint",
}


@pytest.fixture
def corpus_db(sample_corpus_dir, tmp_path):
    """The sample corpus folder ingested via the SDK facade."""
    db_path = tmp_path / "corpus.db"
    Gate.ingest(sample_corpus_dir, db_path)
    return db_path


@pytest.fixture
def audit_env(monkeypatch):
    """Force the persistent-secret-file path (F14-a), whatever the host env."""
    monkeypatch.delenv("VERIGATE_AUDIT_SECRET", raising=False)


def _audit_rows(audit_path):
    conn = sqlite3.connect(audit_path)
    try:
        return conn.execute(
            "SELECT action, data_json FROM audit_entries ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()


# ============================================================== SDK: Gate


def test_gate_three_line_happy_path(no_network, sample_corpus_dir, tmp_path):
    Gate.ingest(sample_corpus_dir, tmp_path / "corpus.db")
    gate = Gate(tmp_path / "corpus.db")
    report = gate.verify(VERIFIED_ANSWER)
    gate.close()
    assert report.verdict is Verdict.VERIFIED
    assert report.score == 1.0
    assert report.corrected_answer == VERIFIED_ANSWER
    assert report.corpus_fingerprint


def test_gate_missing_corpus_db_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        Gate(tmp_path / "missing.db")


def test_gate_ingest_returns_ingest_result(sample_corpus_dir, tmp_path):
    result = Gate.ingest(sample_corpus_dir, tmp_path / "c.db")
    assert isinstance(result, IngestResult)
    assert result.n_docs == 3
    assert result.fingerprint
    assert result.skipped == ()


def test_gate_verify_corpus_delegates_ok(corpus_db):
    with Gate(corpus_db) as gate:
        ok, errors = gate.verify_corpus()
    assert ok is True
    assert errors == []


def test_gate_context_grounds_false_atom(corpus_db):
    with Gate(corpus_db) as gate:
        without = gate.verify(CTX_ANSWER)
        with_ctx = gate.verify(CTX_ANSWER, context=[CTX_CHUNK])
    assert without.verdict is Verdict.INSUFFICIENT
    assert with_ctx.verdict is Verdict.VERIFIED
    assert with_ctx.atoms[0].matched_source == "context"


def test_gate_verify_writes_exactly_one_audit_entry_with_dictated_keys(
    corpus_db, tmp_path, audit_env
):
    audit_path = tmp_path / "audit.db"
    with Gate(corpus_db, audit_db=audit_path) as gate:
        report = gate.verify(CORRECTED_ANSWER)
    rows = _audit_rows(audit_path)
    assert len(rows) == 1
    action, data_json = rows[0]
    assert action == "verify"
    data = json.loads(data_json)
    assert set(data) == EXPECTED_AUDIT_KEYS
    assert data["answer_sha256"] == report.answer_sha256
    assert data["verdict"] == "CORRECTED"
    assert data["score"] == 0.5
    assert data["n_false"] == 1
    assert data["rejected"] == ["money:EUR:999.99"]
    assert data["corpus_fingerprint"] == report.corpus_fingerprint


def test_gate_verify_one_entry_per_call(corpus_db, tmp_path, audit_env):
    audit_path = tmp_path / "audit.db"
    with Gate(corpus_db, audit_db=audit_path) as gate:
        gate.verify(VERIFIED_ANSWER)
        gate.verify(CORRECTED_ANSWER)
        gate.verify(UNVERIFIABLE_ANSWER)
    rows = _audit_rows(audit_path)
    assert len(rows) == 3
    assert [action for action, _ in rows] == ["verify", "verify", "verify"]


def test_gate_audit_db_contains_no_raw_answer_text(corpus_db, tmp_path, audit_env):
    audit_path = tmp_path / "audit.db"
    answer = "Keep this gossip private: the AquaPump 3000 costs €999.99."
    with Gate(corpus_db, audit_db=audit_path) as gate:
        gate.verify(answer)
    blobs = "\n".join(data_json for _, data_json in _audit_rows(audit_path))
    assert answer not in blobs
    assert "gossip" not in blobs
    assert "private" not in blobs
    # Hash instead of text — the privacy-by-design contract.
    assert hashlib.sha256(answer.encode("utf-8")).hexdigest() in blobs


def test_gate_audit_chain_verifies_after_several_calls(corpus_db, tmp_path, audit_env):
    audit_path = tmp_path / "audit.db"
    with Gate(corpus_db, audit_db=audit_path) as gate:
        for answer in (VERIFIED_ANSWER, CORRECTED_ANSWER, INSUFFICIENT_ANSWER, CTX_ANSWER):
            gate.verify(answer)
    with AuditTrail(audit_path) as trail:
        valid, errors = trail.verify_chain()
        count = trail.entry_count()
    assert valid is True
    assert errors == []
    assert count == 4


def test_gate_without_audit_db_writes_nothing(corpus_db, tmp_path):
    before = set(tmp_path.rglob("*"))
    with Gate(corpus_db) as gate:
        gate.verify(CORRECTED_ANSWER)
        gate.verify(VERIFIED_ANSWER)
    after = set(tmp_path.rglob("*"))
    assert after == before


# ============================================================ CLI: ingest


def test_cli_ingest_prints_counts_fingerprint_and_skipped(
    sample_corpus_dir, tmp_path, capsys
):
    (sample_corpus_dir / "notes.xyz").write_text("not ingestible", encoding="utf-8")
    (sample_corpus_dir / "broken.txt").write_bytes(b"\xff\xfe broken")
    reference = Gate.ingest(sample_corpus_dir, tmp_path / "reference.db")

    rc = main(["ingest", str(sample_corpus_dir), "--db", str(tmp_path / "corpus.db")])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"documents: {reference.n_docs}" in out
    assert f"references: {reference.n_refs}" in out
    assert f"numbers: {reference.n_numbers}" in out
    assert f"entities: {reference.n_entities}" in out
    assert f"fingerprint: {reference.fingerprint}" in out
    # Skipped files are reported with their reason — never silent.
    assert "skipped: notes.xyz (unsupported extension .xyz)" in out
    assert "skipped: broken.txt" in out
    assert "not valid UTF-8" in out


def test_cli_ingest_bad_folder_exit_1(tmp_path, capsys):
    rc = main(["ingest", str(tmp_path / "no_such_dir"), "--db", str(tmp_path / "c.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not an existing folder" in captured.err


def test_cli_ingest_bad_glossary_exit_1(sample_corpus_dir, tmp_path, capsys):
    bad = tmp_path / "bad_glossary.yaml"
    bad.write_text("entities: 42\n", encoding="utf-8")
    rc = main(
        [
            "ingest",
            str(sample_corpus_dir),
            "--db",
            str(tmp_path / "c.db"),
            "--glossary",
            str(bad),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "glossary" in captured.err


# ============================================================ CLI: verify


@pytest.mark.parametrize(
    ("answer", "expected_exit", "verdict"),
    [
        (VERIFIED_ANSWER, 0, "VERIFIED"),
        (CORRECTED_ANSWER, 1, "CORRECTED"),
        (INSUFFICIENT_ANSWER, 2, "INSUFFICIENT"),
        (UNVERIFIABLE_ANSWER, 3, "UNVERIFIABLE"),
    ],
)
def test_cli_verify_exit_codes_mirror_verdicts(
    corpus_db, capsys, answer, expected_exit, verdict
):
    rc = main(["verify", "--db", str(corpus_db), answer])
    out = capsys.readouterr().out
    assert rc == expected_exit
    assert verdict in out


def test_cli_verify_json_round_trips(corpus_db, capsys):
    rc = main(["verify", "--db", str(corpus_db), "--json", CORRECTED_ANSWER])
    out = capsys.readouterr().out
    data = json.loads(out)
    with Gate(corpus_db) as gate:
        expected = gate.verify(CORRECTED_ANSWER).to_dict()
    assert rc == 1
    assert data == expected


def test_cli_verify_stdin_dash(corpus_db, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(VERIFIED_ANSWER))
    rc = main(["verify", "--db", str(corpus_db), "-"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "VERIFIED" in out


def test_cli_verify_stdin_when_answer_absent(corpus_db, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(CORRECTED_ANSWER))
    rc = main(["verify", "--db", str(corpus_db)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "CORRECTED" in out


def test_cli_verify_context_file_feeds_context(corpus_db, tmp_path, capsys):
    ctx = tmp_path / "ctx.txt"
    ctx.write_text(CTX_CHUNK, encoding="utf-8")
    rc_without = main(["verify", "--db", str(corpus_db), CTX_ANSWER])
    rc_with = main(
        ["verify", "--db", str(corpus_db), "--context-file", str(ctx), CTX_ANSWER]
    )
    capsys.readouterr()
    assert rc_without == 2  # INSUFFICIENT: the reference is not in the corpus
    assert rc_with == 0  # VERIFIED: the context chunk grounds it


def test_cli_verify_missing_context_file_is_operational_error(corpus_db, capsys):
    rc = main(
        ["verify", "--db", str(corpus_db), "--context-file", "/no/such/ctx.txt", CTX_ANSWER]
    )
    captured = capsys.readouterr()
    assert rc == 4
    assert "context file" in captured.err


def test_cli_verify_missing_corpus_db_is_operational_error(tmp_path, capsys):
    rc = main(["verify", "--db", str(tmp_path / "nope.db"), VERIFIED_ANSWER])
    captured = capsys.readouterr()
    assert rc == 4
    assert "not found" in captured.err


def test_cli_verify_human_output_icons_and_corrected_answer(corpus_db, capsys):
    answer = (
        "The AquaPump 3000 costs €999.99. "
        "The AquaPump Deluxe Edition Pro is unreleased."
    )
    rc = main(["verify", "--db", str(corpus_db), answer])
    out = capsys.readouterr().out
    assert rc == 1
    assert "✅" in out  # verified entity AquaPump 3000
    assert "❌" in out  # not_found money figure
    assert "➖" in out  # unverifiable near-glossary candidate
    assert "Corrected answer:" in out
    assert "⟨unverified figure, removed⟩" in out


def test_cli_verify_human_output_mismatched_icon(corpus_db, capsys):
    rc = main(["verify", "--db", str(corpus_db), "The AquaPump 9000 is great."])
    out = capsys.readouterr().out
    assert rc == 2  # only atom is false -> INSUFFICIENT
    assert "❌" in out
    assert "mismatched" in out
    assert "AquaPump 3000" in out  # closest-known detail surfaces in the line


def test_cli_verify_human_output_unverifiable_warns(corpus_db, capsys):
    rc = main(["verify", "--db", str(corpus_db), UNVERIFIABLE_ANSWER])
    out = capsys.readouterr().out
    assert rc == 3
    assert "warning:" in out
    assert "Corrected answer:" not in out  # nothing was removed


def test_cli_verify_audit_db_flag_records_entry(corpus_db, tmp_path, capsys, audit_env):
    audit_path = tmp_path / "cli_audit.db"
    rc = main(
        ["verify", "--db", str(corpus_db), "--audit-db", str(audit_path), VERIFIED_ANSWER]
    )
    capsys.readouterr()
    assert rc == 0
    rows = _audit_rows(audit_path)
    assert len(rows) == 1
    assert rows[0][0] == "verify"


# ===================================================== CLI: verify-corpus


def test_cli_verify_corpus_ok(corpus_db, capsys):
    rc = main(["verify-corpus", "--db", str(corpus_db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("OK")
    with CorpusDB(corpus_db) as db:
        fingerprint = db.fingerprint()
    assert fingerprint in out


def test_cli_verify_corpus_corrupted_exit_1(corpus_db, capsys):
    conn = sqlite3.connect(corpus_db)
    conn.execute(
        "UPDATE documents SET text = text || ' TAMPERED', sha256 = 'deadbeef'"
        " WHERE id = 'policy.txt'"
    )
    conn.commit()
    conn.close()
    rc = main(["verify-corpus", "--db", str(corpus_db)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "canonical column does not match text" in captured.err
    assert "fingerprint" in captured.err


def test_cli_verify_corpus_missing_db_exit_1(tmp_path, capsys):
    rc = main(["verify-corpus", "--db", str(tmp_path / "nope.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not found" in captured.err


# ====================================================== CLI: audit-export


def _make_audit_db(corpus_db, tmp_path, n=3):
    audit_path = tmp_path / "audit.db"
    with Gate(corpus_db, audit_db=audit_path) as gate:
        for _ in range(n):
            gate.verify(CORRECTED_ANSWER)
    return audit_path


def test_cli_audit_export_healthy_prints_csv(corpus_db, tmp_path, capsys, audit_env):
    audit_path = _make_audit_db(corpus_db, tmp_path)
    rc = main(["audit-export", "--db", str(audit_path)])
    out = capsys.readouterr().out
    assert rc == 0
    lines = out.splitlines()
    assert lines[0] == '"Date";"Action";"User";"Justification";"PrevHash";"Signature";"Sequence"'
    assert len(lines) == 1 + 3  # header + one row per journaled verify
    assert '"verify"' in out


def test_cli_audit_export_out_writes_file(corpus_db, tmp_path, capsys, audit_env):
    audit_path = _make_audit_db(corpus_db, tmp_path)
    out_file = tmp_path / "export.csv"
    rc = main(["audit-export", "--db", str(audit_path), "--out", str(out_file)])
    capsys.readouterr()
    assert rc == 0
    content = out_file.read_text(encoding="utf-8")
    assert content.startswith('"Date";"Action"')


def test_cli_audit_export_tampered_exit_1(corpus_db, tmp_path, capsys, audit_env):
    audit_path = _make_audit_db(corpus_db, tmp_path)
    conn = sqlite3.connect(audit_path)
    conn.execute("UPDATE audit_entries SET user = 'intruder' WHERE sequence = 1")
    conn.commit()
    conn.close()
    rc = main(["audit-export", "--db", str(audit_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "Signature mismatch" in captured.err
    assert '"Date"' not in captured.out  # a broken chain is never exported


def test_cli_audit_export_missing_db_exit_1(tmp_path, capsys):
    rc = main(["audit-export", "--db", str(tmp_path / "nope.db")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "not found" in captured.err


# ========================================== CLI: output encoding (D-010)

#: Absolute src/ path so the subprocess imports this working tree.
_SRC_DIR = str(Path(__file__).resolve().parents[1] / "src")


@pytest.mark.parametrize("json_flag", [False, True], ids=["human", "json"])
def test_cli_verify_ascii_stdout_never_aliases_a_verdict(corpus_db, json_flag):
    """Regression: the report contains non-ASCII by construction (verdict
    em-dash, ✅/❌ icons, ⟨…⟩ markers, the answer's own €). Without forced
    UTF-8 stdout, PYTHONIOENCODING=ascii made print() raise
    UnicodeEncodeError and the interpreter exit 1 — which D-010 defines as
    CORRECTED. The CLI must exit with the true verdict (0 here) or the
    operational code 4; never 1/2/3 caused by an encoding failure."""
    env = dict(os.environ, PYTHONIOENCODING="ascii")
    env["PYTHONPATH"] = _SRC_DIR + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "verigate.cli", "verify", "--db", str(corpus_db)]
    if json_flag:
        cmd.append("--json")
    cmd.append(VERIFIED_ANSWER)
    proc = subprocess.run(cmd, capture_output=True, check=False, env=env)
    assert proc.returncode in (0, 4), (
        f"exit {proc.returncode} aliases a verdict; "
        f"stderr: {proc.stderr.decode('utf-8', 'replace')}"
    )
    if proc.returncode == 0:
        # The fix forces UTF-8: the report bytes do not depend on the locale.
        out = proc.stdout.decode("utf-8")
        assert "VERIFIED" in out


class _AsciiOnlyStdout:
    """A stdout WITHOUT ``.reconfigure()`` that refuses non-ASCII writes —
    models a wrapped/legacy stream the hasattr guard cannot repair."""

    def write(self, s: str) -> int:
        s.encode("ascii")  # raises UnicodeEncodeError on the em-dash/icons
        return len(s)

    def flush(self) -> None:
        pass


def test_cli_verify_unencodable_stdout_is_operational_error(
    corpus_db, monkeypatch, capsys
):
    # When stdout cannot be reconfigured AND cannot encode the report, the
    # exit code must be 4 (operational), never the interpreter's 1.
    monkeypatch.setattr(sys, "stdout", _AsciiOnlyStdout())
    rc = main(["verify", "--db", str(corpus_db), VERIFIED_ANSWER])
    captured = capsys.readouterr()
    assert rc == 4
    assert "cannot write the report to stdout" in captured.err


# ============================================================= CLI: serve


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.7", "example.com"])
def test_cli_serve_refuses_non_loopback_host(corpus_db, capsys, host):
    # The refusal happens BEFORE the lazy uvicorn/api imports: this passes
    # even though verigate.api.app does not exist yet (later phase), which
    # proves no import was attempted.
    rc = main(["serve", "--db", str(corpus_db), "--host", host])
    captured = capsys.readouterr()
    assert rc == 1
    assert "localhost only" in captured.err
    assert host in captured.err
