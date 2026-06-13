"""Tests for the sales-demo examples (proof.py + middleware_anthropic.py).

Each script is run ONCE as a real subprocess — exactly the way a prospect
runs it (``PYTHONPATH=src python examples/…`` from the repo root) — and the
assertions read the captured stdout. NO_COLOR is forced so the output is
ANSI-free and byte-stable regardless of the environment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from verigate.types import REMOVAL_MARKERS, AtomType

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_example(name: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONPATH="src", NO_COLOR="1", PYTHONIOENCODING="utf-8")
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "examples" / name)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=REPO_ROOT,
        timeout=60,
        check=False,
    )


@pytest.fixture(scope="module")
def proof_run() -> subprocess.CompletedProcess[str]:
    """proof.py executed once for the whole module (keeps the suite fast)."""
    return _run_example("proof.py")


@pytest.fixture(scope="module")
def middleware_run() -> subprocess.CompletedProcess[str]:
    """middleware_anthropic.py executed once for the whole module."""
    return _run_example("middleware_anthropic.py")


# ----------------------------------------------------------------- proof.py


def test_proof_exits_zero_and_reports_assertions(proof_run):
    assert proof_run.returncode == 0, proof_run.stdout + proof_run.stderr
    assert "assertions passed" in proof_run.stdout


def test_proof_shows_all_removal_markers(proof_run):
    """The ⟨…⟩ markers are the demo money shot — all four kinds visible."""
    for atom_type in AtomType:
        assert REMOVAL_MARKERS[atom_type] in proof_run.stdout, atom_type


def test_proof_shows_near_miss_detail_and_all_verdicts(proof_run):
    assert "closest known:" in proof_run.stdout
    for verdict in ("VERIFIED", "CORRECTED", "INSUFFICIENT"):
        assert verdict in proof_run.stdout


def test_proof_no_ansi_when_not_a_tty(proof_run):
    """Captured stdout is not a TTY (and NO_COLOR is set): zero ANSI codes."""
    assert "\x1b[" not in proof_run.stdout


# ------------------------------------------------- middleware_anthropic.py


def test_middleware_exits_zero(middleware_run):
    assert middleware_run.returncode == 0, (
        middleware_run.stdout + middleware_run.stderr
    )


def test_middleware_removes_hallucinations_with_markers(middleware_run):
    out = middleware_run.stdout
    assert REMOVAL_MARKERS[AtomType.NUMBER] in out
    assert REMOVAL_MARKERS[AtomType.REFERENCE] in out
    # before/after is shown: the raw completion still contains the fakes…
    assert "[before]" in out and "[after]" in out
    # …and the verified line keeps the grounded SKU while dropping the fakes.
    after = out.split("[after]", 1)[1]
    assert "HN-2200-P" in after
    assert "HN-9999-Q" not in after and "299.90" not in after


def test_middleware_never_imports_anthropic(middleware_run):
    """The integration is shown in comments only — no anthropic import, so
    the demo runs with zero extra dependencies and zero network."""
    source = (REPO_ROOT / "examples" / "middleware_anthropic.py").read_text(
        encoding="utf-8"
    )
    real_imports = [
        line
        for line in source.splitlines()
        if line.startswith(("import anthropic", "from anthropic"))
    ]
    assert real_imports == []


# ------------------------------------------------------- closed_domain.py


@pytest.fixture(scope="module")
def closed_domain_run() -> subprocess.CompletedProcess[str]:
    """closed_domain.py executed once. Part A (the guarantee) always runs;
    Part B is skipped when no local Ollama is reachable, so CI stays offline."""
    return _run_example("closed_domain.py")


def test_closed_domain_guarantee_holds(closed_domain_run):
    assert closed_domain_run.returncode == 0, (
        closed_domain_run.stdout + closed_domain_run.stderr
    )
    assert "guarantee held" in closed_domain_run.stdout


def test_closed_domain_catches_cross_attribution_and_abstains(closed_domain_run):
    # The €1299 cross-attribution must be flagged and the answer refused.
    assert "cross-attribution" in closed_domain_run.stdout
    assert "I can't confirm that from the official catalog." in closed_domain_run.stdout
