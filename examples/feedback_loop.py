#!/usr/bin/env python3
"""feedback_loop.py — how VeriGate COLLABORATES with an LLM.

The key architectural point this demo makes concrete:

    VeriGate never connects to the LLM itself. It must not — a network call
    would break the "offline, deterministic, no-LLM" guarantee that is the
    whole product. Instead, YOUR thin orchestration code sits between the two:

        1. you call your LLM            (network lives HERE, in your code)
        2. you call gate.verify(answer) (pure, offline, deterministic)
        3. if VeriGate flagged atoms, you feed its report BACK to the LLM
           and ask it to answer again using only verified facts
        4. repeat until the answer is clean (or give up and show the
           corrected/flagged version)

So VeriGate and the LLM absolutely collaborate — through `verify()`'s
structured report, which is designed to be handed back to the model. The
collaboration is a LOOP your code drives, not a connection VeriGate opens.

This script uses a local Ollama model (so it stays offline), but the LLM
could be Anthropic, OpenAI, anything — only step 1 changes.

Run:  ollama serve  &&  PYTHONPATH=src python examples/feedback_loop.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# --- example bootstrap so `python examples/feedback_loop.py` just works ----
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))  # noqa: E402

from verigate import Gate  # noqa: E402
from verigate.types import FALSE_STATUSES, Verdict  # noqa: E402

OLLAMA = "http://localhost:11434/api/chat"
MODEL = "gemma3:4b"
DATA = Path(__file__).resolve().parent / "data"

SYSTEM = (
    "You are the customer-support chatbot of HydroNova, a water-pump brand. "
    "Answer the customer in 2-3 sentences, in English, using ONLY the official "
    "catalog facts you are given. Give concrete prices, SKUs and warranties."
)

CATALOG_CONTEXT = (DATA / "catalog.md").read_text(encoding="utf-8")


def ask_llm(question: str, correction: str = "") -> str:
    """Step 1: call the LLM. This is the ONLY networked part, and it lives in
    the example (your code), never inside VeriGate."""
    user = f"Official catalog:\n{CATALOG_CONTEXT}\n\nCustomer question: {question}"
    if correction:
        user += (
            f"\n\nIMPORTANT — a deterministic verifier checked your previous "
            f"answer against the official catalog and rejected the following "
            f"because they do not appear in it:\n{correction}\n"
            f"Answer again. Use ONLY facts present in the catalog above. For "
            f"anything not in the catalog, say plainly that it is not available."
        )
    body = json.dumps(
        {
            "model": MODEL,
            "stream": False,
            "options": {"temperature": 0, "seed": 7},
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())["message"]["content"].strip()


def rejected_summary(report) -> str:
    """Turn VeriGate's report into a correction the LLM can act on."""
    lines = []
    for r in report.atoms:
        if r.status in FALSE_STATUSES:
            lines.append(f'  - {r.atom.type.value} "{r.atom.raw}": {r.detail}')
    return "\n".join(lines)


def run(question: str, gate: Gate, max_rounds: int = 3) -> None:
    print(f"\n{'='*70}\nCUSTOMER: {question}\n{'='*70}")
    correction = ""
    for rnd in range(1, max_rounds + 1):
        answer = ask_llm(question, correction)
        report = gate.verify(answer, context=[CATALOG_CONTEXT])
        print(f"\n— Round {rnd} — LLM said:")
        print(f"  {answer}")
        print(f"  VeriGate: {report.verdict.value} (score {report.score:.2f}, "
              f"{report.n_false} rejected)")
        if report.verdict == Verdict.VERIFIED:
            print("  ✓ grounded — nothing to correct. Loop ends.")
            return
        print("  ✗ rejected atoms (fed back to the LLM):")
        print(rejected_summary(report) or "    (none)")
        print(f"  corrected answer shown to user if we stop now:\n"
              f"    {report.corrected_answer}")
        correction = rejected_summary(report)
    print("\n  → max rounds reached. The user is shown the CORRECTED answer "
          "(false atoms removed), never the raw hallucination.")


def main() -> int:
    try:
        urllib.request.urlopen(  # liveness probe
            urllib.request.Request("http://localhost:11434/api/tags"), timeout=3
        )
    except (urllib.error.URLError, OSError):
        print("Ollama not reachable on localhost:11434 — start it with "
              "`ollama serve` and `ollama pull gemma3:4b`. Skipping demo.")
        return 0

    print("VeriGate × LLM — the collaboration loop (VeriGate stays offline; "
          "the loop is driven here, in your code).")
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "corpus.db"
        Gate.ingest(DATA, db)
        gate = Gate(db)
        # A question that nudges the model to invent: the AquaJet 2500 does
        # not exist in the catalog (only the 2200 does).
        run("How much is the HydroNova AquaJet 2500 and what's its warranty?", gate)
        # A fully answerable one — should verify on round 1.
        run("What does the TidalMax Pro cost and how long is its warranty?", gate)
        gate.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
