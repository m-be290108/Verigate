"""VeriGate — deterministic verification layer for generative AI answers.

No LLM, no network, on-premise only. Verifiable atoms (references, figures,
quotes, glossary entities) are checked against a trusted corpus; false atoms
are removed with a visible marker; unverifiable prose is flagged, never
blessed.
"""

from verigate.types import (
    Atom,
    AtomResult,
    AtomStatus,
    AtomType,
    Report,
    Verdict,
)

__version__ = "0.1.0"

__all__ = [
    "Atom",
    "AtomResult",
    "AtomStatus",
    "AtomType",
    "Report",
    "Verdict",
    "Gate",
    "__version__",
]


def __getattr__(name: str):
    # Lazy import: keeps `import verigate` working before optional modules
    # land, and avoids importing sqlite3/yaml machinery for type-only users.
    if name == "Gate":
        from verigate.sdk import Gate

        return Gate
    raise AttributeError(f"module 'verigate' has no attribute {name!r}")
