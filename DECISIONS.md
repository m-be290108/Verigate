# DECISIONS

Product/design decisions made while building the MVP. Each entry: the choice,
why, and what was rejected. (Décisions consignées au fil de l'eau — demande
explicite du brief.)

## D-001 — Offset-based removal instead of regex re-substitution
The Beaume verifier removed false references by re-running `re.sub` on the
reference text (audit finding F3: only bracketed formats were removed, prose
forms survived). VeriGate records the exact `(start, end)` span of every atom
at extraction time and rewrites the answer by splicing spans (descending
order). Coverage of *all* formats is structural, not pattern-by-pattern.
Rejected: Beaume-style re.sub (already failed an audit once).

## D-002 — Verdict names (English, demo-friendly)
`VERIFIED / CORRECTED / INSUFFICIENT / UNVERIFIABLE` — direct translations of
the proven Beaume gradation (VALIDÉ / CORRIGÉ / INSUFFISANT / NON VÉRIFIABLE).
Thresholds identical: all checkable atoms ok → VERIFIED; some false and
score ≥ 0.5 → CORRECTED; score < 0.5 → INSUFFICIENT; zero checkable atoms →
score 0.0 + UNVERIFIABLE (never a vacuous 100%).

## D-003 — Unverifiable atoms are excluded from the score denominator
Score = verified / (verified + false). Atoms with nothing to check against
(bare small integers, quotes under 3 words, entities absent from the
glossary) are reported as `unverifiable` but do not dilute the score in
either direction. Rationale: a meaningful, defensible score — counting
unverifiable atoms as "ok" would bless prose, counting them as "false" would
make the detector cry wolf (bench measures both directions).

## D-004 — Bare small integers are not verified
A bare "10" matches everywhere in any corpus (false-positive machine).
Only *anchored* numbers are checkable: money amounts, percentages, numbers
with units, dates, and decimals. Bare integers < 1000 without unit context →
`unverifiable`. Verified against a number index built at ingest time.

## D-005 — False atoms are removed + marked, never silently "corrected"
Marker: `⟨unverified <kind>, removed⟩` (e.g. reference, amount, quote,
entity). VeriGate never substitutes the "right" value from the corpus into
the answer — that would be the tool asserting content, which is out of scope
and dangerous. Visible removal only (Beaume truth rule).

## D-006 — Report contains no wall-clock time
Byte-identical reproducibility (same corpus + same answer → same report
bytes) is a contractual property, proven by test. Timestamps live only in
the audit trail, which is by nature append-only and time-stamped.

## D-007 — FTS5 external-content with the documented 'delete' triggers
Beaume finding-11: INSERT OR REPLACE + naive triggers corrupted 32% of the
production FTS index (ghost rowids). VeriGate uses
`INSERT INTO ..._fts(..._fts, rowid, ...) VALUES('delete', old.rowid, ...)`
triggers and `INSERT ... ON CONFLICT DO UPDATE` upserts exclusively. A test
reproduces the ghost-entry scenario and asserts the extended
`('integrity-check', 1)` form passes after re-upserts.

## D-008 — Audit trail is synchronous-only
Beaume's async queue writer (asyncio.Queue + writer loop) exists to keep a
chatty UI responsive; an HTTP verification API has no such constraint, and
the F14-b sequence-collision bug class disappears entirely with a single
synchronous write path (BEGIN IMMEDIATE + lock). Kept: persistent 0600
secret, out-of-DB anchor file (truncation detection), verify_chain on every
export.

## D-009 — Stdlib-first dependency policy
Core = stdlib + PyYAML (packs). FastAPI/uvicorn and pypdf/python-docx are
optional extras (`[api]`, `[ingest]`). A client can run the verification
engine with nothing but Python 3.11 + PyYAML — an on-premise selling point.
