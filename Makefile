PYTHON ?= python3.11
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

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
