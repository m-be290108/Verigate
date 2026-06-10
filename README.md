# VeriGate (working name)

**Deterministic verification layer for generative AI answers.**
No LLM. No network. On-premise only. Audit-ready.

> Your AI answers from YOUR data? VeriGate checks every answer against that
> data: every reference, figure, quote and known entity is confronted with
> your trusted corpus. What is false is removed and visibly marked. What
> cannot be verified is flagged. Never a blind blessing. Tamper-evident
> audit log included.

*(Skeleton README — pitch, demo and bench numbers land at the end of the MVP.)*

## What VeriGate is — and is not (honesty section)

VeriGate does **not** "detect hallucinations" in any magical sense. It
verifies the **verifiable atoms** of an answer against a trusted corpus:

- **references / identifiers** (SKUs, legal refs, DOIs, internal codes…),
- **anchored figures** (money, percentages, numbers with units, dates),
- **quoted text** (does this sentence exist in the corpus?),
- **glossary entities** (product names, people, reference amounts).

Free-form prose that contains none of these is marked **unverifiable** —
never validated. That is precisely what makes the product defensible: a
score that means something, reproducible byte-for-byte, that you can show a
regulator or a customer. Any marketing claim beyond this is a lie, and this
repository refuses to make it.

## License

BUSL-1.1 (placeholder — final license TBD).
