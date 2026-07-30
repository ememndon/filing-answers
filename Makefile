.PHONY: help install test lint format check evaluate degraded ask serve docker clean

# Passed through to the gate, so the demonstration is one word on the
# command line rather than an edit: `make evaluate PASSAGES=2`.
MODEL     ?=
THRESHOLD ?=
PASSAGES  ?=
TICKER    ?= AAPL
Q         ?= What were total net sales in fiscal 2025?

GATE_ARGS := $(if $(MODEL),--model $(MODEL)) \
             $(if $(THRESHOLD),--threshold $(THRESHOLD)) \
             $(if $(PASSAGES),--passages $(PASSAGES))

help:
	@echo "  make test        run the unit tests"
	@echo "  make check       tests and linter, as CI runs them"
	@echo "  make evaluate    put 48 known questions to it and decide whether it may ship"
	@echo "  make degraded    the same, with the model shown 2 passages instead of 8"
	@echo "  make ask         one question: make ask TICKER=BLK Q='What was total revenue in 2025?'"
	@echo "  make serve       run the web service on :8000"

install:
	python -m venv .venv
	.venv/bin/pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	ruff format --check .

format:
	ruff format .
	ruff check --fix .

check: lint test

# The release gate. Exits non-zero when the answers are not good enough,
# which is what makes it a gate rather than a report.
evaluate:
	python -m filing_answers evaluate $(GATE_ARGS)

# The same gate against a deliberately worse configuration. Fewer
# passages is a shorter prompt and a smaller bill, it breaks no test,
# and it costs sixteen right answers.
degraded:
	python -m filing_answers evaluate --passages 2

ask:
	python -m filing_answers ask $(TICKER) "$(Q)"

serve:
	uvicorn filing_answers.api:app --host 0.0.0.0 --port 8000

docker:
	docker build -t filing-answers .

clean:
	rm -rf .pytest_cache .ruff_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
