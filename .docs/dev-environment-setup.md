# Developer Environment Setup — Linux Docker Sandbox

This document describes how to set up the development environment for `danno` in a
Linux Docker sandbox bash shell, matching the GitHub Actions workflow.

## Prerequisites

- `uv` installed (see [Installation](https://docs.astral.sh/uv/#installation))
- Python >= 3.13

## Setup Steps

```bash
# Clone and navigate to the project
cd /path/to/book-em-danno

# Install Python dependencies into a virtual environment (matches CI)
uv sync --locked --all-extras --dev

# Verify tools are available via uv run (these come from pyproject.toml dev deps)
uv run ruff --version
uv run mypy --version
uv run pytest --version

# ninja comes from the system package manager (not in pyproject.toml)
# Install on Linux:
sudo apt-get update -qq && sudo apt-get install -y ninja-build

# Verify ninja is available both ways
ninja --version
uv run ninja --version  # should also work via uv if installed

# Run the quality gate
ninja check
# or explicitly for each check:
ninja lint      # ruff check .
ninja fmt       # ruff format --check .
ninja typecheck # mypy
ninja test      # pytest -q -m "not slow"
```

## Toolchain

| Tool | Source | Command |
|------|--------|---------|
| Package manager | System/uv | `uv` |
| Linting | Project deps (via `uv sync`) | `uv run ruff check .` |
| Formatting | Project deps (via `uv sync`) | `uv run ruff format --check .` |
| Type checking | Project deps (via `uv sync`) | `uv run mypy` |
| Testing | Project deps (via `uv sync`) | `uv run pytest -q -m "not slow"` |
| Build system | System package (`ninja-build`) | `ninja check` |

## CI Alignment

The GitHub Actions workflow (`.github/workflows/check.yml`) runs these steps:

1. **Install uv** via `astral-sh/setup-uv@v5`
2. **Install ninja**:
   - macOS: `brew install ninja`
   - Linux: `sudo apt-get install -y ninja-build`
3. **Sync dependencies**: `uv sync --locked --all-extras --dev`
4. **Run gate**: `ninja check`

This document's steps mirror CI to ensure local development matches production verification.

## macOS Developer Notes

If you're developing on a Mac, use the same setup but replace the ninja install:

```bash
# On macOS:
brew install ninja
```

All other commands (`uv sync`, `ninja check`, etc.) are identical.