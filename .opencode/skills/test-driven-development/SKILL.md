---
name: test-driven-development
description: Mandatory quality gate before delivering any code change in this repository. Use whenever code was created or edited, before presenting work as done, and when asked to verify, test, lint, or check the project. Runs uv run ruff and uv run pytest and blocks delivery on failure.
---

# Test-Driven Development

No code is considered delivered until both checks pass. Run them from the
repository root:

```bash
uv run ruff check .
uv run pytest
```

## Workflow

1. After every code change (source, tests, or config affecting behavior),
   run ruff first, then pytest. Run both even if the edit looks trivial.
2. If ruff reports issues, fix them and re-run before running tests.
3. If tests fail, diagnose and fix the root cause — never weaken assertions,
   skip tests, or mark tests as expected-to-fail just to go green.
4. Re-run both commands after fixes. Repeat until both pass cleanly.
5. Only then present the change as complete, stating that ruff and pytest pass.

## Rules

- New functionality requires a corresponding test. If the change lacks coverage,
  add one as part of the task rather than shipping untested code.
- Do not use `--no-cov`, `-x` shortcuts to mask failures, or narrowed test
  selections as the final verification run — final runs cover the full suite.
- If `uv` is unavailable or the environment is broken, report that instead of
  declaring the work done.
