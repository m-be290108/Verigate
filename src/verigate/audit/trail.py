"""Tamper-evident audit trail — the compliance backbone of VeriGate.

Synchronous-only port of the production-proven Beaume ``audit_trail.py``
(decision D-008): the asyncio queue, writer loop, replay registry and
EventBus integration are dropped; every integrity property is kept,
including the 2026-06-10 audit fixes:

- F14-a: the HMAC secret is persisted per installation (0600 file next to
  the DB), so the chain stays verifiable across restarts.
- F14-b: sequence allocation, signing and INSERT happen in ONE
  ``BEGIN IMMEDIATE`` transaction under a lock, and the INSERT is strict —
  a collision raises instead of silently dropping an entry.
- F14-c: an out-of-DB anchor file pins the last (sequence, signature), so
  deleting the trailing rows (or the whole table) is detected.

Timestamps here are the one allowed wall-clock read in VeriGate: D-006
forbids clock time in Reports; the audit trail is append-only and
timestamped by nature.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SECRET_ENV = "VERIGATE_AUDIT_SECRET"
_PRIVATE_MODE = 0o600
_GENESIS = "GENESIS"


class AuditIntegrityError(RuntimeError):
    """The audit chain failed verification — raised instead of exporting a
    broken chain silently. ``errors`` carries the verify_chain() findings."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("audit chain verification failed: " + "; ".join(errors))
        self.errors = list(errors)


