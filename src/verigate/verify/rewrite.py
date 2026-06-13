"""Answer rewriting — splice false-atom spans out, leave a visible marker.

D-001: the Beaume verifier removed false references by re-running ``re.sub``
on the reference text and only covered bracketed formats (audit finding F3:
prose forms survived in the delivered note while being reported "removed").
VeriGate instead records the exact ``(start, end)`` span of every atom at
extraction time and rewrites the answer by splicing those spans, so coverage
of every format the extractors know is structural, not pattern-by-pattern.

D-005: removal is never silent — each spliced span is replaced by the
visible ``REMOVAL_MARKERS[atom.type]`` marker.

Pure function, no I/O, 100% deterministic.
"""

from __future__ import annotations

from itertools import pairwise

from verigate.types import FALSE_STATUSES, REMOVAL_MARKERS, AtomResult, AtomStatus


def rewrite_answer(
    answer: str, results: list[AtomResult], strip_unverifiable: bool = False
) -> str:
    """Return `answer` with every false atom span replaced by its marker.

    Every ``AtomResult`` whose status is in ``FALSE_STATUSES`` has its
    ``answer[start:end]`` slice replaced by ``REMOVAL_MARKERS[atom.type]``.
    Spans are processed in DESCENDING start order so earlier offsets stay
    valid while splicing.

    ``strict`` mode (D-018) passes ``strip_unverifiable=True``: UNVERIFIABLE
    atoms are also spliced out, so the shown answer contains only grounded
    facts (closed-world). The default is unchanged — only false atoms go.

    The engine guarantees globally non-overlapping spans (dedupe), but this
    is asserted defensively: a ValueError is raised on any overlap, because
    splicing overlapping spans would silently corrupt customer text.
    """
    strip = set(FALSE_STATUSES)
    if strip_unverifiable:
        strip.add(AtomStatus.UNVERIFIABLE)
    spans = sorted(
        (r.atom.start, r.atom.end, r.atom.type)
        for r in results
        if r.status in strip
    )
    for (s1, e1, _t1), (s2, e2, _t2) in pairwise(spans):
        if e1 > s2:
            raise ValueError(
                "overlapping false spans cannot be rewritten safely: "
                f"({s1}, {e1}) overlaps ({s2}, {e2})"
            )
    out = answer
    for start, end, atom_type in reversed(spans):
        out = out[:start] + REMOVAL_MARKERS[atom_type] + out[end:]
    return out
