# Contributing to Reddit MCP Server

Thanks for taking the time to contribute! This document covers everything you need to go from zero to a merged pull request.

## Table of Contents

- [Development Setup](#development-setup)
- [Workflow](#workflow)
- [Pull Request Process](#pull-request-process)
- [Adding a Search Provider](#adding-a-search-provider)
- [Code of Conduct](#code-of-conduct)

## Development Setup

**Requirements:** Python 3.11+, [`uv`](https://docs.astral.sh/uv/) (recommended)

```bash
# 1. Fork the repo on GitHub, then clone your fork
git clone https://github.com/YOUR_USERNAME/reddit-mcp-server.git
cd reddit-mcp-server

# 2. Install all dependencies (including dev tools) from the lockfile
uv sync --locked --extra dev

# 3. Verify everything works
uv run pytest tests/
```

> **No `uv`?** You can use `pip install -e ".[dev]"` instead, but the CI runs against the locked dependency set (`uv.lock`), so results may differ.

Read [`docs/architecture.md`](docs/architecture.md) to understand the 4-layer structure before making changes.

## Workflow

```bash
# 1. Create a focused branch
git checkout -b feature/my-new-feature   # or fix/issue-123

# 2. Make your changes, then check lint and formatting
uv run ruff check .
uv run ruff format .

# 3. Run the full test suite (all tests are mocked — no network required)
uv run pytest tests/

# 4. Commit with a clear message
git commit -m "feat: add X" -m "Fixes #123"

# 5. Push and open a PR
git push origin feature/my-new-feature
```

Commit messages should follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`, `chore:`, etc.).

> **Lockfile rule:** if you bump the version in `pyproject.toml` or change
> dependencies, regenerate the lockfile with `uv lock` before committing.
> `uv.lock` pins the project's own version, and CI runs `uv sync --locked`,
> which fails on a stale lockfile. Never edit `uv.lock` by hand.

## Pull Request Process

1. Fill in the PR template — describe *what* changed and *why*.
2. Link the related issue with `Fixes #NNN` in the description.
3. Ensure all GitHub Actions checks pass (lint, format, tests).
4. A maintainer will review within a few days and may request changes.
5. Once approved, a maintainer will merge your PR.

PRs that add new functionality without tests will not be merged.

## Adding a Search Provider

The search layer uses a Strategy Pattern — adding a new provider takes minutes. See [`src/reddit_mcp/infrastructure/search/providers/README.md`](src/reddit_mcp/infrastructure/search/providers/README.md) for a step-by-step guide.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you agree to uphold its standards. Violations can be reported via GitHub Security Advisories.