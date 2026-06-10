"""Tests for the local FastAPI surface (verigate.api.app).

Everything runs in-process through ``fastapi.testclient.TestClient`` (ASGI
transport, no sockets); the module-level autouse fixture applies the
``no_network`` guard to EVERY test here, proving the whole API stack never
attempts an outbound connection. Real files in tmp_path, real sqlite,
deterministic — no LLM.

Answers are crafted against the ``sample_corpus_dir`` fixture (catalog.md +
policy.txt + products.csv): AquaPump 3000 / SKU AP-3000-X / €249.99 exist
in the corpus; ZZ-9999-Q and GR-77-X9 do not.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import verigate
from verigate.api.app import create_app
from verigate.corpus import CorpusDB
from verigate.sdk import Gate

VERIFIED_ANSWER = "The AquaPump 3000 (SKU AP-3000-X) costs €249.99."
FAKE_SKU_ANSWER = "The AquaPump 3000 uses part ZZ-9999-Q."
CTX_ANSWER = "Use spare part GR-77-X9 for the repair."
CTX_CHUNK = "Approved spare parts list: GR-77-X9 is certified."

CSV_HEADER = '"Date";"Action";"User";"Justification";"PrevHash";"Signature";"Sequence"'

HEALTH_KEYS = {"status", "version", "corpus_fingerprint", "documents"}
INGEST_KEYS = {"n_docs", "n_refs", "n_numbers", "n_entities", "fingerprint", "skipped"}


def _canon(data: dict) -> str:
    """Deterministic JSON serialization for byte-equality comparisons."""
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _audit_rows(audit_path):
    conn = sqlite3.connect(audit_path)
    try:
        return conn.execute(
            "SELECT action, data_json FROM audit_entries ORDER BY sequence"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _offline(no_network, monkeypatch):
    """Sockets blocked for every test in this module (in-process ASGI only)
    + the persistent-secret-file audit path forced (F14-a), host env aside."""
    monkeypatch.delenv("VERIGATE_AUDIT_SECRET", raising=False)


@pytest.fixture
def corpus_db(sample_corpus_dir, tmp_path):
    """The sample corpus folder ingested into tmp_path/corpus.db."""
    db_path = tmp_path / "corpus.db"
    Gate.ingest(sample_corpus_dir, db_path)
    return db_path


@pytest.fixture
def client(corpus_db):
    """TestClient over an app WITHOUT audit trail (lifespan exercised)."""
    with TestClient(create_app(corpus_db)) as c:
        yield c


@pytest.fixture
def audit_setup(corpus_db, tmp_path):
    """(TestClient, audit_db_path) over an app WITH an audit trail."""
    audit_db = tmp_path / "audit.db"
    with TestClient(create_app(corpus_db, audit_db=audit_db)) as c:
        yield c, audit_db


def _new_docs_folder(tmp_path):
    """A server-local folder with one NEW document (entity + SKU + price
    absent from the sample corpus)."""
    folder = tmp_path / "new_docs"
    folder.mkdir()
    (folder / "valves.md").write_text(
        "# Valves\n\n"
        "## TurboValve X (SKU TV-900-Z)\n"
        "High-pressure valve. Price: €123.45.\n",
        encoding="utf-8",
    )
    return folder


# ------------------------------------------------------------- GET /health


def test_health_shape(client, corpus_db):
    resp = client.get("/health")
    body = resp.json()
    assert resp.status_code == 200
    assert set(body) == HEALTH_KEYS
    assert body["status"] == "ok"
    assert body["version"] == verigate.__version__
    assert body["documents"] == 3
    with CorpusDB(corpus_db) as db:
        assert body["corpus_fingerprint"] == db.fingerprint()


# ------------------------------------------------------------ POST /verify


def test_verify_clean_answer_verified(client):
    resp = client.post("/verify", json={"answer": VERIFIED_ANSWER})
    body = resp.json()
    assert resp.status_code == 200
    assert body["verdict"] == "VERIFIED"
    assert body["score"] == 1.0
    assert body["corrected_answer"] == VERIFIED_ANSWER
    assert body["counts"]["false"] == 0
    assert body["counts"]["verified"] >= 1


def test_verify_fake_sku_false_atom_and_marker(client):
    resp = client.post("/verify", json={"answer": FAKE_SKU_ANSWER})
    body = resp.json()
    assert resp.status_code == 200
    assert body["verdict"] == "CORRECTED"
    false_atoms = [a for a in body["atoms"] if a["status"] == "not_found"]
    assert len(false_atoms) == 1
    assert false_atoms[0]["atom"]["type"] == "reference"
    assert false_atoms[0]["atom"]["raw"] == "ZZ-9999-Q"
    assert "⟨unverified reference, removed⟩" in body["corrected_answer"]
    assert "ZZ-9999-Q" not in body["corrected_answer"]


def test_verify_context_grounding(client):
    without = client.post("/verify", json={"answer": CTX_ANSWER}).json()
    with_ctx = client.post(
        "/verify", json={"answer": CTX_ANSWER, "context": [CTX_CHUNK]}
    ).json()
    assert without["verdict"] == "INSUFFICIENT"
    assert with_ctx["verdict"] == "VERIFIED"
    assert with_ctx["atoms"][0]["matched_source"] == "context"


def test_verify_missing_answer_422(client):
    resp = client.post("/verify", json={})
    assert resp.status_code == 422


def test_verify_context_wrong_type_422(client):
    resp = client.post("/verify", json={"answer": "x", "context": "not-a-list"})
    assert resp.status_code == 422


def test_verify_empty_answer_unverifiable(client):
    resp = client.post("/verify", json={"answer": ""})
    body = resp.json()
    assert resp.status_code == 200
    assert body["verdict"] == "UNVERIFIABLE"
    assert body["score"] == 0.0
    assert body["corrected_answer"] == ""
    assert body["warnings"]  # explicit, never a vacuous silence


def test_verify_explicit_null_context_ok(client):
    resp = client.post("/verify", json={"answer": VERIFIED_ANSWER, "context": None})
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "VERIFIED"


def test_verify_report_parity_with_direct_gate(client, corpus_db):
    """The API report equals Report.to_dict() from a direct Gate on the
    same corpus — byte-equal after a JSON round-trip."""
    api_body = client.post("/verify", json={"answer": FAKE_SKU_ANSWER}).json()
    with Gate(corpus_db) as gate:
        direct = gate.verify(FAKE_SKU_ANSWER).to_dict()
    assert _canon(api_body) == _canon(json.loads(json.dumps(direct)))


# ------------------------------------------------------------ POST /ingest


def test_ingest_then_verify_sees_new_docs(client, tmp_path):
    folder = _new_docs_folder(tmp_path)
    resp = client.post("/ingest", json={"folder": str(folder)})
    body = resp.json()
    assert resp.status_code == 200
    assert body["n_docs"] == 4  # 3 sample docs + valves.md, same database
    assert body["skipped"] == []

    # The app's Gate must see the NEW corpus content: entity (glossary
    # snapshot), reference and price all come from the just-ingested doc.
    report = client.post(
        "/verify", json={"answer": "The TurboValve X (SKU TV-900-Z) costs €123.45."}
    ).json()
    assert report["verdict"] == "VERIFIED"
    assert report["corpus_fingerprint"] == body["fingerprint"]


def test_ingest_response_shape_and_skipped_reported(client, tmp_path):
    folder = _new_docs_folder(tmp_path)
    (folder / "junk.xyz").write_text("not ingestible", encoding="utf-8")
    resp = client.post("/ingest", json={"folder": str(folder)})
    body = resp.json()
    assert resp.status_code == 200
    assert set(body) == INGEST_KEYS
    assert body["fingerprint"]
    # tuples become lists over JSON — explicit skip report, never silent.
    assert body["skipped"] == [["junk.xyz", "unsupported extension .xyz"]]


def test_ingest_bad_folder_400(client, tmp_path):
    resp = client.post("/ingest", json={"folder": str(tmp_path / "no_such_dir")})
    assert resp.status_code == 400
    assert "not an existing folder" in resp.json()["detail"]


def test_ingest_bad_glossary_400(client, tmp_path):
    folder = _new_docs_folder(tmp_path)
    bad = tmp_path / "bad_glossary.yaml"
    bad.write_text("entities: 42\n", encoding="utf-8")
    resp = client.post("/ingest", json={"folder": str(folder), "glossary": str(bad)})
    assert resp.status_code == 400
    assert "glossary" in resp.json()["detail"]


def test_ingest_missing_folder_field_422(client):
    resp = client.post("/ingest", json={})
    assert resp.status_code == 422


def test_ingest_updates_health(client, tmp_path):
    before = client.get("/health").json()
    client.post("/ingest", json={"folder": str(_new_docs_folder(tmp_path))})
    after = client.get("/health").json()
    assert after["documents"] == before["documents"] + 1
    assert after["corpus_fingerprint"] != before["corpus_fingerprint"]


# -------------------------------------------------------- GET /audit/export


def test_audit_export_404_when_unconfigured(client):
    resp = client.get("/audit/export")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "audit trail not configured"


def test_verify_writes_audit_entries(audit_setup):
    client, audit_db = audit_setup
    client.post("/verify", json={"answer": VERIFIED_ANSWER})
    client.post("/verify", json={"answer": FAKE_SKU_ANSWER})
    rows = _audit_rows(audit_db)
    assert [action for action, _ in rows] == ["verify", "verify"]
    # Privacy by design: the raw answer never lands in the audit db.
    blobs = "\n".join(data_json for _, data_json in rows)
    assert VERIFIED_ANSWER not in blobs


def test_audit_export_returns_csv(audit_setup):
    client, _ = audit_setup
    client.post("/verify", json={"answer": VERIFIED_ANSWER})
    resp = client.get("/audit/export")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    lines = resp.text.splitlines()
    assert lines[0] == CSV_HEADER
    assert len(lines) == 2  # header + the one journaled verify
    assert '"verify"' in lines[1]


def test_audit_export_empty_trail_ok(audit_setup):
    client, _ = audit_setup
    resp = client.get("/audit/export")
    assert resp.status_code == 200
    assert resp.text.splitlines() == [CSV_HEADER]


def test_audit_export_tampered_db_409_lists_errors(audit_setup):
    client, audit_db = audit_setup
    client.post("/verify", json={"answer": VERIFIED_ANSWER})
    client.post("/verify", json={"answer": FAKE_SKU_ANSWER})
    conn = sqlite3.connect(audit_db)
    conn.execute("UPDATE audit_entries SET user = 'intruder' WHERE sequence = 1")
    conn.commit()
    conn.close()
    resp = client.get("/audit/export")
    assert resp.status_code == 409
    errors = resp.json()["detail"]
    assert isinstance(errors, list) and errors
    assert any("Signature mismatch" in e for e in errors)
    assert "Date" not in resp.text  # a broken chain is never exported


def test_audit_chain_survives_ingest_reopen(audit_setup, tmp_path):
    """The /ingest Gate-reopen reuses the persistent secret (F14-a): the
    chain written before and after the swap still verifies and exports."""
    client, _ = audit_setup
    client.post("/verify", json={"answer": VERIFIED_ANSWER})
    resp = client.post("/ingest", json={"folder": str(_new_docs_folder(tmp_path))})
    assert resp.status_code == 200
    client.post("/verify", json={"answer": VERIFIED_ANSWER})
    export = client.get("/audit/export")
    assert export.status_code == 200
    assert len(export.text.splitlines()) == 3  # header + 2 verifies


# ----------------------------------------------------------- app lifecycle


def test_create_app_missing_corpus_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFoundError):
        create_app(tmp_path / "missing.db")
