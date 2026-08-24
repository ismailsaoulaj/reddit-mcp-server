---
name: code-security-auditor
description: Security audit for this MCP server codebase. Use when reviewing changes before commit/release, when the user asks for a security check, audit, or review of tokens, secrets, SSRF, URL fetching, or input validation. Checks for token leaks, SSRF, and missing input limits.
---

# Code Security Auditor

Audit changed or requested code in three passes. Report findings ordered by
severity (critical / high / medium / low) with `file:line` references and a
concrete fix for each.

## 1. Token & secret leaks

- Search diffs and new code for hardcoded secrets, bearer tokens, client IDs,
  client secrets, refresh tokens, and API keys.
- Verify credentials are only read from environment variables or `{file:}`
- interpolated config (e.g. `~/.tokens/*.token`), never logged, echoed in error
  messages, embedded in URLs/query strings, or written to disk.
- Check `.gitignore` covers token files, `.env`, caches; confirm no secret-bearing
  file is tracked by git (`git ls-files`).
- Ensure log statements and exception messages do not interpolate auth headers,
  request bodies, or Reddit OAuth payloads.
- Flag any secret committed to history even if later removed.

## 2. SSRF

- Every user-supplied URL or hostname reaching an HTTP call must be validated:
  scheme allowlist (`https`), host allowlist limited to legitimate Reddit API
  domains, no raw IP literals, and resolution must reject private/loopback/link-local
  ranges (`127.0.0.0/8`, `10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`,
  `169.254.0.0/16`, `::1`, `fc00::/7`) including after DNS resolution.
- Reject redirects to non-allowlisted hosts or re-validate per hop.
- Flag string concatenation of user input into URLs; require encoding via
  parameterized query builders.

## 3. Input limits

- All tool arguments accepted by MCP tools must have: maximum length bounds,
  regex/format validation where applicable, and numeric range checks
  (e.g. pagination limits capped).
- HTTP responses must enforce size caps (never stream unbounded bodies into memory).
- Timeouts on all outbound requests; no unbounded retries without backoff and a cap.
- ReDoS: reject pathological regexes applied to user input.

## Output

End with a verdict: PASS, PASS WITH NOTES, or FAIL (blocking findings listed).
Do not fix anything unless explicitly asked — report first.
