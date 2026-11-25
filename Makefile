.PHONY: install hooks lint fmt test featuremap featuremap-baseline secrets dependencies check lock archive

PYTHON ?= python3.12
VENV := .venv/bin
LOCK_FLAGS := --universal --python-version 3.12 --generate-hashes --no-python-downloads

install:
	$(PYTHON) -m venv .venv
	$(VENV)/pip install --require-hashes -r requirements-dev.lock

hooks:
	git config core.hooksPath tools/hooks

lint:
	tools/privacy.sh index
	$(VENV)/ruff check .
	$(VENV)/ruff format --check .

fmt:
	$(VENV)/ruff format .

test:
	$(VENV)/python -m pytest

featuremap:
	node tools/featuremap/featuremap.mjs check $(if $(BASE),--base $(BASE))

featuremap-baseline:
	node tools/featuremap/featuremap.mjs baseline

secrets:
	tools/security.sh secrets

dependencies:
	tools/security.sh dependencies

check: lint test featuremap

archive:
	rm -rf dist
	$(VENV)/python -m release.build_archive --version "$$VERSION" --out dist
	$(VENV)/python -m release.launch_proof dist/*.tar.gz
	$(VENV)/python -m release.host_acceptance dist/*.tar.gz

lock:
	uv pip compile requirements.txt $(LOCK_FLAGS) -o requirements.lock
	uv pip compile requirements-dev.txt $(LOCK_FLAGS) -o requirements-dev.lock
