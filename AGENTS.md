# AGENTS.md

## Commands

```bash
uv sync --locked --extra dev     # install (Python 3.11+, src layout)
uv run ruff check .              # lint
uv run ruff format --check .     # format check — CI runs this separately; a passing `ruff check` does not imply it
uv run pytest tests/             # full suite
uv run pytest tests/test_tools.py -k <name>   # single test
```

CI gate = lint + format-check + tests. Run all three before delivering code.

## Architecture

Layered, dependency-inward (`domain` ← `application` ← `infrastructure`/`interface`):

- `src/reddit_mcp/domain/` — Pydantic models, enrichment logic
- `src/reddit_mcp/application/tools.py` — MCP tool definitions + `DependencyContainer`
- `src/reddit_mcp/interface/server.py` — FastMCP server; transport chosen via `--transport stdio|sse|http` in `main.py`
- `src/reddit_mcp/infrastructure/` — Reddit clients implementing the cascading fallback: OAuth → session cookie → browser-impersonated JSON (curl_cffi) → Arctic Shift → DDG

Server works zero-config; credentials are optional env vars (`REDDIT_CLIENT_ID/SECRET`, `REDDIT_SESSION_COOKIE`, `REDDIT_SAVED_RSS_URL`) loaded by pydantic-settings from `.env`. See `.env.example`.

## Gotchas

- **stdout is JSON-RPC in stdio mode.** All logging must go to stderr when `--transport stdio` (handled by `setup_logging`). Never `print()` to stdout.
- Anti-ban shields are load-bearing: token-bucket rate limiter, global semaphore (`REDDIT_MAX_CONCURRENCY`), singleflight coalescing. New outbound HTTP calls must route through the shared HTTP client and respect these.
- Tests use explicit `@pytest.mark.asyncio` markers; there is no conftest.py and no pytest config section — don't rely on asyncio auto-mode.

## Git workflow

- **Never push directly to `main`** — even when a push would succeed (admin bypass). Always create a feature branch, open a PR, let CI pass, then merge.

## Releases

- Version lives only in `pyproject.toml`; keep `CHANGELOG.md` (Keep a Changelog format) in sync on bumps.
- **Bumping the version invalidates `uv.lock`** (it pins the project's own version) — always run `uv lock` after a bump, or CI's `uv sync --locked` fails.
- Publishing is automated: pushing a GitHub release triggers build + PyPI trusted publish (`.github/workflows/release.yml`). Don't publish manually.
- Commit style: Conventional Commits (`feat`, `fix`, …); breaking changes need `!` and a `BREAKING CHANGE:` footer.

## Local opencode setup

- Skills live in `.opencode/skills/` (security audit, TDD gate with ruff+pytest, release workflow).
- `opencode.json` holds project MCP servers and permissions; GitHub MCP auth reads `{file:~/.tokens/reddit_mcp.token}`.
