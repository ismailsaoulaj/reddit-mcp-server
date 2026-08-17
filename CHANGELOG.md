# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Resilient Fallbacks & Smart Filtering

This release hardens the server's two headline promises: **graceful degradation**
(fallbacks now trigger on *failed* credentials, not just missing ones, and provider
outages are never reported as "no results") and **LLM-safe filtering** (fresh threads
are no longer silently emptied, legitimate users are no longer mistaken for bots).

The changes came out of a systematic adversarial code review of the entire codebase;
each entry below traces to a filed and verified issue. The test suite grew from 22
to 82 tests, and CI now runs locked, reproducible resolution.

### Fixed

**Fallback & degradation**

- Fallback paths now trigger on failed credentials (401/429/network errors), not
  only missing ones — a typo'd client secret previously produced a *worse*
  experience than no configuration at all.
- Provider outages (DuckDuckGo, Arctic Shift) are distinguishable from genuine
  "no results": provider errors surface as `status: "degraded"` responses with a
  message, instead of empty successes the LLM could mistake for a dead topic.
- Responses served by the fallback are tagged `data_source: "arctic_shift"` with
  an archive-lag note, and only when Arctic Shift actually served the data.
- The degraded-response message no longer claims both providers are down when
  only one fallback step failed.

**OAuth & HTTP resilience**

- Stale OAuth tokens self-heal: a mid-flight 401 invalidates the cached token and
  retries once with a fresh one; long-running servers no longer stay broken until
  restart. The token-refresh retry has its own allowance and does not consume the
  general retry budget.
- Bearer tokens are only attached to Reddit API hosts. The shared HTTP client can
  no longer leak a Reddit OAuth token to third-party hosts (Arctic Shift), and a
  third-party 401 can no longer invalidate the Reddit token.
- The retry flow is bounded by an aggregate 14-second deadline (previously, retry
  sleeps and slow responses could exceed the tool timeouts that were supposed to
  contain them).
- `Retry-After` is honored at most 5 seconds; a hostile or misconfigured header
  can no longer stall a tool call for minutes.

**Comment & post quality**

- The quality filter is thread-age aware: comments scoring 1 survive on threads
  ≤ 2 days old, so `analyze_niche_trends(rising)` → `extract_public_opinion`
  no longer silently returns zero comments on fresh threads.
- Bot detection matches exact names (`AutoModerator`) and `_bot`/`-bot`/`bot_`
  suffixes instead of the `bot` substring — humans like u/robotics_fan are no
  longer filtered out.
- Posts with missing timestamps report `age_in_days: null` (unknown) instead of
  `0` ("posted today") — no more false freshness fed to the LLM.
- Comment deep-links handle `r/name`, `/r/name`, and whitespace-padded subreddit
  forms (previously `/r/python` produced `r//python` URLs).

**Lifecycle**

- `limit` / `max_comments` tool parameters are schema-bounded (1–100), enforced
  client-side by the generated JSON schema.
- Dependency initialization is transactional: a failed constructor can no longer
  strand a half-initialized container, and the partially constructed HTTP client
  is closed on failure.
- Server shutdown closes initialized clients and always clears the container,
  even when closing fails; failed startups no longer construct the dependency
  graph just to close it.

### Changed

- Pure mapping helpers (`truncate_text`, `calculate_age_in_days`,
  `format_timestamp`, `build_comment_url`) moved to `domain/enrichment.py`;
  the infrastructure layer no longer imports from the application layer,
  matching the documented 4-layer architecture.
- `fastmcp` dependency floor raised to `>=3.4` (the previously declared
  `>=0.4.1` was never a runnable configuration for this code).
- CI installs from `uv.lock` (`uv sync --locked`) via `setup-uv`, testing exactly
  the pinned dependency set; httpx per-request timeout tightened to 5s with
  `max_retries` defaulting to 2 so the resilience budget fits inside tool
  timeouts.

### Verified

- Test suite: 22 → 82 tests, all mocked (no network), sub-second runtime.
- `ruff check` / `ruff format --check` clean; `uv lock --check` consistent.
- End-to-end probes through FastMCP: 401 → fallback fires; provider outage →
  `status: "degraded"`; schema bounds enforced; aggregate deadline cancels
  in-flight retries.

## [0.1.0] - Initial Release: AI-Native Reddit MCP Server

- Four LLM-oriented tools: `search_knowledge`, `explore_reddit_discussions`,
  `extract_public_opinion`, `analyze_niche_trends`.
- Zero-config operation with DuckDuckGo + Arctic Shift fallback.
- OAuth 2.0 client-credentials flow for the official Reddit API.
- Strict stderr logging to keep the STDIO JSON-RPC stream clean.
- 4-layer architecture (domain / infrastructure / application / interface).

[0.2.0]: https://github.com/ismailsaoulaj/reddit-mcp-server/releases/tag/v0.2.0
[0.1.0]: https://github.com/ismailsaoulaj/reddit-mcp-server/releases/tag/v0.1.0
