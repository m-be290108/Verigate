---
name: "Missed lie or false positive"
about: "VeriGate verified something false, or flagged something true. The most
  valuable report there is."
title: "[verification] "
labels: ["verification-gap"]
---

## Which way did it fail?

- [ ] **Missed lie** — a fabricated reference / figure / quote / entity was
  reported `VERIFIED` (or not removed).
- [ ] **False positive** — a value that IS in the corpus was flagged / removed.

## Minimal reproduction

The corpus content (or a tiny excerpt), the answer text, and the report.
Ideally a few lines that build a corpus and call `verify()`:

```python
from verigate import Gate
# Gate.ingest(folder, "corpus.db")   # or describe the corpus
gate = Gate("corpus.db")
report = gate.verify("...your answer...")
print(report.to_json())
```

## What you expected vs. what happened

- Expected:
- Actual:

## Is it already a documented limitation?

Please skim the README "What it is — and is not" section and `DECISIONS.md`
(especially D-013 membership-not-association). If your case is one of those,
it is a known trade-off — feel free to open a **discussion** about widening
coverage instead.

- [ ] I checked, and this is NOT a documented limitation.
