# The VeriGate guarantee (and its honest limits)

This is the document an enterprise security, compliance or procurement team
should read before deploying VeriGate. It states precisely what VeriGate
guarantees, how, and what it does **not** guarantee. Nothing here is marketing;
every claim maps to a tested behavior or a documented limitation in
`DECISIONS.md`.

## The guarantee, in one sentence

> On a closed domain, no **verifiable fact** that isn't grounded in your corpus
> **for the subject of the answer** reaches the user — it is removed, or the
> answer abstains. Deterministic, reproducible, auditable.
>
> **VeriGate guarantees facts, not judgment.**

## What "a fact" means here

VeriGate does not reason about meaning. It verifies the **discrete, checkable
atoms** of an answer against your corpus:

- **references / identifiers** — SKUs, legal citations, DOIs, ISO/RFC numbers,
  internal codes, `[REF: …]` tags (plus any format you add as a YAML pack);
- **anchored figures** — money, percentages, numbers with units, dates
  (a bare integer with no unit is *not* a checkable fact — it matches
  everything, so it is deliberately ignored, see D-004);
- **verbatim quotes** — text in quotation marks, matched normalized;
- **glossary entities** — product names, people, any named subject of your
  corpus.

Everything else in an answer — reasoning, advice, tone, implication,
recommendation — is **judgment**, not fact. VeriGate does not and cannot verify
judgment. A bot can still give bad advice over a perfect database; that is
outside any deterministic system's reach, and we say so plainly rather than
pretend otherwise.

## How VeriGate knows it is the *right* datum

The danger is confusing one value with a similar value that has a different
meaning. VeriGate resolves this by **deterministic narrowing** — never by
guessing — across three levels:

1. **Type.** A number is never matched bare. It carries its kind and unit:
   `money:EUR:999` is a different fact from `unit:l_min:999`. A price can never
   be confused with a flow rate.
2. **Subject (section scope).** The fact must exist in the corpus **for the
   subject the answer is about**. "Product A costs €999" is verified against
   A's record only; if €999 is in fact B's price, it is **not** found for A and
   is flagged as cross-attribution. This is what stops a real value being
   attributed to the wrong subject (D-013 → resolved by scoped mode).
3. **Field (optional, for high-stakes domains).** Within the right subject, the
   value can be tied to the right attribute (price vs. deposit) by matching the
   label next to the number in the answer against the labelled field in the
   corpus. Deterministic, requires a well-structured corpus.

Each fact resolves to a precise, reproducible **address**:
`document › section › subject › field`. That address appears in the report and
in the audit trail — so every validated fact is traceable to its exact source
coordinate. No training, no embeddings, no model: the right datum is reached by
elimination, not by prediction.

## What VeriGate guarantees (in scoped + strict mode)

- Every checkable atom in a covered format is **grounded for its subject, or
  removed / refused**. The user never sees an ungrounded checkable fact.
- The verdict and report are **byte-for-byte reproducible**: same corpus + same
  answer → identical report, forever. Provable in a regulator's office, not a
  probability.
- Every verification is **journaled in a tamper-evident HMAC chain**, with the
  source address of each validated fact; the chain is re-verified at export.
- **Zero network**: nothing leaves your infrastructure (enforced by a
  socket-guard test). With a local LLM, the whole pipeline is offline.

## What VeriGate does NOT guarantee (read this too)

- **Judgment, reasoning, advice.** No checkable atom → not verified. "Facts,
  not judgment."
- **Corpus completeness.** If the answer concerns something absent from your
  corpus, VeriGate makes the system **abstain** ("I don't have that
  information") — it does not fill the gap. Refusing to invent is the feature;
  inventing the missing knowledge is not something any honest tool can do.
- **Formats you haven't configured.** Coverage is exactly the atom types above
  plus your packs. A fabricated value in an unconfigured format passes through
  as unverifiable prose.
- **Paraphrased quotes.** Verbatim (normalized) only.
- **General-purpose assistants.** The guarantee holds on a **bounded domain**
  where your corpus is the source of truth — not on an open-ended chatbot. The
  narrowness of the domain is what makes the guarantee possible.
- **The audit threat model.** Tamper-evidence holds against modification of the
  audit database alone. By default the HMAC secret sits next to it (mode 0600);
  an attacker with full filesystem access could read it and forge a chain.
  Supply the secret out-of-band (`VERIGATE_AUDIT_SECRET`) for that threat model.

## Two deployment postures

- **Strict (closed-world).** Anything not grounded is removed or the answer
  abstains. The user only ever sees grounded facts. Use for high-stakes,
  regulated, customer-facing answers.
- **Flag-only (advisory).** Nothing is removed; the report annotates what could
  not be verified, for a human or your UI to act on. Use when a marked gap in a
  live answer would be worse than a flag (the real-data lesson).

## Why this is different from every competitor

Competitors fight on **coverage** — "we catch more hallucinations" — using ML
judges that are themselves unreliable, and some claim "100% elimination," which
is false. VeriGate does not win on coverage. It wins on **proof**:

> It does not catch *more*. What it catches, it catches **provably,
> reproducibly, and with the exact source address** — and what it cannot prove,
> it refuses rather than blesses.

That is the only honest, defensible guarantee in this space, and it is the
whole product.
