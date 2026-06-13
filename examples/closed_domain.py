#!/usr/bin/env python3
"""closed_domain.py — the VeriGate guarantee on a bounded domain.

Demonstrates `VerifyConfig(scoped=True, strict=True)`: on a closed domain,
no verifiable fact that isn't grounded in the corpus FOR THE SUBJECT of the
answer reaches the user — it is removed, or the answer abstains.

Part A (always runs, no LLM): three hand-built answers through scoped+strict,
proving the guarantee deterministically — including a cross-attribution (a
real catalog price attributed to the wrong product), which plain membership
would bless and scoping catches.

Part B (only if a local Ollama is up): the live loop — the model answers, we
verify scoped+strict, and if the result is not grounded we ABSTAIN instead of
showing it. VeriGate never connects to the model; this code drives the loop.

Run:  PYTHONPATH=src python examples/closed_domain.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # noqa: E402

from verigate import Gate  # noqa: E402
from verigate.verify.engine import VerifyConfig  # noqa: E402

DATA = Path(__file__).resolve().parent / "data"
OLLAMA = "http://localhost:11434/api/chat"
MODEL = "gemma3:4b"
REFUSAL = "I can't confirm that from the official catalog."


def banner(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def show(answer: str, report) -> None:
    print(f"\nanswer:    {answer}")
    print(f"verdict:   {report.verdict.value}  (grounded={report.is_grounded})")
    for r in report.atoms:
        if r.status.value != "verified":
            print(f"  ✗ {r.atom.type.value} «{r.atom.raw}» — {r.status.value}: {r.detail}")
    # The closed-domain delivery: show the answer only if grounded, else abstain.
    delivered = report.corrected_answer if report.is_grounded else REFUSAL
    print(f"delivered: {delivered}")


def part_a(gate: Gate) -> None:
    banner("Part A — the guarantee, deterministically (no LLM)")
    # 1. Clean, grounded.
    a1 = "The HydroNova AquaJet 2200 (SKU HN-2200-P) costs €349.90."
    r1 = gate.verify(a1)
    show(a1, r1)
    assert r1.is_grounded

    # 2. Cross-attribution: €1299.00 is real — it is the TidalMax Pro's price,
    #    attributed here to the AquaJet 2200. Membership would bless it.
    a2 = "The HydroNova AquaJet 2200 costs €1299.00."
    r2 = gate.verify(a2)
    show(a2, r2)
    assert not r2.is_grounded
    assert "cross-attribution" in " ".join(x.detail for x in r2.atoms)

    # 3. Invented product name → flagged; the answer is not grounded.
    a3 = "The HydroNova AquaJet 5000 is our newest pump."
    r3 = gate.verify(a3)
    show(a3, r3)
    assert not r3.is_grounded

    print("\n✓ guarantee held: grounded answers delivered, ungrounded ones "
          "corrected or refused — deterministic, zero LLM, zero network.")


def ollama_up() -> bool:
    try:
        urllib.request.urlopen(
            urllib.request.Request("http://localhost:11434/api/tags"), timeout=3
        )
        return True
    except (urllib.error.URLError, OSError):
        return False


def ask_llm(question: str, catalog: str) -> str:
    body = json.dumps({
        "model": MODEL,
        "stream": False,
        "options": {"temperature": 0, "seed": 7},
        "messages": [
            {"role": "system", "content":
                "You are HydroNova's support chatbot. Answer in one sentence "
                "using only the official catalog."},
            {"role": "user", "content": f"Official catalog:\n{catalog}\n\nQ: {question}"},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["message"]["content"].strip()


def part_b(gate: Gate) -> None:
    banner("Part B — live: the model answers, we deliver only if grounded")
    if not ollama_up():
        print("Ollama not reachable on localhost:11434 — skipping the live part "
              "(`ollama serve` + `ollama pull gemma3:4b` to enable it).")
        return
    catalog = (DATA / "catalog.md").read_text(encoding="utf-8")
    for q in ["How much is the AquaJet 2200?",
              "What's the price and warranty of the TidalMax Pro?"]:
        answer = ask_llm(q, catalog)
        report = gate.verify(answer)
        print(f"\nQ: {q}")
        show(answer, report)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "corpus.db"
        Gate.ingest(DATA, db)
        gate = Gate(db, config=VerifyConfig(scoped=True, strict=True))
        part_a(gate)
        part_b(gate)
        gate.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
