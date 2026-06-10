#!/usr/bin/env python3
"""VeriGate sales demo — deterministic verification, no LLM, no network.

Five AI-style answers about the fictional HydroNova catalog
(``examples/data``) run through ``Gate.verify``. Two are fully grounded in
the corpus; three contain classic hallucinations (an invented SKU, a wrong
price, a distorted quote plus an invented product variant). VeriGate flags
every ungrounded atom and rewrites the answer with a VISIBLE removal marker
(never a silent "correction") — that rewrite is the money shot.

Honest framing: VeriGate certifies that an answer is GROUNDED IN THE CORPUS
you trust. It does not — and does not claim to — detect all hallucinations.

Run from the repo root::

    PYTHONPATH=src python examples/proof.py

(with verigate pip-installed, plain ``python examples/proof.py`` works too).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def _bootstrap() -> None:
    """Make `verigate` importable from a source checkout without PYTHONPATH."""
    try:
        import verigate  # noqa: F401
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


_bootstrap()

from verigate.sdk import Gate  # noqa: E402
from verigate.types import (  # noqa: E402
    REMOVAL_MARKERS,
    AtomStatus,
    AtomType,
    Report,
    Verdict,
)

DATA_DIR = Path(__file__).resolve().parent / "data"

#: (answer, expected verdict) — crafted against examples/data.
CASES: list[tuple[str, Verdict]] = [
    # 1 — clean: real SKU + real price -> VERIFIED
    (
        "The HydroNova AquaJet 2200 (SKU HN-2200-P) costs €349.90.",
        Verdict.VERIFIED,
    ),
    # 2 — clean: exact warranty quote + real product name -> VERIFIED
    (
        'The catalog states: "All HydroNova products include a minimum '
        '12-month manufacturer warranty." This covers the '
        "HydroNova ClearStream Duo.",
        Verdict.VERIFIED,
    ),
    # 3 — INVENTED SKU (plausible format) -> removed, marker visible
    (
        "Order spare cartridge HN-9999-Q for the HydroNova PureFlow Mini "
        "filter.",
        Verdict.CORRECTED,
    ),
    # 4 — right SKU, WRONG price -> false number removed, marker visible
    (
        "The HydroNova AquaJet 2200 (SKU HN-2200-P) sells for €299.90.",
        Verdict.CORRECTED,
    ),
    # 5 — DISTORTED quote (before -> after) + invented product variant
    #     -> false quote + entity near-miss ('closest known:' detail)
    (
        'Per the manual, "Always disconnect the pump from the mains after '
        'any maintenance operation." The HydroNova AquaJet 2500 is the '
        "newest model.",
        Verdict.INSUFFICIENT,
    ),
]

# ---------------------------------------------------------------- display

_COLORS: dict[Verdict, str] = {
    Verdict.VERIFIED: "\x1b[32m",      # green
    Verdict.CORRECTED: "\x1b[33m",     # yellow
    Verdict.INSUFFICIENT: "\x1b[31m",  # red
    Verdict.UNVERIFIABLE: "\x1b[90m",  # gray
}
_RESET = "\x1b[0m"

_ICONS: dict[AtomStatus, str] = {
    AtomStatus.VERIFIED: "✅",
    AtomStatus.MISMATCHED: "❌",
    AtomStatus.NOT_FOUND: "❌",
    AtomStatus.UNVERIFIABLE: "➖",
}


def _use_color() -> bool:
    """ANSI only on a real TTY and when NO_COLOR is unset (no-color.org)."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _verdict_label(verdict: Verdict) -> str:
    if _use_color():
        return f"{_COLORS[verdict]}{verdict.value}{_RESET}"
    return verdict.value


def _print_case(number: int, answer: str, report: Report) -> None:
    print(f"--- Case {number} " + "-" * (66 - len(str(number))))
    print(f"Answer:    {answer}")
    print(
        f"Verdict:   {_verdict_label(report.verdict)} — score "
        f"{report.score:.2f} ({report.n_verified} verified / "
        f"{report.n_false} false / {report.n_unverifiable} unverifiable)"
    )
    for result in report.atoms:
        line = (
            f"  {_ICONS[result.status]} {result.atom.type.value:9s} "
            f"{result.atom.raw!r} — {result.status.value}"
        )
        if result.matched_source:
            line += f" ({result.matched_source})"
        elif result.detail:
            line += f" — {result.detail}"
        print(line)
    if report.corrected_answer != answer:
        print(f"Corrected: {report.corrected_answer}")
    print()


# ------------------------------------------------------------------ checks

_PASSED: list[str] = []


def _check(condition: bool, label: str) -> None:
    assert condition, f"demo assertion failed: {label}"
    _PASSED.append(label)


def main() -> int:
    print("=== VeriGate — deterministic verification demo ===")
    print("Every claim below is checked for being grounded in the trusted")
    print(f"corpus ({DATA_DIR}) — pure regex + sqlite, zero LLM, zero network.")
    print()

    with tempfile.TemporaryDirectory(prefix="verigate-demo-") as tmp:
        db_path = Path(tmp) / "corpus.db"  # temporary — never touches the repo
        result = Gate.ingest(DATA_DIR, db_path)
        print(
            f"Ingested {result.n_docs} documents -> {result.n_refs} references, "
            f"{result.n_numbers} numbers, {result.n_entities} entities"
        )
        print(f"Corpus fingerprint: {result.fingerprint[:16]}…")
        print()

        with Gate(db_path) as gate:
            reports: list[Report] = []
            for i, (answer, expected) in enumerate(CASES, start=1):
                report = gate.verify(answer)
                _print_case(i, answer, report)
                _check(
                    report.verdict is expected,
                    f"case {i} verdict {report.verdict.value} == {expected.value}",
                )
                reports.append(report)

            # The markers ARE the product: visible removal, never silent.
            r3, r4, r5 = reports[2], reports[3], reports[4]
            _check(
                REMOVAL_MARKERS[AtomType.REFERENCE] in r3.corrected_answer
                and "HN-9999-Q" not in r3.corrected_answer,
                "case 3: invented SKU removed with a visible marker",
            )
            _check(
                REMOVAL_MARKERS[AtomType.NUMBER] in r4.corrected_answer
                and "299.90" not in r4.corrected_answer,
                "case 4: wrong price removed with a visible marker",
            )
            _check(
                REMOVAL_MARKERS[AtomType.QUOTE] in r5.corrected_answer,
                "case 5: distorted quote removed with a visible marker",
            )
            _check(
                any("closest known:" in res.detail for res in r5.atoms),
                "case 5: entity near-miss reports the closest known entity",
            )

            # Byte-identical reproducibility (D-006): same corpus + same
            # answer -> the exact same report bytes.
            again = gate.verify(CASES[0][0])
            _check(
                again.to_json() == reports[0].to_json(),
                "same input -> byte-identical report (deterministic)",
            )

    print(
        f"✓ all {len(_PASSED)} assertions passed — deterministic, "
        "zero LLM, zero network"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
