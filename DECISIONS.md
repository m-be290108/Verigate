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

## D-010 — CLI exit codes mirror the verdict gradation
`verigate verify` exits 0 VERIFIED / 1 CORRECTED / 2 INSUFFICIENT /
3 UNVERIFIABLE, so shell pipelines and CI gates can branch on the verdict
without parsing output. Exit 4 = operational failure (e.g. stdout cannot
encode the report) — an I/O failure must never alias a verdict.
`ingest`/`verify-corpus`/`audit-export` use 0/1. `serve` refuses
non-loopback hosts (on-premise contract: localhost by default, rebinding is
a deliberate customer decision, not a flag typo).

## D-011 — Re-ingest prunes documents removed from the source folder
The customer folder IS the corpus: a document deleted from the folder is
deleted from corpus.db at the next ingest (FTS stays consistent via the
D-007 triggers) and reported in `IngestResult.pruned` — explicit, never
silent. Rejected: silent retention (stale docs kept verifying answers and
the fingerprint was not reproducible from the folder alone).

## D-012 — Ambiguous straight quotes are not extracted
Straight `"` is also the inch/second/ditto mark. Openers/closers must sit
on word boundaries, and if the count of plausible straight-quote delimiters
in an answer is odd, NO straight-quote atom is extracted from it
(typographic `“ ”` and `« »` are unambiguous and unaffected). Deleting
innocent prose on a mispair is strictly worse than leaving one quote
unchecked (2026-06-10 review finding, HIGH).

## D-013 — Membership, not association (and the bench says so)
An atom is VERIFIED when its canonical form exists anywhere in the corpus;
VeriGate does not check that the value belongs to the sentence's subject.
Cross-attribution lies (real price, wrong product) pass. This is stated in
the README limitations and the bench caveat (whose injected lies are novel
by construction and thus cannot measure this failure mode). Rejected:
claiming "catches wrong prices" unqualified — that was an overclaim
(2026-06-10 review finding, HIGH).

## D-014 — Opt-in LRU report cache, audit still per-event
`Gate(cache_size=N)` / `create_app(cache_size=N)` / `serve --cache-size N`
memoize reports keyed on (answer, context tuple) — sound because for a
fixed corpus the report is a pure function of that key (D-006), proven
byte-identical by test. Cache hits are deep-copied out (a caller mutating a
report must not poison the cache) and STILL journal an audit entry: the
trail records verification events, not engine computations. Default 0
(disabled): measured engine latency is ~0.2–2.5 ms/call, so the cache is a
high-QPS/FAQ optimization, not a default need. The API's post-ingest Gate
swap restarts the cache (old reports were rendered against the old
fingerprint).

## D-015 — Abbreviating a glossary entry is not a lie
Real-data eval (BDPM, 2026-06-11): 4 of the 5 mode-A false positives were
the model writing the full commercial name ('FENOFIBRATE TEVA SANTE
200 mg') while the glossary held the long official form ('…, gélule') —
the candidate extractor truncated the span before the lowercase dose
suffix, and the engine then MISMATCHED the resulting near-miss, mutilating
7.1% of grounded answers. Two changes: (1) a candidate span ending in a
digit extends over a following lowercase dose-unit token (mg, g, ml, µg,
ui, …); (2) a candidate whose canonical tokens — split at digit↔letter
boundaries, so '25mg' equals '25 mg' — form a *contiguous run* inside a
glossary entry is VERIFIED (detail names the entry): the user abbreviates
a real name. A changed token ('… 300 mg' against a 200 mg entry) breaks
the run and still lands in the MISMATCHED ratio path. Trade-off accepted:
an abbreviation shared by several entries verifies against the first in
sorted order — membership, not association (D-013), unchanged. Rejected:
keeping MISMATCHED for abbreviations (deletes correct product names).
