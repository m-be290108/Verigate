"""Shared fixtures for the VeriGate test suite.

Every test must be deterministic, offline (0 network — see
test_no_network.py) and LLM-free.
"""

from __future__ import annotations

import socket

import pytest


class NetworkCalledError(AssertionError):
    """Raised if any code attempts an outbound connection during tests."""


def _blocked_connect(*args, **kwargs):
    raise NetworkCalledError(f"socket.connect attempted during test: {args!r}")


@pytest.fixture
def no_network(monkeypatch):
    """Block socket connections for the duration of a test (Beaume pattern:
    lucie test_no_network.py). Use on any test exercising a full pipeline."""
    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked_connect)
    yield


@pytest.fixture
def sample_corpus_dir(tmp_path):
    """A tiny trusted-corpus folder: product catalog + policy + price list."""
    d = tmp_path / "corpus_src"
    d.mkdir()
    (d / "catalog.md").write_text(
        "# Product catalog\n\n"
        "## AquaPump 3000 (SKU AP-3000-X)\n"
        "Submersible pump, 230 V, 550 W. Price: €249.99.\n"
        'Warranty statement: "This product is covered for 24 months '
        'from the date of purchase."\n\n'
        "## HydroFilter Mini (SKU HF-MINI-2)\n"
        "Inline filter, max flow 12 L/min. Price: €39.50.\n",
        encoding="utf-8",
    )
    (d / "policy.txt").write_text(
        "Returns are accepted within 30 days. Refunds are processed in "
        "5 business days. Support: https://intranet.example/support.\n",
        encoding="utf-8",
    )
    (d / "products.csv").write_text(
        "sku,name,price_eur\nAP-3000-X,AquaPump 3000,249.99\n"
        "HF-MINI-2,HydroFilter Mini,39.50\n",
        encoding="utf-8",
    )
    return d
