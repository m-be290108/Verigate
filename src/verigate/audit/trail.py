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
import time
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


def _create_private_exclusive(path: Path, payload: bytes) -> bytes:
    """Create `path` with `payload`, atomically AND exclusively (mode 0600).

    mkstemp + os.link gives both properties at once: the payload is fully
    written to a private tmpfile first, so a crash never leaves a torn or
    world-readable file at `path`; and the hard link fails with
    FileExistsError when `path` already exists, so exactly ONE of any
    number of concurrent first-time creators wins (O_CREAT|O_EXCL
    semantics, without the torn-file window of a direct exclusive write).

    Returns the bytes that actually live at `path` afterwards: the
    caller's payload if this call won the race, the winner's bytes if it
    lost. A loser MUST use the returned bytes — keeping its own would
    mean (for the HMAC secret) signing rows with a key that exists
    nowhere on disk, making the chain unverifiable after a restart.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        try:
            os.fchmod(fd, _PRIVATE_MODE)
            os.write(fd, payload)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return _read_private_file(path)
        return payload
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


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
        # Exclusive create: two instances racing first-time creation must
        # converge on ONE persisted salt, or pseudonyms silently diverge
        # per instance. The loser adopts the winner's bytes.
        return _create_private_exclusive(self._salt_path, secrets.token_bytes(32))

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
        # Switching to WAL needs a brief exclusive lock, and SQLite does not
        # route this particular conflict through the busy handler: two
        # instances constructed concurrently on the same fresh db can hit
        # 'database is locked' immediately instead of waiting. The journal
        # mode is persistent in the db header, so once ANY racer succeeds
        # the retry merely confirms 'wal'. Bounded retry, then fail loudly.
        for attempt in range(20):
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc) or attempt == 19:
                    raise
                time.sleep(0.005 * (attempt + 1))
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
        # Exclusive create: when two instances race first-time creation on
        # the same fresh db_path, both must end up signing with the SAME
        # persisted secret. The old exists()->generate->write sequence was
        # last-writer-wins (os.replace): the racer whose write lost kept
        # signing with an in-memory secret that no longer existed on disk,
        # so verify_chain() reported 'Signature mismatch' on its rows after
        # any restart. With the exclusive create, a loser adopts the
        # winner's bytes before signing anything.
        secret = _create_private_exclusive(path, secrets.token_bytes(32))
        logger.info("Audit HMAC secret persisted: %s", path)
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
            # If THIS write fails, the row is already durably committed and
            # the anchor is left lagging one row behind; verify_chain()
            # recognizes that exact state (lagging anchor + intact forward
            # chain) and self-reconciles instead of raising a false
            # truncation alarm.
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

        Lagging-anchor self-reconciliation: record() COMMITs the row BEFORE
        it writes the anchor, so an I/O failure between the two (disk full,
        read-only dir, crash) leaves the anchor pinning sequence N while
        the table legitimately ends at N+k. That state is distinguishable
        from tampering: a delete-the-tail attack leaves the table BEHIND
        the anchor (never ahead of it), and forging rows past the anchor
        requires the HMAC secret. So when the anchor lags strictly behind
        the last row, its (sequence, signature) matches the genuine row it
        was written for, and every row from there to the end verifies
        cleanly, the chain is VALID and the anchor is atomically re-pinned
        to the true last row. Any other divergence (forged anchor
        signature, broken forward chain, anchor ahead of the table) stays
        a hard truncation/tamper error.
        """
        errors: list[str] = []
        bad_seqs: set[int] = set()

        def flag(seq: int, message: str) -> None:
            bad_seqs.add(int(seq))
            errors.append(message)

        # The whole verification runs under the lock so the lagging-anchor
        # re-pin below cannot race a concurrent record(): re-writing the
        # anchor from a stale snapshot would regress a newer anchor.
        with self._lock:
            rows = self._conn.execute(
                "SELECT sequence, timestamp, action, user, justification,"
                " data_json, prev_hash, signature"
                " FROM audit_entries ORDER BY sequence"
            ).fetchall()

            prev_sig = _GENESIS
            expected_seq = 1
            sig_by_seq: dict[int, str] = {}
            for seq, ts, action, user, just, data_json, prev_hash, sig in rows:
                sig_by_seq[int(seq)] = str(sig)
                if seq != expected_seq:
                    flag(seq, f"Sequence gap: expected {expected_seq}, got {seq}")
                if prev_hash != prev_sig:
                    flag(
                        seq,
                        f"Hash chain break at sequence {seq}: "
                        f"expected prev_hash={prev_sig!r}, got {prev_hash!r}",
                    )
                try:
                    data = json.loads(data_json)
                except json.JSONDecodeError as exc:
                    flag(seq, f"Unparseable data_json at sequence {seq}: {exc}")
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
                        flag(seq, f"Signature mismatch at sequence {seq}")
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
                    last_seq, last_sig = int(rows[-1][0]), str(rows[-1][7])
                    a_seq, a_sig = anchor["sequence"], anchor["signature"]
                    if last_seq == a_seq and last_sig == a_sig:
                        pass  # anchor pins the last row exactly — nominal
                    elif self._anchor_lag_is_benign(
                        a_seq, a_sig, last_seq, sig_by_seq, bad_seqs
                    ):
                        # Crashed anchor write, not tampering. Re-pin to
                        # the true last row — but only on an otherwise
                        # clean chain: an audit verification must never
                        # mutate state while it is reporting a failure.
                        # Best-effort: if the re-pin itself fails (the
                        # disk may still be full), the chain is still
                        # valid and the next verification retries.
                        if not errors:
                            try:
                                self._write_anchor(last_seq, last_sig)
                            except OSError:
                                logger.warning(
                                    "Lagging audit anchor is consistent but"
                                    " could not be re-pinned to sequence %d —"
                                    " will retry on the next verification",
                                    last_seq,
                                )
                    else:
                        errors.append(
                            "Truncation detected: expected last entry sequence="
                            f"{a_seq}, found sequence={last_seq}"
                        )

        return len(errors) == 0, errors

    @staticmethod
    def _anchor_lag_is_benign(
        a_seq: int,
        a_sig: str,
        last_seq: int,
        sig_by_seq: dict[int, str],
        bad_seqs: set[int],
    ) -> bool:
        """True iff a non-matching anchor is explained by a crashed anchor
        write rather than tampering.

        Three conditions, all required:
        - the anchor lags STRICTLY behind the table (a truncation attack
          leaves the table behind the anchor, never ahead of it);
        - the anchor's (sequence, signature) matches the genuine row it
          was written for (a forged/garbage anchor fails here);
        - every row from the anchored one to the end verified cleanly —
          no gap, no linkage break, no HMAC mismatch (appending rows
          without the secret fails here).
        """
        if a_seq >= last_seq:
            return False
        if sig_by_seq.get(a_seq) != a_sig:
            return False
        return all(seq not in bad_seqs for seq in range(a_seq, last_seq + 1))

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
