"""Tests for the tamper-evident audit trail (verigate.audit.trail).

Covers the three 2026-06-10 audit regressions ported from Beaume:
F14-a (secret persistence), F14-b (no sequence collision on back-to-back
writes), F14-c (out-of-DB anchor detects trailing truncation).
"""

from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import threading

import pytest

from verigate.audit.trail import AuditIntegrityError, AuditTrail, PiiPseudonymizer

SECRET_ENV = "VERIGATE_AUDIT_SECRET"


@pytest.fixture(autouse=True)
def _no_env_secret(monkeypatch):
    """Isolate every test from a developer's real environment secret."""
    monkeypatch.delenv(SECRET_ENV, raising=False)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "audit.db"


@pytest.fixture
def trail(db_path):
    t = AuditTrail(db_path)
    yield t
    t.close()


def _raw_sql(db_path, sql, params=()):
    """Tamper with the DB through a separate connection, like an attacker."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _mode(path) -> int:
    return os.stat(path).st_mode & 0o777


# ---------------------------------------------------------------------------
# Recording and chain validity
# ---------------------------------------------------------------------------


def test_record_returns_completed_entry(trail):
    entry = trail.record("corpus.ingested", data={"docs": 3}, user="alice")
    assert entry.sequence == 1
    assert entry.prev_hash == "GENESIS"
    assert len(entry.signature) == 64  # hex SHA-256
    assert entry.user == "alice"


def test_chain_valid_after_records(trail):
    for i in range(10):
        trail.record(f"action.{i}", data={"i": i})
    valid, errors = trail.verify_chain()
    assert valid
    assert errors == []


def test_sequences_consecutive_from_one(trail):
    entries = [trail.record("a", data={"i": i}) for i in range(7)]
    assert [e.sequence for e in entries] == list(range(1, 8))


def test_two_records_back_to_back_no_collision(trail):
    # F14-b regression: two consecutive record() calls used to be allocated
    # the same sequence, and the second entry was silently lost.
    e1 = trail.record("first")
    e2 = trail.record("second")
    assert (e1.sequence, e2.sequence) == (1, 2)
    assert trail.entry_count() == 2
    assert e2.prev_hash == e1.signature


def test_entry_count(trail):
    assert trail.entry_count() == 0
    trail.record("a")
    trail.record("b")
    assert trail.entry_count() == 2


def test_context_manager(db_path):
    with AuditTrail(db_path) as t:
        t.record("inside")
        assert t.entry_count() == 1
    t2 = AuditTrail(db_path)
    try:
        valid, _ = t2.verify_chain()
        assert valid
    finally:
        t2.close()


# ---------------------------------------------------------------------------
# Secret persistence and priority (F14-a)
# ---------------------------------------------------------------------------


def test_secret_persists_across_instances(db_path):
    # F14-a regression: without persistence, a new process regenerated the
    # secret and verify_chain() failed on every entry after a restart.
    t1 = AuditTrail(db_path)
    for i in range(5):
        t1.record("restart.survivor", data={"i": i})
    t1.close()

    t2 = AuditTrail(db_path)
    try:
        valid, errors = t2.verify_chain()
        assert valid, errors
    finally:
        t2.close()


def test_env_secret_beats_file(db_path, tmp_path, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, "env-secret")
    t1 = AuditTrail(db_path)
    t1.record("with.env")
    t1.close()
    # The env path never touches the persistent secret file.
    assert not (tmp_path / ".audit.db.hmac_secret").exists()

    t2 = AuditTrail(db_path, secret=b"env-secret")
    try:
        valid, errors = t2.verify_chain()
        assert valid, errors
    finally:
        t2.close()


def test_explicit_secret_beats_env(db_path, monkeypatch):
    monkeypatch.setenv(SECRET_ENV, "env-secret")
    t1 = AuditTrail(db_path, secret=b"explicit-wins")
    t1.record("with.explicit")
    t1.close()

    t2 = AuditTrail(db_path, secret=b"explicit-wins")
    try:
        valid, _ = t2.verify_chain()
        assert valid
    finally:
        t2.close()

    t3 = AuditTrail(db_path)  # falls back to the env secret -> wrong key
    try:
        valid, errors = t3.verify_chain()
        assert not valid
        assert any("Signature mismatch" in e for e in errors)
    finally:
        t3.close()


# ---------------------------------------------------------------------------
# Companion file permissions
# ---------------------------------------------------------------------------


def test_secret_file_created_0600(db_path, tmp_path):
    AuditTrail(db_path).close()
    secret_file = tmp_path / ".audit.db.hmac_secret"
    assert secret_file.exists()
    assert _mode(secret_file) == 0o600


def test_salt_file_created_0600(db_path, tmp_path):
    AuditTrail(db_path).close()
    salt_file = tmp_path / ".audit.db.salt"
    assert salt_file.exists()
    assert _mode(salt_file) == 0o600


def test_anchor_file_created_0600(trail, tmp_path):
    trail.record("anchored")
    anchor_file = tmp_path / ".audit.db.anchor"
    assert anchor_file.exists()
    assert _mode(anchor_file) == 0o600
    payload = json.loads(anchor_file.read_text(encoding="utf-8"))
    assert payload["sequence"] == 1


def test_lax_secret_permissions_reenforced(db_path, tmp_path):
    t1 = AuditTrail(db_path)
    t1.record("before.chmod")
    t1.close()
    secret_file = tmp_path / ".audit.db.hmac_secret"
    os.chmod(secret_file, 0o644)

    t2 = AuditTrail(db_path)
    try:
        assert _mode(secret_file) == 0o600
        valid, _ = t2.verify_chain()
        assert valid  # same secret content, just re-protected
    finally:
        t2.close()


# ---------------------------------------------------------------------------
# Tamper detection
# ---------------------------------------------------------------------------


def test_tamper_data_json_detected(trail, db_path):
    for i in range(5):
        trail.record(f"act.{i}", data={"v": i})
    _raw_sql(db_path, "UPDATE audit_entries SET data_json = ? WHERE sequence = 3", ('{"v": 999}',))
    valid, errors = trail.verify_chain()
    assert not valid
    assert any("Signature mismatch" in e for e in errors)


def test_tamper_prev_hash_chain_break(trail, db_path):
    for i in range(5):
        trail.record(f"act.{i}")
    _raw_sql(db_path, "UPDATE audit_entries SET prev_hash = 'deadbeef' WHERE sequence = 3")
    valid, errors = trail.verify_chain()
    assert not valid
    assert any("Hash chain break at sequence 3" in e for e in errors)


def test_sequence_gap_reported(trail, db_path):
    for i in range(5):
        trail.record(f"act.{i}")
    _raw_sql(db_path, "DELETE FROM audit_entries WHERE sequence = 3")
    valid, errors = trail.verify_chain()
    assert not valid
    assert any("Sequence gap" in e for e in errors)


def test_truncation_delete_last_row(trail, db_path):
    # F14-c regression: trailing deletion used to pass — the walk simply
    # stopped earlier. The out-of-DB anchor catches it.
    for i in range(5):
        trail.record(f"act.{i}")
    _raw_sql(
        db_path,
        "DELETE FROM audit_entries WHERE sequence = (SELECT MAX(sequence) FROM audit_entries)",
    )
    valid, errors = trail.verify_chain()
    assert not valid
    assert any("Truncation detected" in e for e in errors)


def test_truncation_delete_all_rows(trail, db_path):
    for i in range(3):
        trail.record(f"act.{i}")
    _raw_sql(db_path, "DELETE FROM audit_entries")
    valid, errors = trail.verify_chain()
    assert not valid
    assert any("Truncation detected" in e and "empty" in e for e in errors)


def test_anchor_invalid_json_reported(trail, tmp_path):
    trail.record("act")
    (tmp_path / ".audit.db.anchor").write_text("not json at all", encoding="utf-8")
    valid, errors = trail.verify_chain()
    assert not valid
    assert any("anchor" in e.lower() for e in errors)


def test_anchor_absent_is_legacy_ok(trail, tmp_path):
    trail.record("act")
    (tmp_path / ".audit.db.anchor").unlink()
    valid, errors = trail.verify_chain()
    assert valid, errors


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def test_export_csv_healthy(trail):
    for i in range(4):
        trail.record(f"act.{i}", data={"i": i}, user="bob", justification=f"j{i}")
    content = trail.export_csv()
    lines = content.splitlines()
    assert len(lines) == 1 + 4
    assert lines[0] == '"Date";"Action";"User";"Justification";"PrevHash";"Signature";"Sequence"'


def test_export_csv_quote_all_semicolon(trail):
    trail.record("semi;colon", data={"x": 1}, user="u;ser", justification="needs; quoting")
    content = trail.export_csv()
    rows = list(csv.reader(io.StringIO(content), delimiter=";"))
    assert rows[0] == [
        "Date", "Action", "User", "Justification", "PrevHash", "Signature", "Sequence",
    ]
    assert rows[1][1] == "semi;colon"
    assert rows[1][2] == "u;ser"
    assert rows[1][6] == "1"
    # QUOTE_ALL: every field on every line is wrapped in double quotes.
    for line in content.splitlines():
        assert line.startswith('"') and line.endswith('"')


def test_export_csv_writes_file(trail, tmp_path):
    trail.record("act", data={"k": "v"})
    out = tmp_path / "export.csv"
    content = trail.export_csv(output=out)
    # read_bytes: read_text would fold the CSV \r\n line endings into \n.
    assert out.read_bytes().decode("utf-8") == content


def test_export_csv_seq_filters(trail):
    for i in range(5):
        trail.record(f"act.{i}")
    content = trail.export_csv(from_seq=2, to_seq=4)
    rows = list(csv.reader(io.StringIO(content), delimiter=";"))
    assert [r[6] for r in rows[1:]] == ["2", "3", "4"]


def test_export_csv_tampered_raises(trail, db_path):
    for i in range(3):
        trail.record(f"act.{i}", data={"i": i})
    _raw_sql(db_path, "UPDATE audit_entries SET data_json = '{}' WHERE sequence = 2")
    with pytest.raises(AuditIntegrityError) as excinfo:
        trail.export_csv()
    assert any("Signature mismatch" in e for e in excinfo.value.errors)


# ---------------------------------------------------------------------------
# PII pseudonymization
# ---------------------------------------------------------------------------


def test_pseudonymizer_stable_and_distinct(tmp_path):
    p = PiiPseudonymizer(tmp_path / "salt")
    a1 = p.pseudonymize("alice@example.com")
    a2 = p.pseudonymize("alice@example.com")
    b = p.pseudonymize("bob@example.com")
    assert a1 == a2
    assert a1 != b
    assert a1.startswith("pii:")
    assert len(a1) == len("pii:") + 16
    # Stable across instances too (same salt file).
    p2 = PiiPseudonymizer(tmp_path / "salt")
    assert p2.pseudonymize("alice@example.com") == a1


def test_pii_fields_pseudonymized_in_db(trail, db_path):
    entry = trail.record(
        "user.login",
        data={"email": "alice@example.com", "ip": "10.0.0.1"},
        pii_fields={"email"},
    )
    assert entry.data["email"].startswith("pii:")
    assert entry.data["ip"] == "10.0.0.1"

    conn = sqlite3.connect(str(db_path))
    try:
        data_json = conn.execute(
            "SELECT data_json FROM audit_entries WHERE sequence = 1"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "alice@example.com" not in data_json
    assert '"pii:' in data_json
    assert "10.0.0.1" in data_json
    # Pseudonymization happens BEFORE signing: the chain must stay valid.
    valid, errors = trail.verify_chain()
    assert valid, errors


# ---------------------------------------------------------------------------
# Concurrency (the D-008 synchronous write path under contention)
# ---------------------------------------------------------------------------


def test_concurrent_records_8_threads(trail):
    sequences: list[int] = []
    seq_lock = threading.Lock()

    def worker(worker_id: int) -> None:
        for i in range(25):
            entry = trail.record("load.test", data={"worker": worker_id, "i": i})
            with seq_lock:
                sequences.append(entry.sequence)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(sequences) == 200
    assert sorted(sequences) == list(range(1, 201))  # unique AND consecutive
    assert trail.entry_count() == 200
    valid, errors = trail.verify_chain()
    assert valid, errors
