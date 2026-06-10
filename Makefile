PYTHON ?= python3.11
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

# Belt-and-braces against macOS UF_HIDDEN on .pth files (site.py skips hidden
# .pth, silently breaking the editable install — seen 2026-06-10).
export PYTHONPATH := src

.PHONY: venv install test lint bench-quick verify clean

venv:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv
	$(PIP) install -e ".[api,ingest,dev]"

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check src tests bench examples

bench-quick:
	$(PY) -m bench.run --quick

verify: lint test bench-quick
	@echo "── verify: ALL GREEN ──"

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache build dist *.egg-info
