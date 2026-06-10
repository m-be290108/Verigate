"""Reference extractor — pack-driven identifier atoms (``AtomType.REFERENCE``).

References are recognized in three layers, all 100% deterministic:

(a) explicit ``[REF: …]`` tags — always an atom, whatever the inner text
    (the answer author asserted it is a reference; ``pack="ref_tag"``);
(b) bracketed ``[…]`` / parenthesized ``(…)`` segments whose stripped inner
    text *fullmatches* one of the loaded pack patterns (first pack/pattern
    in registration order wins — deterministic);
(c) bare pack-pattern matches in prose.

Atom spans always include the surrounding delimiters (D-001) so that
removal-by-span leaves no dangling syntax. Overlaps between layers are
resolved by :func:`verigate.extract.base.dedupe_overlapping` (longest span
wins; on ties, earliest in list order).

Patterns come from YAML packs. All pack regexes are compiled with
``re.IGNORECASE`` uniformly; a pattern that needs case sensitivity scopes it
with an inline ``(?-i:…)`` group (see ``packs/generic.yaml``). When a pattern
defines a ``(?P<ref>…)`` named group, the canonical key is computed from that
group while the atom span still covers the whole match (e.g. a Cass. case-law
citation is keyed on the pourvoi number, a ``SKU AP-3000-X`` mention on the
bare code).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

import yaml

from verigate.canonical import canonical_ref
from verigate.extract.base import dedupe_overlapping
from verigate.types import Atom, AtomType


class PackError(ValueError):
    """A reference pack could not be loaded or parsed."""


@dataclass(frozen=True)
class PackPattern:
    """One compiled pattern of a pack."""

    id: str
    regex: re.Pattern[str]


@dataclass(frozen=True)
class Pack:
    """A named collection of reference patterns loaded from YAML."""

    name: str
    description: str
    patterns: tuple[PackPattern, ...]


def builtin_pack_names() -> list[str]:
    """Sorted names of the built-in packs shipped under ``extract/packs``."""
    packs_dir = files("verigate.extract") / "packs"
    return sorted(
        entry.name[: -len(".yaml")]
        for entry in packs_dir.iterdir()
        if entry.name.endswith(".yaml")
    )


def load_pack(name_or_path: str | Path) -> Pack:
    """Load a pack from a built-in name or from a YAML file path.

    A `Path` instance, a string containing ``/``, or a ``.yaml``/``.yml``
    suffix is treated as a filesystem path; anything else as a built-in pack
    name resolved via ``importlib.resources`` (the ``packs`` directory has no
    ``__init__.py``, so it is resolved from the parent package). Raises
    :class:`PackError` on any failure.
    """
    as_str = str(name_or_path)
    if isinstance(name_or_path, Path) or "/" in as_str or as_str.endswith((".yaml", ".yml")):
        path = Path(name_or_path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PackError(f"pack file not readable: {path}") from exc
        return _parse_pack(text, source=str(path))
    available = builtin_pack_names()
    if as_str not in available:
        raise PackError(
            f"unknown builtin pack {as_str!r}; available: {', '.join(available)}"
        )
    resource = files("verigate.extract") / "packs" / f"{as_str}.yaml"
    return _parse_pack(resource.read_text(encoding="utf-8"), source=as_str)


def _parse_pack(text: str, source: str) -> Pack:
    """Parse and validate YAML pack content; compile patterns (IGNORECASE)."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PackError(f"invalid YAML in pack {source!r}: {exc}") from exc
    if not isinstance(data, dict):
        raise PackError(f"pack {source!r} must be a YAML mapping")
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise PackError(f"pack {source!r} is missing a non-empty 'name'")
    raw_patterns = data.get("patterns")
    if not isinstance(raw_patterns, list) or not raw_patterns:
        raise PackError(f"pack {name!r} ({source}) must define a non-empty 'patterns' list")
    patterns: list[PackPattern] = []
    for i, entry in enumerate(raw_patterns):
        if not isinstance(entry, dict) or "id" not in entry or "regex" not in entry:
            raise PackError(
                f"pack {name!r} ({source}): pattern #{i} must be a mapping "
                "with 'id' and 'regex' keys"
            )
        pattern_id = str(entry["id"])
        try:
            compiled = re.compile(str(entry["regex"]), re.IGNORECASE)
        except re.error as exc:
            raise PackError(
                f"pack {name!r}: invalid regex for pattern {pattern_id!r}: {exc}"
            ) from exc
        patterns.append(PackPattern(id=pattern_id, regex=compiled))
    return Pack(
        name=name,
        description=str(data.get("description", "")),
        patterns=tuple(patterns),
    )


