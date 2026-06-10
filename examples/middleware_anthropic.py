#!/usr/bin/env python3
"""VerifiedLLM — the VeriGate integration pattern customers copy.

Wrap ANY LLM client exposing ``complete(prompt) -> str`` so that every
completion is verified against your trusted corpus BEFORE your application
sees it. Ungrounded atoms (SKUs, prices, quotes, entities) are removed and
replaced by a visible ``⟨unverified …, removed⟩`` marker — never silently
"fixed". Honest framing: VeriGate certifies that an answer is grounded in
the corpus you trust; it does not claim to detect all hallucinations.

This file is 100% offline and deterministic: it never imports ``anthropic``
and never opens a socket. The ``__main__`` demo uses a FakeClient returning
a canned hallucinated answer over ``examples/data``.

Real-world usage with the Anthropic SDK (shown in comments ONLY — not
executed here)::

    # pip install anthropic
    import anthropic

    from verigate.sdk import Gate
    from middleware_anthropic import VerifiedLLM

    class AnthropicClient:
        '''Adapter: Anthropic SDK -> the complete(prompt) -> str protocol.'''

        def __init__(self, model: str = "claude-opus-4-8") -> None:
            # Reads ANTHROPIC_API_KEY from the environment.
            self._client = anthropic.Anthropic()
            self._model = model

        def complete(self, prompt: str) -> str:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                block.text for block in response.content if block.type == "text"
            )

    verified = VerifiedLLM(AnthropicClient(), Gate("corpus.db"))
    answer = verified.complete("What does the HydroNova AquaJet 2200 cost?")
    print(answer.text)                  # corrected answer, markers visible
    print(answer.report.verdict.value)  # VERIFIED / CORRECTED / ...

Run the offline demo from the repo root::

    PYTHONPATH=src python examples/middleware_anthropic.py
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


def _bootstrap() -> None:
    """Make `verigate` importable from a source checkout without PYTHONPATH."""
    try:
        import verigate  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


_bootstrap()

from verigate.sdk import Gate  # noqa: E402
from verigate.types import Report  # noqa: E402


class CompletionClient(Protocol):
    """Anything that turns a prompt into a completion string.

    The Anthropic adapter in the module docstring satisfies this protocol,
    and so does any other vendor SDK wrapped the same way — VeriGate does
    not care where the text comes from.
    """

    def complete(self, prompt: str) -> str:
        """Return the raw completion for `prompt`."""
        ...


@dataclass(frozen=True)
class VerifiedAnswer:
    """A completion AFTER verification.

    `text` is the corrected answer — ungrounded atoms replaced by visible
    ``⟨unverified …, removed⟩`` markers (identical to the raw completion
    when everything verified). `report` is the full deterministic
    :class:`~verigate.types.Report` (verdict, score, per-atom results).
    """

    text: str
    report: Report


class VerifiedLLM:
    """Wrap a :class:`CompletionClient` so every completion passes the Gate.

    The Gate (and its corpus snapshot) is built once by the caller and
    reused for every call — verification adds milliseconds, not an extra
    model round-trip.
    """

    def __init__(self, client: CompletionClient, gate: Gate) -> None:
        self._client = client
        self._gate = gate

    def complete(
        self, prompt: str, context: list[str] | None = None
    ) -> VerifiedAnswer:
        """Complete `prompt`, verify the result, return the safe answer.

        `context` is optional per-call trusted text (e.g. the RAG chunks
        the completion was generated from) — forwarded to ``Gate.verify``.
        """
        raw = self._client.complete(prompt)
        report = self._gate.verify(raw, context)
        return VerifiedAnswer(text=report.corrected_answer, report=report)


# --------------------------------------------------------------- demo only


class FakeClient:
    """Stands in for a real LLM client in this offline demo: returns a
    canned answer mixing real facts (product, SKU) with two hallucinations
    (wrong price, invented spare part)."""

    def complete(self, prompt: str) -> str:
        return (
            "The HydroNova AquaJet 2200 (SKU HN-2200-P) costs €299.90 and "
            "ships with spare part HN-9999-Q."
        )


def main() -> int:
    data_dir = Path(__file__).resolve().parent / "data"
    print("=== VerifiedLLM — before/after (offline FakeClient demo) ===")
    with tempfile.TemporaryDirectory(prefix="verigate-mw-") as tmp:
        db_path = Path(tmp) / "corpus.db"  # temporary — never in the repo
        Gate.ingest(data_dir, db_path)
        with Gate(db_path) as gate:
            llm = VerifiedLLM(FakeClient(), gate)
            answer = llm.complete("What does the HydroNova AquaJet 2200 cost?")

    raw = FakeClient().complete("")
    print()
    print("[before] raw completion (what the LLM said):")
    print(f"  {raw}")
    print()
    report = answer.report
    print(
        f"[after]  verified completion — {report.verdict.value}, "
        f"score {report.score:.2f} ({report.n_verified} verified / "
        f"{report.n_false} false):"
    )
    print(f"  {answer.text}")
    print()

    assert report.verdict.value == "CORRECTED", report.verdict
    assert "⟨unverified figure, removed⟩" in answer.text
    assert "⟨unverified reference, removed⟩" in answer.text
    assert "HN-9999-Q" not in answer.text and "299.90" not in answer.text
    print(
        "✓ middleware demo passed — ungrounded price and SKU removed with "
        "visible markers, grounded facts kept (zero LLM, zero network)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
