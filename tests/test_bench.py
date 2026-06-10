"""Tests for the self-validating benchmark (bench/generate.py + bench/run.py).

The bench is itself a deterministic artifact: same seed -> same model, same
answers, byte-identical --json output. These tests pin that contract and
prove the published gates (detection >= 95%, false positives <= 2%) are met
end-to-end through the REAL pipeline (ingest -> Gate -> verify), offline.
"""

from __future__ import annotations

import random
import re

import pytest

from bench import run as bench_run
from bench.generate import (
    CORRUPTION_TYPES,
    DEFAULT_SEED,
    generate_answers,
    generate_corpus,
    write_corpus_files,
)

#: Small end-to-end sizes — fast enough for the unit suite, large enough to
#: exercise every corruption type twice (14 = 2 full cycles of 7).
SMALL = {"n_products": 8, "n_clean": 10, "n_corrupted": 14}


def _generate(seed: int, n_products: int = 10, n_clean: int = 6, n_corrupted: int = 7):
    """One deterministic generation pass: (model, answers)."""
    rng = random.Random(seed)
    model = generate_corpus(rng, n_products)
    answers = generate_answers(rng, model, n_clean, n_corrupted)
    return model, answers


# ------------------------------------------------------------ determinism


def test_same_seed_reproduces_model_and_answers():
    model_a, answers_a = _generate(DEFAULT_SEED)
    model_b, answers_b = _generate(DEFAULT_SEED)
    assert model_a == model_b
    assert answers_a == answers_b


def test_different_seeds_differ():
    model_a, answers_a = _generate(DEFAULT_SEED)
    model_b, answers_b = _generate(DEFAULT_SEED + 1)
    assert model_a != model_b
    assert answers_a != answers_b


# -------------------------------------------------------- generated content


def test_clean_answers_contain_no_injected_mutations():
    _, answers = _generate(DEFAULT_SEED, n_clean=12, n_corrupted=14)
    cleans = [a for a in answers if a.label == "clean"]
    corrupted = [a for a in answers if a.label != "clean"]
    assert len(cleans) == 12 and len(corrupted) == 14
    for clean in cleans:
        assert clean.injected is None
        for bad in corrupted:
            assert bad.injected.value not in clean.text


def test_each_corrupted_answer_contains_its_injected_string():
    _, answers = _generate(DEFAULT_SEED, n_corrupted=14)
    corrupted = [a for a in answers if a.label != "clean"]
    # Types are cycled deterministically: i -> CORRUPTION_TYPES[i % 7].
    assert [a.label for a in corrupted] == [
        CORRUPTION_TYPES[i % len(CORRUPTION_TYPES)] for i in range(14)
    ]
    for answer in corrupted:
        assert answer.injected is not None
        assert answer.injected.value in answer.text
        assert answer.injected.canonical


def test_ean13_checksums_valid():
    model, _ = _generate(DEFAULT_SEED, n_products=20)
    for product in model.products:
        ean = product.ean13
        assert re.fullmatch(r"\d{13}", ean)
        # Independent recomputation of the GS1 checksum (weights 1,3,1,3,…).
        total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(ean[:12]))
        assert int(ean[12]) == (10 - total % 10) % 10


def test_model_facts_well_formed():
    model, _ = _generate(DEFAULT_SEED, n_products=20)
    names = {p.name for p in model.products}
    skus = {p.sku for p in model.products}
    eans = {p.ean13 for p in model.products}
    assert len(names) == len(skus) == len(eans) == 20  # all unique
    for p in model.products:
        assert re.fullmatch(r"\d+\.\d{2}", p.price_eur)  # 2-decimal EUR
        assert re.fullmatch(r"[A-Z]{2}-\d{4}-[A-Z]", p.sku)  # letters-digits-letter
        assert p.name.endswith(f" {p.model_number}")
        assert len(p.quote.split()) >= 3
    assert model.support_url.startswith("https://")


def test_write_corpus_files_layout(tmp_path):
    model, _ = _generate(DEFAULT_SEED, n_products=5)
    write_corpus_files(model, tmp_path / "corpus")
    catalog = (tmp_path / "corpus" / "catalog.md").read_text(encoding="utf-8")
    csv_text = (tmp_path / "corpus" / "products.csv").read_text(encoding="utf-8")
    policy = (tmp_path / "corpus" / "policy.md").read_text(encoding="utf-8")
    for p in model.products:
        assert f"## {p.name} (SKU {p.sku})" in catalog
        assert f'"{p.quote}"' in catalog
        assert f"€{p.price_eur}" in catalog
    lines = csv_text.strip().splitlines()
    assert lines[0] == "sku,name,price_eur,warranty_months"
    assert len(lines) == 1 + len(model.products)
    assert model.support_url in policy
    assert f"within {model.return_days} days" in policy


# ------------------------------------------------------------- end to end


def test_end_to_end_small_run_meets_gates():
    result = bench_run.run_bench(seed=DEFAULT_SEED, **SMALL)
    assert result.detection_rate >= bench_run.DETECTION_TARGET
    assert result.fp_rate <= bench_run.FP_TARGET
    assert result.gates_ok  # what makes main() exit 0
    # Every corruption type was exercised twice and detected.
    assert result.total_by_type == {t: 2 for t in CORRUPTION_TYPES}
    assert result.detected_by_type == {t: 2 for t in CORRUPTION_TYPES}
    assert result.clean_checkable > 0
    assert result.false_positives == []
    assert result.undetected == []


def test_main_quick_json_byte_identical_and_exit_0(capsys):
    argv = ["--quick", "--json", "--seed", str(DEFAULT_SEED)]
    rc_a = bench_run.main(argv)
    out_a = capsys.readouterr().out
    rc_b = bench_run.main(argv)
    out_b = capsys.readouterr().out
    assert rc_a == rc_b == 0
    assert out_a == out_b  # byte-identical --json output (same seed)
    assert out_a.startswith("{") and '"detection_rate"' in out_a


@pytest.mark.network_guard
def test_bench_pipeline_is_offline(no_network):
    """The whole generate -> ingest -> verify pipeline under the socket
    guard: any outbound connection attempt fails the test."""
    result = bench_run.run_bench(seed=DEFAULT_SEED, **SMALL)
    assert result.gates_ok
