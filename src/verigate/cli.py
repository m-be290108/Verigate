"""Command-line interface for VeriGate (stdlib argparse only).

``verigate <command>`` wraps the SDK facade (:class:`verigate.sdk.Gate`)
for shell pipelines and CI gates:

- ``ingest``        build/update the trusted corpus database from a folder;
- ``verify``        verify one answer (argument, or stdin when absent/'-');
- ``verify-corpus`` run the corpus integrity lock;
- ``serve``         run the localhost HTTP API (optional ``[api]`` extra);
- ``audit-export``  export the audit trail as CSV (chain verified first).

Exit codes (D-010 — branch on the verdict without parsing output):

- ``verify``: 0 VERIFIED, 1 CORRECTED, 2 INSUFFICIENT, 3 UNVERIFIABLE;
  4 on an operational error (missing database, unreadable context file) so
  an infrastructure failure can never be mistaken for a CORRECTED verdict.
- ``ingest`` / ``verify-corpus`` / ``serve`` / ``audit-export``: 0 success,
  1 error.

On-premise contract: ``serve`` binds loopback only and REFUSES any
non-loopback ``--host`` with exit 1, *before* importing the API stack.
VeriGate never exposes the API beyond localhost by itself — rebinding is a
deliberate customer deployment decision (their reverse proxy, their terms),
never a CLI flag typo (D-010).
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

from verigate.audit.trail import AuditIntegrityError, AuditTrail
from verigate.corpus import CorpusDB
from verigate.sdk import Gate
from verigate.types import AtomStatus, Report, Verdict

#: ``verify`` exit codes mirror the verdict gradation (D-010).
_VERDICT_EXIT: dict[Verdict, int] = {
    Verdict.VERIFIED: 0,
    Verdict.CORRECTED: 1,
    Verdict.INSUFFICIENT: 2,
    Verdict.UNVERIFIABLE: 3,
}

#: ``verify`` operational failure (missing db, unreadable context file) —
#: distinct from every verdict code, so CI gates cannot misread it.
_EXIT_OPERATIONAL = 4

#: Per-atom icons of the human report (the dossier_citations style).
_ICONS: dict[AtomStatus, str] = {
    AtomStatus.VERIFIED: "✅",
    AtomStatus.MISMATCHED: "❌",
    AtomStatus.NOT_FOUND: "❌",
    AtomStatus.UNVERIFIABLE: "➖",
}

_EXIT_CODES_EPILOG = """\
exit codes:
  verify          0 VERIFIED / 1 CORRECTED / 2 INSUFFICIENT / 3 UNVERIFIABLE
                  4 operational error (missing database, unreadable context file)
  other commands  0 success / 1 error
"""


# ---------------------------------------------------------------- helpers


def _fail(message: str) -> int:
    """Print an error line on stderr; return exit code 1."""
    print(f"error: {message}", file=sys.stderr)
    return 1


def _is_loopback(host: str) -> bool:
    """True iff `host` is 'localhost' or a loopback IP literal. Hostnames
    are NOT resolved (no DNS — offline rule); anything unparseable is
    refused, conservatively."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _print_human_report(answer: str, report: Report) -> None:
    """Human-readable report: verdict + score, one icon line per atom, then
    the corrected answer when it differs from the input."""
    print(
        f"Verdict: {report.verdict.value} — score {report.score:.2f} "
        f"({report.n_verified} verified / {report.n_false} false / "
        f"{report.n_unverifiable} unverifiable)"
    )
    for warning in report.warnings:
        print(f"warning: {warning}")
    for result in report.atoms:
        line = (
            f"{_ICONS[result.status]} {result.atom.type.value} "
            f"{result.atom.raw!r} — {result.status.value}"
        )
        if result.matched_source:
            line += f" ({result.matched_source})"
        elif result.detail:
            line += f" — {result.detail}"
        print(line)
    if report.corrected_answer != answer:
        print()
        print("Corrected answer:")
        print(report.corrected_answer)


# ------------------------------------------------------------ subcommands


