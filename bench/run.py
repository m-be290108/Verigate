"""Self-validating benchmark runner — produces the two published numbers.

Pipeline (fully offline, no LLM, deterministic for a given seed):

    generate_corpus -> write_corpus_files (temp dir) -> ingest_folder
    -> Gate -> verify every generated answer

Metrics, always BOTH:

* **detection rate** — fraction of corrupted answers where the engine
  flagged *the injected lie itself* as false (canonical or raw match among
  the false atoms) AND the verdict is non-VERIFIED. Flagging "something"
  while missing the lie does not count.
* **false-positive rate** — false atoms in clean answers / total checkable
  atoms in clean answers (checkable = verified + false, D-003 denominator).

Gates (CI runs this via ``make verify`` -> ``bench-quick``): detection
>= 0.95 AND false-positive rate <= 0.02 -> exit 0; otherwise explicit FAIL
lines and exit 1.

Determinism: same seed -> byte-identical ``--json`` output. No wall-clock,
no durations, no filesystem paths in the payload (D-006 spirit).
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from bench.generate import (
    CORRUPTION_TYPES,
    DEFAULT_SEED,
    generate_answers,
    generate_corpus,
    write_corpus_files,
)
from verigate.ingest.ingestor import ingest_folder
from verigate.sdk import Gate
from verigate.types import FALSE_STATUSES, Verdict

DETECTION_TARGET = 0.95
FP_TARGET = 0.02

#: Sizes: (n_products, n_clean, n_corrupted).
QUICK_SIZES = (25, 40, 60)
FULL_SIZES = (100, 150, 250)


@dataclass
class BenchResult:
    """Everything the runner measured — deterministic for a given seed."""

    seed: int
    n_products: int
    n_clean: int
    n_corrupted: int
    corpus_docs: int
    corpus_refs: int
    corpus_numbers: int
    corpus_entities: int
    corpus_fingerprint: str
    detected_by_type: dict[str, int] = field(default_factory=dict)
    total_by_type: dict[str, int] = field(default_factory=dict)
    clean_checkable: int = 0
    clean_false: int = 0
    false_positives: list[dict] = field(default_factory=list)
    undetected: list[dict] = field(default_factory=list)

    @property
    def n_detected(self) -> int:
        return sum(self.detected_by_type.values())

    @property
    def detection_rate(self) -> float:
        return self.n_detected / self.n_corrupted if self.n_corrupted else 0.0

    @property
    def fp_rate(self) -> float:
        return self.clean_false / self.clean_checkable if self.clean_checkable else 0.0

    @property
    def gates_ok(self) -> bool:
        return self.detection_rate >= DETECTION_TARGET and self.fp_rate <= FP_TARGET

    def to_dict(self) -> dict:
        return {
            "seed": self.seed,
            "n_products": self.n_products,
            "n_clean": self.n_clean,
            "n_corrupted": self.n_corrupted,
            "corpus": {
                "docs": self.corpus_docs,
                "refs": self.corpus_refs,
                "numbers": self.corpus_numbers,
                "entities": self.corpus_entities,
                "fingerprint": self.corpus_fingerprint,
            },
            "detection_rate": round(self.detection_rate, 6),
            "false_positive_rate": round(self.fp_rate, 6),
            "per_type": {
                t: {"detected": self.detected_by_type[t], "total": self.total_by_type[t]}
                for t in CORRUPTION_TYPES
                if self.total_by_type.get(t)
            },
            "clean": {
                "checkable_atoms": self.clean_checkable,
                "false_atoms": self.clean_false,
            },
            "false_positives": self.false_positives,
            "undetected": self.undetected,
            "gates": {
                "detection_target": DETECTION_TARGET,
                "fp_target": FP_TARGET,
                "pass": self.gates_ok,
            },
        }

    def to_json(self) -> str:
        """Byte-identical for identical inputs (sort_keys, fixed separators)."""
        return json.dumps(
            self.to_dict(), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )


def run_bench(
    seed: int, n_products: int, n_clean: int, n_corrupted: int
) -> BenchResult:
    """Generate, ingest, verify; return the measured :class:`BenchResult`."""
    rng = random.Random(seed)
    model = generate_corpus(rng, n_products)
    answers = generate_answers(rng, model, n_clean, n_corrupted)

    with tempfile.TemporaryDirectory(prefix="verigate-bench-") as td:
        folder = Path(td) / "corpus"
        write_corpus_files(model, folder)
        db_path = Path(td) / "corpus.db"
        ingest = ingest_folder(folder, db_path)
        result = BenchResult(
            seed=seed,
            n_products=n_products,
            n_clean=n_clean,
            n_corrupted=n_corrupted,
            corpus_docs=ingest.n_docs,
            corpus_refs=ingest.n_refs,
            corpus_numbers=ingest.n_numbers,
            corpus_entities=ingest.n_entities,
            corpus_fingerprint=ingest.fingerprint,
        )
        with Gate(db_path) as gate:
            for index, answer in enumerate(answers):
                report = gate.verify(answer.text)
                false_atoms = [r for r in report.atoms if r.status in FALSE_STATUSES]
                if answer.label == "clean":
                    result.clean_checkable += report.n_verified + report.n_false
                    result.clean_false += len(false_atoms)
                    for r in false_atoms:
                        result.false_positives.append(
                            {
                                "answer_index": index,
                                "text": answer.text,
                                "raw": r.atom.raw,
                                "canonical": r.atom.canonical,
                                "status": r.status.value,
                                "detail": r.detail,
                            }
                        )
                    continue
                kind = answer.label
                result.total_by_type[kind] = result.total_by_type.get(kind, 0) + 1
                inj = answer.injected
                lie_flagged = any(
                    r.atom.canonical == inj.canonical or inj.value in r.atom.raw
                    for r in false_atoms
                )
                if report.verdict is not Verdict.VERIFIED and lie_flagged:
                    result.detected_by_type[kind] = (
                        result.detected_by_type.get(kind, 0) + 1
                    )
                else:
                    result.undetected.append(
                        {
                            "answer_index": index,
                            "text": answer.text,
                            "type": kind,
                            "injected_value": inj.value,
                            "injected_canonical": inj.canonical,
                            "verdict": report.verdict.value,
                            "false_atoms": [
                                {"raw": r.atom.raw, "canonical": r.atom.canonical}
                                for r in false_atoms
                            ],
                        }
                    )
        for kind in result.total_by_type:
            result.detected_by_type.setdefault(kind, 0)
    return result


def _print_report(result: BenchResult) -> None:
    print("VeriGate self-validating benchmark")
    print(
        f"seed={result.seed} products={result.n_products} "
        f"clean={result.n_clean} corrupted={result.n_corrupted}"
    )
    print(
        f"corpus: {result.corpus_docs} documents, {result.corpus_refs} refs, "
        f"{result.corpus_numbers} numbers, {result.corpus_entities} entities "
        f"(fingerprint {result.corpus_fingerprint[:12]}…)"
    )
    print()
    print(f"Detection rate: {result.detection_rate * 100:.1f}% (target >= 95%)")
    print(f"False-positive rate: {result.fp_rate * 100:.2f}% (target <= 2%)")
    print()
    print("Per-type detection:")
    for kind in CORRUPTION_TYPES:
        total = result.total_by_type.get(kind, 0)
        if total:
            detected = result.detected_by_type.get(kind, 0)
            print(f"  {kind:<24} {detected}/{total}")
    if result.fp_rate > 0:
        print()
        print("False positives in clean answers (per-answer detail):")
        for fp in result.false_positives:
            print(
                f"  answer #{fp['answer_index']}: [{fp['status']}] "
                f"raw={fp['raw']!r} canonical={fp['canonical']!r} — {fp['detail']}"
            )
            print(f"    text: {fp['text']}")
    if result.undetected:
        print()
        print("Undetected lies (per-answer detail):")
        for miss in result.undetected:
            print(
                f"  answer #{miss['answer_index']}: type={miss['type']} "
                f"injected={miss['injected_value']!r} verdict={miss['verdict']}"
            )
            print(f"    text: {miss['text']}")


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark; exit 0 iff both gates are met."""
    parser = argparse.ArgumentParser(
        prog="bench.run",
        description="VeriGate self-validating benchmark (deterministic, offline).",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help=f"quick sizes {QUICK_SIZES} instead of full {FULL_SIZES} (CI gate)",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED, help=f"RNG seed (default {DEFAULT_SEED})"
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="print the deterministic JSON payload only (byte-identical per seed)",
    )
    args = parser.parse_args(argv)

    n_products, n_clean, n_corrupted = QUICK_SIZES if args.quick else FULL_SIZES
    result = run_bench(args.seed, n_products, n_clean, n_corrupted)

    if args.as_json:
        print(result.to_json())
    else:
        _print_report(result)
        print()

    failures: list[str] = []
    if result.detection_rate < DETECTION_TARGET:
        failures.append(
            f"FAIL: detection rate {result.detection_rate * 100:.1f}% "
            f"< {DETECTION_TARGET * 100:.0f}%"
        )
    if result.fp_rate > FP_TARGET:
        failures.append(
            f"FAIL: false-positive rate {result.fp_rate * 100:.2f}% "
            f"> {FP_TARGET * 100:.0f}%"
        )
    if failures:
        # In --json mode the FAIL lines go to stderr so stdout stays pure
        # (byte-identical) JSON; the exit code carries the gate either way.
        stream = sys.stderr if args.as_json else sys.stdout
        for line in failures:
            print(line, file=stream)
        return 1
    if not args.as_json:
        print("PASS: both gates met.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
