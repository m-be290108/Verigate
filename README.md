# VeriGate

<!-- GitHub repo: m-be290108/verigate -->
[![CI](https://github.com/m-be290108/verigate/actions/workflows/ci.yml/badge.svg)](https://github.com/m-be290108/verigate/actions/workflows/ci.yml)
[![License: FSL-1.1-ALv2](https://img.shields.io/badge/license-FSL--1.1--ALv2-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

**Deterministic verification layer for generative AI answers.**
No LLM. No network. On-premise only. Audit-ready.

> Your AI answers from YOUR data? VeriGate checks every answer against that
> data: every reference, figure, quote and known entity is confronted with
> your trusted corpus. What is false is removed and visibly marked. What
> cannot be verified is flagged. Never a blind blessing. Tamper-evident
> audit log included.

Built for teams whose chatbot or RAG is already in production — legaltech,
e-commerce, pharma, finance, support — and who are being called out on
invented references, wrong prices, fabricated clauses.

## How it works

```
 your documents                    your AI's answer
 (pdf docx md txt csv json)               │
        │                                 ▼
        ▼                        ┌─────────────────┐
  verigate ingest ──▶ corpus.db  │  extract atoms   │  references, figures,
  (FTS5 + ID registry            │  (pure regex,    │  quotes, glossary
   + glossary + SHA-256          │   deterministic) │  entities
   provenance manifest)          └────────┬────────┘
                                          ▼
                                 ┌─────────────────┐
                  corpus.db ───▶ │  verify each     │ ──▶ Report (JSON)
                                 │  atom            │      verdict + score
                                 └────────┬────────┘      + per-atom status
                                          ▼
                              corrected answer: false atoms
                              removed, visibly marked
                              ⟨unverified reference, removed⟩
                                          │
                                          ▼
                              tamper-evident audit trail
                              (HMAC chain, verified at export)
```

Verdicts are graduated, never vacuous: **VERIFIED** (every checkable atom
grounded), **CORRECTED** (false atoms removed, marked), **INSUFFICIENT**
(mostly ungrounded), **UNVERIFIABLE** (nothing checkable — score 0.0, never
a free 100%).

## Quickstart

```bash
pip install -e ".[api,ingest]"

verigate ingest ./your-data --db corpus.db     # builds the trusted corpus
verigate verify --db corpus.db "Our pump AP-3000-X costs €249.99."
verigate serve  --db corpus.db                 # localhost API
```

Or three lines of Python around any LLM call:

```python
from verigate import Gate
gate = Gate("corpus.db")
report = gate.verify(answer)        # report.corrected_answer, report.verdict
```

See it catch hallucinations right now (no setup beyond the repo):

```bash
PYTHONPATH=src python examples/proof.py
```

Connecting it to your own LLM stack (Python library or HTTP sidecar, any
language) → [docs/INTEGRATION.md](docs/INTEGRATION.md).

## What it checks

| Atom | Examples | Verified against |
|---|---|---|
| References / IDs | SKU, legal refs, DOI/ISO/RFC, internal URLs, `[REF: …]` | ID registry built at ingest (+ custom YAML packs) |
| Anchored figures | `€249.99`, `25 %`, `230 V`, `24 months`, dates | number index built at ingest |
| Quotes | text between `"…"`, `“…”`, `« … »` (≥ 3 words) | normalized full-text containment |
| Glossary entities | product names, people — incl. near-misses (`AquaJet 2500` vs known `AquaJet 2200`) | glossary built at ingest |

Customers add their own reference formats as YAML regex packs — one file,
no code.

## What VeriGate is — and is not (read this before buying)

VeriGate does **not** "detect hallucinations" in any magical sense. It
verifies the **verifiable atoms** of an answer against a trusted corpus, and
it checks **groundedness, not truth**: a claim that is true in the world but
absent from your corpus is flagged, because your corpus is the contract.
Free-form prose containing no checkable atom is marked **unverifiable** —
never validated.

Known limitations, by design (each one is a deliberate false-positive
trade-off, documented in `DECISIONS.md`):

- **Membership, not association** (default mode): an atom is VERIFIED when it
  appears *anywhere* in your corpus. A real value attributed to the wrong
  subject (product A sold at product B's genuine price, a real article number
  cited for the wrong rule) is **not** caught by default. *Scoped mode fixes
  this* — see "The closed-domain guarantee" below; turn it on for bounded
  domains. Without it, VeriGate proves "this value exists in your data", not
  "this value belongs to this sentence's subject".
- Bare small integers ("the window is 25") are not checked — only anchored
  figures (money, %, units, dates) are.
- Quotes under 3 words and single-quoted text are not checked.
- An invented entity name *far* from anything in the glossary is flagged
  `unverifiable`, not `false` (near-misses are caught; pure inventions of
  unknown shape have nothing to be checked against).
- **Abbreviating is not lying**: an entity name whose tokens form a
  contiguous run inside a glossary entry ("FENOFIBRATE TEVA SANTE 200 mg"
  for the official "…, gélule" form) is VERIFIED, not flagged (D-015).
  The flip side: an abbreviation shared by several entries verifies
  against *some* real entry — which one the sentence meant is not checked
  (same membership-not-association trade-off as above). A changed token
  ("… 300 mg" when only 200 mg exists) is still caught.
- Paraphrased quotes are not matched — verbatim (normalized) only.
- Identifiers in prose are redacted only when they match a known pack
  format: if a flagged `[REF: X]` has a bare twin `X` of a shape no pack
  recognizes, the twin survives as unverifiable prose.
- Coverage is exactly the formats above plus your packs. A lie outside the
  covered formats passes through as unverifiable prose.

That is precisely what makes the product defensible: a score that means
something, reproducible **byte-for-byte** (same corpus + same answer →
identical report, proven by test), that you can show a regulator or a
customer. Any marketing claim beyond this is a lie, and this repository
refuses to make it.

## The closed-domain guarantee (scoped + strict mode)

For a bounded domain — your catalog, your policies, a body of law — VeriGate
offers a stronger, opt-in posture:

> No verifiable fact that isn't grounded in your corpus **for the subject of
> the answer** reaches the user — it is removed, or the answer abstains.

Turn it on with `VerifyConfig(scoped=True, strict=True)`:

- **scoped** verifies each fact against the section of the answer's *subject*,
  not the whole corpus. So a real value attributed to the wrong subject
  (product A quoted at product B's genuine price) is caught — the
  cross-attribution that plain membership misses (D-018). On real data (the
  French BDPM medicines database) this caught **40/40** cross-attribution
  cases that membership-only passed, at a **1.28%** false-positive cost.
- **strict** shows only grounded facts: anything unverifiable is removed too,
  and `report.is_grounded` tells your code when to abstain
  (`if not report.is_grounded: show "I don't have that information"`).

How does it know it's the *right* datum, not a similar value elsewhere? By
deterministic narrowing — never guessing: **type** (€999 is not 999 L/min) →
**subject** (this product's section) → optionally **field**. Each validated
fact resolves to a precise, reproducible address. The full contract, and its
honest limits, are in **[docs/GUARANTEE.md](docs/GUARANTEE.md)** — the document
your security and compliance teams should read. Integration: see
[docs/INTEGRATION.md](docs/INTEGRATION.md).

## Benchmark — what it proves, and what it can't

**Read this scope first, before the numbers** (it is the honest part, and the
first thing a skeptic should ask): these figures are on a **synthetic** corpus
with **one labeled, anchored-atom lie injected per corrupted answer**, across
**7 covered lie types** (mutated SKU, wrong price, distorted quote, entity
variant, mutated EAN, wrong URL path, wrong warranty duration). Every injected
lie is **novel by construction** — its value appears nowhere in the corpus —
so the detection figure measures catching values that are *absent* from the
corpus. It does **not** measure cross-attribution errors (a real corpus value
attributed to the wrong subject — see D-013), and it is not a claim that "no
hallucination can pass." A detector that misses lies is worthless; so is one
that cries wolf — so both directions are always reported:

| Metric | Result (full run: 100 products, 150 clean + 250 corrupted answers) | Gate |
|---|---|---|
| Detection rate (injected lies flagged) | **100.0 %** | ≥ 95 % |
| False-positive rate (grounded atoms wrongly flagged) | **0.00 %** | ≤ 2 % |

Reproduce: `make bench-quick` (CI-gated) or
`PYTHONPATH=src python -m bench.run` (full). Same seed → byte-identical output.

**On real data** (a held-out test on the official French BDPM medicines
database — 300 medicines, answers from a local LLM), the false-positive rate
on grounded answers was higher and corpus-format-dependent, and the real
failure mode is cross-attribution on dense catalogs (D-013). The honest
takeaway: VeriGate is strongest where the atom is a **unique identifier**
(legal citation, DOI, SKU, code), where absence-from-corpus *is* the lie.

## Latency

Verification is pure regex + indexed SQLite lookups — it adds nothing a
user can feel next to LLM generation time (measured on an M-series Mac):

| Scenario | Latency |
|---|---|
| `verify()`, 3-document corpus | ~0.2 ms / call |
| `verify()`, 400-product catalog (quotes included) | ~2.5 ms / call |
| Repeat answer with the opt-in LRU cache (`Gate(..., cache_size=1024)`) | ~0.02 ms / call |
| Ingest (one-off per corpus update), 400 products | ~1 s |

The cache is safe *because* verification is deterministic: for a fixed
corpus, (answer, context) fully determines the report, byte for byte
(D-014). Cache hits still write their audit entry — the journal records
events, not computations.

## Compliance

- **Deterministic**: no LLM judge, no temperature, no drift. Same inputs,
  same bytes. 408 tests, zero network (enforced by a socket-guard test).
- **On-premise**: your data never leaves. The API binds localhost; there is
  no telemetry, no cloud, no outbound call anywhere in the codebase.
- **Tamper-evident audit trail**: every verification is journaled (answer
  hash — never the raw text —, verdict, score, rejected atoms) in an
  HMAC-SHA256 hash chain with an out-of-database anchor; the chain is
  re-verified at every export. Designed for AI-Act-style accountability
  conversations. *Threat model, stated plainly*: tamper-evidence holds
  against modification of the audit database alone. By default the HMAC
  secret lives in a 0600 file next to the database, so an attacker with
  full local filesystem access could read it and forge a passing chain —
  for that threat model, supply the secret out-of-band
  (`VERIGATE_AUDIT_SECRET` or an explicit `secret=`).

## The honest sales pitch

**What it catches**: invented references/SKUs/articles in any format
(bracketed, parenthesized, prose), figures absent from your corpus (a wrong
price that is no record's real price), distorted quotes, near-miss product
names — removed and visibly marked `⟨unverified …, removed⟩`, never
silently corrected.

**What it doesn't catch**: lies told in plain prose without a checkable
atom, paraphrased quotes, inventions with no anchor in your glossary or
packs. Those are flagged unverifiable at best — which is the honest signal
that a human should look.

## License & commercial use

**FSL-1.1-ALv2** (Functional Source License) — see [LICENSE](LICENSE).

Open-core, in plain words: **free to self-host for your own use**, even
commercially — verifying your own AI's answers costs nothing and needs no
permission. You only need a commercial agreement to **embed or resell**
VeriGate inside a product you sell, or to offer a competing product on its
code. Two years after each release, that version becomes Apache-2.0. This
protects the work, not the idea (D-017).

Want to embed it, or want support / an enterprise tier? →
[COMMERCIAL.md](COMMERCIAL.md).