def _cmd_ingest(args: argparse.Namespace) -> int:
    """Build the corpus; print counts + fingerprint + every skipped file
    with its reason (never silent)."""
    try:
        result = Gate.ingest(
            args.folder, args.db, packs=args.packs, glossary_path=args.glossary
        )
    except ValueError as exc:
        return _fail(str(exc))
    print(f"documents: {result.n_docs}")
    print(f"references: {result.n_refs}")
    print(f"numbers: {result.n_numbers}")
    print(f"entities: {result.n_entities}")
    print(f"fingerprint: {result.fingerprint}")
    for relpath, reason in result.skipped:
        print(f"skipped: {relpath} ({reason})")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    answer = args.answer
    if answer is None or answer == "-":
        answer = sys.stdin.read()

    context: list[str] | None = None
    if args.context_file:
        context = []
        for name in args.context_file:
            try:
                context.append(Path(name).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                print(f"error: context file {name}: {exc}", file=sys.stderr)
                return _EXIT_OPERATIONAL

    try:
        gate = Gate(args.db, audit_db=args.audit_db)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return _EXIT_OPERATIONAL
    try:
        report = gate.verify(answer, context=context)
    finally:
        gate.close()

    if args.json:
        print(report.to_json())
    else:
        _print_human_report(answer, report)
    return _VERDICT_EXIT[report.verdict]


def _cmd_verify_corpus(args: argparse.Namespace) -> int:
    try:
        db = CorpusDB(args.db)
    except FileNotFoundError as exc:
        return _fail(str(exc))
    try:
        ok, errors = db.verify_corpus()
        fingerprint = db.fingerprint()
    finally:
        db.close()
    if ok:
        print(f"OK — fingerprint: {fingerprint}")
        return 0
    for error in errors:
        print(f"error: {error}", file=sys.stderr)
    return 1


def _cmd_serve(args: argparse.Namespace) -> int:
    """Run the HTTP API on localhost. The loopback check runs BEFORE the
    lazy uvicorn/api imports: refusing a public bind must not depend on the
    optional [api] extra being installed."""
    if not _is_loopback(args.host):
        return _fail(
            f"refusing to bind non-loopback host {args.host!r} — VeriGate "
            "serves on localhost only (on-premise contract). Exposing the "
            "API beyond loopback is a deliberate deployment decision "
            "(customer-managed reverse proxy), not a CLI flag."
        )
    # Lazy imports: the CLI must work without the optional [api] extra and
    # before the api module exists at all.
    try:
        import uvicorn

        from verigate.api.app import create_app
    except ImportError as exc:
        return _fail(
            f"the HTTP API requires the optional [api] extra "
            f"(pip install 'verigate[api]'): {exc}"
        )
    try:
        app = create_app(args.db, audit_db=args.audit_db)
    except FileNotFoundError as exc:
        return _fail(str(exc))
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def _cmd_audit_export(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if not db_path.is_file():
        return _fail(f"audit database not found: {db_path}")
    trail = AuditTrail(db_path)
    try:
        content = trail.export_csv(output=args.out)
    except AuditIntegrityError as exc:
        for error in exc.errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        trail.close()
    if args.out is None:
        print(content, end="")
    else:
        print(f"audit trail exported to {args.out}")
    return 0


# ----------------------------------------------------------------- parser


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="verigate",
        description=(
            "Deterministic verification layer for AI answers — "
            "no LLM, offline, on-premise."
        ),
        epilog=_EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    p = sub.add_parser(
        "ingest",
        help="build/update the trusted corpus database from a folder",
    )
    p.add_argument("folder", help="folder of trusted documents")
    p.add_argument("--db", required=True, help="corpus database path (created/updated)")
    p.add_argument(
        "--packs",
        nargs="+",
        default=None,
        metavar="PACK",
        help="reference packs (built-in names or YAML paths); default: all built-ins",
    )
    p.add_argument("--glossary", default=None, help="explicit YAML glossary path")
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser(
        "verify",
        help="verify one answer against the corpus",
        epilog=_EXIT_CODES_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "answer",
        nargs="?",
        default=None,
        help="the answer text; omit or pass '-' to read stdin",
    )
    p.add_argument("--db", required=True, help="corpus database path")
    p.add_argument(
        "--audit-db", default=None, help="journal this verification in an audit trail"
    )
    p.add_argument(
        "--context-file",
        action="append",
        default=None,
        metavar="FILE",
        help="trusted context chunk for this call (repeatable)",
    )
    p.add_argument("--json", action="store_true", help="print the JSON report")
    p.set_defaults(func=_cmd_verify)

    p = sub.add_parser("verify-corpus", help="run the corpus integrity lock")
    p.add_argument("--db", required=True, help="corpus database path")
    p.set_defaults(func=_cmd_verify_corpus)

    p = sub.add_parser("serve", help="run the HTTP API on localhost")
    p.add_argument("--db", required=True, help="corpus database path")
    p.add_argument("--audit-db", default=None, help="audit trail database path")
    p.add_argument("--port", type=int, default=8470, help="TCP port (default 8470)")
    p.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address — loopback only (non-loopback hosts are refused)",
    )
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser(
        "audit-export", help="export the audit trail as CSV (chain verified first)"
    )
    p.add_argument("--db", required=True, help="audit trail database path")
    p.add_argument("--out", default=None, help="write the CSV here instead of stdout")
    p.set_defaults(func=_cmd_audit_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point (``verigate = verigate.cli:main``); returns the exit code."""
    args = _build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
