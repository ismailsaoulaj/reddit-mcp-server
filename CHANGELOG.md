# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Polish from the adversarial-review backlog (issues #15–#22).

### Added

- New `get_saved_posts` tool: fetches the user's own saved Reddit posts for a
  definable period (`day`/`week`/`month`/`year`/`all`) via their private
  saved-items RSS feed (`REDDIT_SAVED_RSS_URL` — no OAuth required). Entries
  are enriched (`age_in_days`, `created_at_human`, HTML-stripped preview),
  sorted newest-first, quality-filtered like `search_knowledge` (the caller's
  limit applies after filtering), and returned with `data_source:
  "saved_rss"` plus a note that feed metrics (scores, comment counts) are
  unavailable. Saved comments are counted and skipped; the feed URL is
  validated against Reddit hosts, every redirect hop is host-validated before
  any request is sent, and the URL/token is never logged. Unconfigured →
  `warning` with setup instructions; feed failures → `degraded`.
- `extract_public_opinion` supports pagination for deep threads via
  provider-prefixed `page_token` / `next_page_token` cursors. A `reddit:`
  cursor pairs the raw-stream offset (for request sizing) with the last-served
  comment ID, so live re-sorts between requests can neither duplicate served
  comments nor skip unseen ones after the anchor (matching Reddit's own `after`
  semantics); an `arctic:` cursor is an offset into the score-sorted list.
  Tokens are bound to the provider that issued them, capped at 10,000 comments
  deep (a capped page omits the token and says so); short pages return no
  token. The Reddit client clamps its request window to the API's 100-item
  per-request limit, so a continuation deeper than Reddit can serve in one
  window ends as a clean bounded page instead of an error.

### Changed

- Arctic Shift thread fetch oversamples (3×), filters deleted/removed, sorts
  by score, and only then slices to `max_comments` — no more under-filled
  pages that weren't the true top-N.
- `llm_timeout` constructs its fallback from the tool's actual response model
  (no more schema-drifted dict; the model-less dict fallback is now strictly
  JSON-serializable), and `build_meta_context` returns the `MetaContext`
  domain model.
- The `explore_reddit_discussions` fallback message now tells the LLM that
  sort and pagination are unavailable in fallback mode.
- The default `User-Agent` includes a per-install random suffix (persisted in
  the user's state directory — created race-safely via exclusive file
  creation so concurrent first-starts share one ID — so it survives restarts;
  process-stable fallback when the directory is unwritable) so zero-config
  users are no longer a single shared identity to Reddit (set
  `REDDIT_USER_AGENT` for a stable descriptive value; README updated).
- Docker runtime stage runs as `nobody` instead of root.

### Removed

- Stray `pyrefly` pragma removed from `server.py`.

## [0.2.0] - 2026-08-17 - Resilient Fallbacks & Smart Filtering

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

## [0.1.0] - 2026-08-16 - Initial Release: AI-Native Reddit MCP Server

- Four LLM-oriented tools: `search_knowledge`, `explore_reddit_discussions`,
  `extract_public_opinion`, `analyze_niche_trends`.
- Zero-config operation with DuckDuckGo + Arctic Shift fallback.
- OAuth 2.0 client-credentials flow for the official Reddit API.
- Strict stderr logging to keep the STDIO JSON-RPC stream clean.
- 4-layer architecture (domain / infrastructure / application / interface).

[0.2.0]: https://github.com/ismailsaoulaj/reddit-mcp-server/releases/tag/v0.2.0
[0.1.0]: https://github.com/ismailsaoulaj/reddit-mcp-server/releases/tag/v0.1.0
