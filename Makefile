.PHONY: setup test synthetic ingest-local ingest-fast panel-local vintage-local transitions-local run-local

PYTHON ?= python

setup:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

synthetic:
	$(PYTHON) -m src.cli synthetic --env local

ingest-local:
	$(PYTHON) -m src.cli ingest --env local

ingest-fast:
	$(PYTHON) -m src.cli ingest --env local --sample-fraction 0.10

panel-local:
	$(PYTHON) -m src.cli panel --env local

vintage-local:
	$(PYTHON) -m src.cli vintage --env local

transitions-local:
	$(PYTHON) -m src.cli transitions --env local

run-local: synthetic ingest-local panel-local vintage-local transitions-local
