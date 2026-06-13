# Contributing to VeriGate

Thanks for looking. VeriGate has an unusual contribution that is worth more
than most code: **a lie it failed to catch, or a true answer it wrongly
flagged.** See the issue templates.

## The bar (non-negotiable, this is the whole product)

- **100% deterministic.** No LLM, no network, no wall-clock or unseeded
  randomness in any computed output. Same corpus + same answer → byte-identical
  report. There is a test for this; it must stay true.
- **No swallowed errors.** No bare `except` and no `except Exception`
  (enforced by `ruff` rule `BLE`). Catch specific exceptions.
- **Tests come with the code.** Every extractor, verdict and fix is born with
  its tests. The suite is deterministic and offline.
- **Honesty over marketing.** If a change makes the product look like it does
  more than it does, it will be rejected. Document new limitations in
  `DECISIONS.md`.

## Dev setup

```bash
make install          # creates .venv, installs with [api,ingest,dev]
make verify           # ruff + full test suite + quick bench (the CI gate)
```

If `import verigate` fails in the venv (a known macOS `.pth` quirk), prefix
commands with `PYTHONPATH=src` — the Makefile already does this.

Run a single test file:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_engine.py -q
```

## Before opening a pull request

1. `make verify` is green (lint + all tests + quick bench gates: ≥95%
   detection, ≤2% false positives).
2. New behavior has tests; new trade-offs are recorded in `DECISIONS.md`.
3. Commits are atomic with clear messages.

## License of contributions

VeriGate is licensed under **FSL-1.1-Apache-2.0** (see `LICENSE`). By
submitting a contribution, you agree it is licensed under the same terms. In
short: anyone may read, self-host and modify VeriGate; nobody may offer it as a
competing commercial product; and the whole thing becomes Apache-2.0 two years
after each release.
