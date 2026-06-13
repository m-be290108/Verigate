# Integrating VeriGate with your LLM

The one thing to understand first: **VeriGate never connects to your LLM.**
It runs *after* the LLM, on the answer text, entirely inside your
infrastructure. Connecting it to the model would break the three guarantees
that are the product — offline, deterministic, no-LLM-in-the-loop.

So "connect VeriGate to my LLM" really means: **insert one call right after
your LLM call, in your own code.** VeriGate doesn't care which model produced
the text (OpenAI, Anthropic, Mistral, a local Llama — anything); it only sees
the output string and your trusted corpus.

```
User → your app → your LLM → ┐
                             │ answer (text)
        corpus.db → verify() ┘ → verified answer → User
        (your documents)        (false atoms removed / flagged)
```

---

## Step 0 — build the corpus (once, then refresh when docs change)

Drop the documents your AI is supposed to know into a folder and ingest them:

```bash
verigate ingest ./your-data --db corpus.db
verigate verify-corpus --db corpus.db      # integrity lock — should print OK
```

`pdf`, `docx`, `md`, `txt`, `csv`, `json` are supported. Re-run `ingest`
whenever the documents change (a deleted file is pruned from the corpus,
D-011). The corpus is a single SQLite file you control.

---

## Case 1 — your backend is Python (in-process library)

The most common case, and the lightest: VeriGate runs *inside* your process,
no separate service. Three lines around your existing LLM call.

Before:

```python
answer = my_llm.generate(prompt)        # your model, any provider
return answer                           # straight to the user
```

After:

```python
from verigate import Gate

gate = Gate("corpus.db")                # build ONCE at startup, reuse it

answer = my_llm.generate(prompt)
report = gate.verify(answer, context=rag_chunks)   # context optional
return report.corrected_answer          # false atoms removed + marked
```

`report` also gives you `report.verdict` (`VERIFIED` / `CORRECTED` /
`INSUFFICIENT` / `UNVERIFIABLE`), `report.score`, and a per-atom breakdown
(`report.to_dict()`) for logging or your own UI.

`context` is optional: pass the RAG chunks the answer was generated from and
they count as trusted-for-this-call, on top of the corpus.

---

## Case 2 — your stack is not Python (local HTTP sidecar)

Node, Java, PHP, Go, .NET… run VeriGate as a small HTTP service on your own
network and call it with one request after each LLM answer.

```bash
verigate serve --db corpus.db --host 127.0.0.1 --port 8470
# add --cache-size 1024 to memoize repeated (answer, context) pairs
```

Then, from any language:

```bash
curl -s http://127.0.0.1:8470/verify \
  -H 'content-type: application/json' \
  -d '{"answer": "Our pump AP-3000-X costs €249.99.", "context": []}'
```

Response (`Report.to_dict()`):

```json
{
  "verdict": "CORRECTED",
  "score": 0.5,
  "counts": {"total": 2, "verified": 1, "false": 1, "unverifiable": 0},
  "corrected_answer": "Our pump AP-3000-X costs ⟨unverified figure, removed⟩.",
  "answer_sha256": "…",
  "corpus_fingerprint": "…",
  "atoms": [ … per-atom status, offsets, matched source … ]
}
```

Node example:

```js
const res = await fetch("http://127.0.0.1:8470/verify", {
  method: "POST",
  headers: { "content-type": "application/json" },
  body: JSON.stringify({ answer, context: ragChunks }),
});
const report = await res.json();
return report.corrected_answer;
```

Other endpoints: `GET /health`, `POST /ingest`, `GET /audit/export`. The
service binds loopback by default and **refuses non-loopback hosts** unless
you deliberately rebind it (on-premise contract). It makes **no outbound
network call**, ever.

---

## Choose the behavior

Same call, three ways to use the result:

- **Flag-only** — keep the original answer, attach the report; let a human (or
  your UI) see what couldn't be verified. *Recommended default for
  customer-facing chat* (real-data eval lesson: don't mutilate live answers).
  Use `report.verdict` / `report.atoms`, ignore `corrected_answer`.
- **Strip** — show `report.corrected_answer`: false atoms are removed and
  visibly marked `⟨unverified …, removed⟩`. Good for internal tools and
  document pipelines where a marked gap beats a wrong value.
- **Feedback loop** — if `report.verdict != VERIFIED`, send the rejected
  atoms back to the LLM and ask it to answer again using only verified facts;
  re-verify; repeat. See [`examples/feedback_loop.py`](../examples/feedback_loop.py)
  for a runnable version against a local model. VeriGate stays offline and
  deterministic — *your* code drives the loop.

---

## Where it physically runs

Everything above happens inside your infrastructure: the library lives in your
process, or the sidecar in a container/VM on your network. The corpus is your
SQLite file. Nothing leaves — that is the point, and it is enforced by a
socket-guard test in the core. For compliance, point `Gate(audit_db=...)` (or
`serve --audit-db`) at an audit database and every verification is journaled
in a tamper-evident HMAC chain (the answer's hash, never its text).