def _atomic_write_private(path: Path, payload: bytes) -> None:
    """Write `payload` to `path` atomically with mode 0600.

    mkstemp in the same directory + fchmod before any byte lands on disk +
    os.replace: a crash mid-write leaves either the old file or the new one,
    never a torn or world-readable secret.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        try:
            os.fchmod(fd, _PRIVATE_MODE)
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_private_file(path: Path) -> bytes:
    """Read a secret file, re-enforcing 0600 if it was found lax."""
    mode = path.stat().st_mode & 0o777
    if mode != _PRIVATE_MODE:
        logger.warning(
            "Secret file %s has lax permissions %o — enforcing 0600", path, mode
        )
        os.chmod(path, _PRIVATE_MODE)
    return path.read_bytes()


class PiiPseudonymizer:
    """Deterministic HMAC-SHA256 pseudonymization (ported as-is from Beaume).

    The salt is per-installation, stored 0600: pseudonyms are stable within
    an installation (same salt -> same output) and unlinkable without it.
    """

    def __init__(self, salt_path: str | Path = ".audit_salt") -> None:
        self._salt_path = Path(salt_path)
        self._salt = self._load_or_create_salt()

    def _load_or_create_salt(self) -> bytes:
        if self._salt_path.exists():
            return _read_private_file(self._salt_path)
        salt = secrets.token_bytes(32)
        _atomic_write_private(self._salt_path, salt)
        return salt

    def pseudonymize(self, value: str) -> str:
        """Return a stable ``pii:`` + 16-hex-char pseudonym for `value`."""
        mac = hmac.new(self._salt, value.encode("utf-8"), hashlib.sha256)
        return "pii:" + mac.hexdigest()[:16]

    def pseudonymize_dict(
        self, data: dict[str, Any], pii_fields: set[str]
    ) -> dict[str, Any]:
        """Return a shallow copy of `data` with `pii_fields` pseudonymized."""
        result = dict(data)
        for key in pii_fields:
            if key in result and result[key] is not None:
                result[key] = self.pseudonymize(str(result[key]))
        return result


@dataclass
class AuditEntry:
    """One journaled event, as written (and as re-verified)."""

    sequence: int
    timestamp: str
    action: str
    user: str
    justification: str
    data: dict[str, Any]
    prev_hash: str
    signature: str = field(default="", init=False)

    def compute_signature(self, secret: bytes) -> str:
        """HMAC-SHA256 over the canonical payload. `data` is serialized with
        sort_keys so the signature is independent of dict insertion order."""
        payload = (
            f"{self.sequence}|{self.timestamp}|{self.action}"
            f"|{self.user}|{self.justification}"
            f"|{self.prev_hash}"
            f"|{json.dumps(self.data, sort_keys=True, ensure_ascii=False)}"
        )
        return hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()


class AuditTrail:
    """Tamper-evident audit trail: HMAC hash chain over SQLite (WAL, FULL).

    Single synchronous write path (D-008) — the F14-b sequence-collision bug
    class cannot exist here. Thread-safe via one connection + threading.Lock.
    """

    _CSV_HEADER = [
        "Date",
        "Action",
        "User",
        "Justification",
        "PrevHash",
        "Signature",
        "Sequence",
    ]

    def __init__(
        self,
        db_path: str | Path,
        secret: bytes | None = None,
        salt_path: str | Path | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        # The parent must exist before sqlite3.connect and before the
        # companion files (.hmac_secret/.anchor/.salt) are created —
        # otherwise init fails depending on the launch cwd (F14).
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # F14-a: without an explicit secret or env var, the secret used to be
        # regenerated each process, so verify_chain() reported 'Signature
        # mismatch' on every entry after a simple restart. Priority:
        # explicit arg > env var > persistent per-installation 0600 file.
        env_secret = os.environ.get(_SECRET_ENV, "")
        if secret is not None:
            self._secret: bytes = secret
        elif env_secret:
            self._secret = env_secret.encode("utf-8")
        else:
            self._secret = self._load_or_create_secret()
        if salt_path is None:
            salt_path = self._db_path.parent / f".{self._db_path.name}.salt"
        self._pseudonymizer = PiiPseudonymizer(salt_path)
        self._lock = threading.Lock()
        # check_same_thread=False is safe: every use of the connection is
        # serialized by self._lock. isolation_level=None hands transaction
        # control to the explicit BEGIN IMMEDIATE/COMMIT below.
        self._conn = sqlite3.connect(
            str(self._db_path), isolation_level=None, check_same_thread=False
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS audit_entries (
                sequence      INTEGER PRIMARY KEY,
                timestamp     TEXT    NOT NULL,
                action        TEXT    NOT NULL,
                user          TEXT    NOT NULL,
                justification TEXT    NOT NULL,
                data_json     TEXT    NOT NULL,
                prev_hash     TEXT    NOT NULL,
                signature     TEXT    NOT NULL
            )
            """
        )

    # ------------------------------------------------------------------
    # Companion files: persistent secret (F14-a) + out-of-DB anchor (F14-c)
    # ------------------------------------------------------------------

    def _secret_path(self) -> Path:
        return self._db_path.parent / f".{self._db_path.name}.hmac_secret"

    def _anchor_path(self) -> Path:
        return self._db_path.parent / f".{self._db_path.name}.anchor"

    def _load_or_create_secret(self) -> bytes:
        path = self._secret_path()
        if path.exists():
            return _read_private_file(path)
        secret = secrets.token_bytes(32)
        _atomic_write_private(path, secret)
        logger.info("Audit HMAC secret generated and persisted: %s", path)
        return secret

    def _write_anchor(self, sequence: int, signature: str) -> None:
        # F14-c: without this external anchor, a DELETE of the N last rows
        # (or a full table wipe) passed verify_chain() — the walk simply
        # stopped earlier. Updated on every write, under the same lock.
        payload = json.dumps({"sequence": sequence, "signature": signature})
        _atomic_write_private(self._anchor_path(), payload.encode("utf-8"))

    def _read_anchor(self) -> dict[str, Any] | None:
        """None if the anchor does not exist; ValueError if unreadable."""
        path = self._anchor_path()
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # ValueError covers json.JSONDecodeError and UnicodeDecodeError.
            raise ValueError(f"Audit anchor unreadable ({path}): {exc}") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("sequence"), int)
            or not isinstance(payload.get("signature"), str)
        ):
            raise ValueError(f"Audit anchor invalid ({path})")
        return payload

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def record(
        self,
        action: str,
        data: dict[str, Any] | None = None,
        user: str = "system",
        justification: str = "",
        pii_fields: set[str] | None = None,
    ) -> AuditEntry:
        """Journal one event synchronously and return the completed entry.

        F14-b: SELECT of the last (sequence, signature) and the INSERT run in
        the same BEGIN IMMEDIATE transaction, under self._lock. The INSERT is
        strict — an sqlite3.IntegrityError propagates instead of being eaten
        by OR IGNORE (silent loss is the absolute anti-pattern for an audit
        trail with legal value).
        """
        sanitized = dict(data or {})
        if pii_fields:
            sanitized = self._pseudonymizer.pseudonymize_dict(sanitized, pii_fields)
        # Wall-clock allowed HERE only: D-006 applies to Reports, the trail
        # is append-only and timestamped by nature.
        timestamp = datetime.now(timezone.utc).isoformat()
        entry = AuditEntry(
            sequence=0,
            timestamp=timestamp,
            action=action,
            user=user,
            justification=justification,
            data=sanitized,
            prev_hash="",
        )
        data_json = json.dumps(sanitized, sort_keys=True, ensure_ascii=False)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT sequence, signature FROM audit_entries"
                    " ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                if row is not None:
                    entry.sequence = int(row[0]) + 1
                    entry.prev_hash = str(row[1])
                else:
                    entry.sequence = 1
                    entry.prev_hash = _GENESIS
                entry.signature = entry.compute_signature(self._secret)
                self._conn.execute(
                    "INSERT INTO audit_entries"
                    " (sequence, timestamp, action, user, justification,"
                    "  data_json, prev_hash, signature)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry.sequence,
                        entry.timestamp,
                        entry.action,
                        entry.user,
                        entry.justification,
                        data_json,
                        entry.prev_hash,
                        entry.signature,
                    ),
                )
                self._conn.execute("COMMIT")
            finally:
                # Reached with an open transaction only when something above
                # raised (the exception keeps propagating past this cleanup).
                if self._conn.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        self._conn.execute("ROLLBACK")
            # Anchor updated after commit, still under the lock (F14-c).
            self._write_anchor(entry.sequence, entry.signature)
        return entry

    # ------------------------------------------------------------------
    # Chain integrity verification
    # ------------------------------------------------------------------

    def verify_chain(self) -> tuple[bool, list[str]]:
        """Walk the whole chain; return (valid, errors).

        Checks, in order: prev_hash linkage (first row must point at
        GENESIS), sequence gaps, the HMAC of every row, then the out-of-DB
        anchor (F14-c). An absent anchor is not an error — legacy/new DB.
        """
        errors: list[str] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT sequence, timestamp, action, user, justification,"
                " data_json, prev_hash, signature"
                " FROM audit_entries ORDER BY sequence"
            ).fetchall()

        prev_sig = _GENESIS
        expected_seq = 1
        for seq, ts, action, user, just, data_json, prev_hash, sig in rows:
            if seq != expected_seq:
                errors.append(f"Sequence gap: expected {expected_seq}, got {seq}")
            if prev_hash != prev_sig:
                errors.append(
                    f"Hash chain break at sequence {seq}: "
                    f"expected prev_hash={prev_sig!r}, got {prev_hash!r}"
                )
            try:
                data = json.loads(data_json)
            except json.JSONDecodeError as exc:
                errors.append(f"Unparseable data_json at sequence {seq}: {exc}")
            else:
                entry = AuditEntry(
                    sequence=seq,
                    timestamp=ts,
                    action=action,
                    user=user,
                    justification=just,
                    data=data,
                    prev_hash=prev_hash,
                )
                if sig != entry.compute_signature(self._secret):
                    errors.append(f"Signature mismatch at sequence {seq}")
            prev_sig = sig
            expected_seq = seq + 1

        try:
            anchor = self._read_anchor()
        except ValueError as exc:
            errors.append(str(exc))
            anchor = None
        if anchor is not None:
            if not rows:
                errors.append(
                    "Truncation detected: anchor at sequence="
                    f"{anchor['sequence']} but audit table is empty"
                )
            else:
                last_seq, last_sig = rows[-1][0], rows[-1][7]
                if int(last_seq) != anchor["sequence"] or str(last_sig) != anchor["signature"]:
                    errors.append(
                        "Truncation detected: expected last entry sequence="
                        f"{anchor['sequence']}, found sequence={last_seq}"
                    )

        return len(errors) == 0, errors

    # ------------------------------------------------------------------
    # CSV export
    # ------------------------------------------------------------------

    def export_csv(
        self,
        output: str | Path | None = None,
        from_seq: int | None = None,
        to_seq: int | None = None,
    ) -> str:
        """Export the trail as semicolon-separated, fully-quoted CSV.

        verify_chain() runs FIRST and a failure raises AuditIntegrityError —
        a broken chain is never exported silently, whatever the seq filters.
        """
        valid, errors = self.verify_chain()
        if not valid:
            raise AuditIntegrityError(errors)

        query = (
            "SELECT sequence, timestamp, action, user, justification,"
            " data_json, prev_hash, signature FROM audit_entries"
        )
        conditions: list[str] = []
        params: list[int] = []
        if from_seq is not None:
            conditions.append("sequence >= ?")
            params.append(from_seq)
        if to_seq is not None:
            conditions.append("sequence <= ?")
            params.append(to_seq)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY sequence"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";", quoting=csv.QUOTE_ALL)
        writer.writerow(self._CSV_HEADER)
        for seq, ts, action, user, just, _data_json, prev_hash, sig in rows:
            writer.writerow([ts, action, user, just, prev_hash, sig, str(seq)])
        content = buf.getvalue()

        if output is not None:
            # newline="": keep the CSV \r\n line endings byte-identical on
            # every platform (no os.linesep translation).
            Path(output).write_text(content, encoding="utf-8", newline="")
        return content

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def entry_count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM audit_entries").fetchone()
        return int(row[0])

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> AuditTrail:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