#: Layer (a): explicit reference tags — always an atom, span incl. brackets.
_REF_TAG_RE = re.compile(r"\[REF:\s*([^\]]+)\]", re.IGNORECASE)

#: Layer (b): delimited candidates — atoms only if the inner text fullmatches
#: a pack pattern. Spans include the delimiters (D-001).
_BRACKETED_RE = re.compile(r"\[([^\[\]\n]{1,80})\]")
_PAREN_RE = re.compile(r"\(([^()\n]{1,80})\)")


class ReferenceExtractor:
    """Extracts REFERENCE atoms using the three layers documented above.

    Pack list order is the registration order: it decides which pattern wins
    a layer-(b) fullmatch and breaks equal-length ties in dedupe.
    """

    name = "references"

    def __init__(self, packs: list[Pack]) -> None:
        self.packs = tuple(packs)

    def extract(self, text: str) -> list[Atom]:
        atoms: list[Atom] = []
        atoms.extend(self._ref_tag_atoms(text))
        atoms.extend(self._delimited_atoms(text))
        atoms.extend(self._bare_atoms(text))
        return dedupe_overlapping(atoms)

    def _ref_tag_atoms(self, text: str) -> list[Atom]:
        """Layer (a): every ``[REF: …]`` tag becomes an atom unconditionally."""
        return [
            Atom(
                type=AtomType.REFERENCE,
                raw=m.group(0),
                canonical=canonical_ref(m.group(1).strip()),
                start=m.start(),
                end=m.end(),
                pack="ref_tag",
            )
            for m in _REF_TAG_RE.finditer(text)
        ]

    def _delimited_atoms(self, text: str) -> list[Atom]:
        """Layer (b): ``[inner]`` / ``(inner)`` where inner fullmatches a pack."""
        atoms: list[Atom] = []
        for delim_re in (_BRACKETED_RE, _PAREN_RE):
            for m in delim_re.finditer(text):
                inner = m.group(1).strip()
                if not inner:
                    continue
                hit = self._fullmatch_pack(inner)
                if hit is None:
                    continue
                pack, pattern, fm = hit
                key = fm.groupdict().get("ref") or fm.group(0)
                atoms.append(
                    Atom(
                        type=AtomType.REFERENCE,
                        raw=m.group(0),
                        canonical=canonical_ref(key),
                        start=m.start(),
                        end=m.end(),
                        pack=f"{pack.name}:{pattern.id}",
                    )
                )
        return atoms

    def _bare_atoms(self, text: str) -> list[Atom]:
        """Layer (c): bare pack-pattern matches in prose."""
        atoms: list[Atom] = []
        for pack in self.packs:
            for pattern in pack.patterns:
                for m in pattern.regex.finditer(text):
                    if m.start() == m.end():  # zero-width: never an atom
                        continue
                    key = m.groupdict().get("ref") or m.group(0)
                    atoms.append(
                        Atom(
                            type=AtomType.REFERENCE,
                            raw=m.group(0),
                            canonical=canonical_ref(key),
                            start=m.start(),
                            end=m.end(),
                            pack=f"{pack.name}:{pattern.id}",
                        )
                    )
        return atoms

    def _fullmatch_pack(
        self, inner: str
    ) -> tuple[Pack, PackPattern, re.Match[str]] | None:
        """First (pack, pattern) whose regex fullmatches `inner`, if any."""
        for pack in self.packs:
            for pattern in pack.patterns:
                fm = pattern.regex.fullmatch(inner)
                if fm is not None:
                    return pack, pattern, fm
        return None


def extract_references(text: str, pack_names: Sequence[str] | None = None) -> list[Atom]:
    """Extract REFERENCE atoms from `text` using built-in packs.

    `pack_names` defaults to all built-in packs in sorted name order; each
    entry is resolved through :func:`load_pack` (so file paths work too).
    """
    names = builtin_pack_names() if pack_names is None else list(pack_names)
    extractor = ReferenceExtractor([load_pack(n) for n in names])
    return extractor.extract(text)
