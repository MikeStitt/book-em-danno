# Makefile — quality gate for the ADOS-on-Ollama toolkit.
# Verbs: check (lint + format + tests), lint, fmt, fmt-fix, test, develop, help.

SHELL := bash

# All shell scripts (*.sh) plus PATH-able tools (executable, no extension).
SH_FILES := $(shell find scripts tools -type f \( -name '*.sh' -o -perm -u+x \) 2>/dev/null | sort -u)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

.PHONY: check
check: lint fmt test cli-reference-check ## Run the full gate: lint + format + tests + docs

.PHONY: lint
lint: ## ShellCheck all shell scripts
	@command -v shellcheck >/dev/null || { echo "shellcheck missing — run scripts/setup-dev.sh"; exit 1; }
	@if [ -n "$(SH_FILES)" ]; then shellcheck -x -P . $(SH_FILES); else echo "lint: no shell files yet"; fi

.PHONY: fmt
fmt: ## Check formatting (shfmt -d)
	@command -v shfmt >/dev/null || { echo "shfmt missing — run scripts/setup-dev.sh"; exit 1; }
	@if [ -n "$(SH_FILES)" ]; then shfmt -i 2 -ci -bn -d $(SH_FILES); else echo "fmt: no shell files yet"; fi

.PHONY: fmt-fix
fmt-fix: ## Apply formatting (shfmt -w)
	@if [ -n "$(SH_FILES)" ]; then shfmt -i 2 -ci -bn -w $(SH_FILES); fi

.PHONY: test
test: ## Run bats test suites
	@scripts/test-all.sh

.PHONY: cli-reference
cli-reference: ## Regenerate docs/cli-reference.md from each tool's --help
	@scripts/gen-cli-reference.sh

.PHONY: cli-reference-check
cli-reference-check: ## Fail if docs/cli-reference.md is out of date
	@scripts/gen-cli-reference.sh --check

.PHONY: develop
develop: ## Install dev dependencies via Homebrew
	@scripts/setup-dev.sh

.PHONY: py-check
py-check: ## Python gate (book-em-danno conversion): ruff + mypy + pytest via uv
	@command -v uv >/dev/null || { echo "uv missing — see https://docs.astral.sh/uv/"; exit 1; }
	@uv run ruff check .
	@uv run ruff format --check .
	@uv run mypy
	@uv run pytest -q
