.PHONY: test

test:
	ruff check .
	python3.11 -m compileall src infra tests
	python -m pytest
