# Security Policy

## Supported Versions

Currently, only the latest release of the Reddit MCP Server is supported with security updates.

| Version | Supported          |
| ------- | ------------------ |
| 0.5.x   | :white_check_mark: |
| 0.4.x   | :white_check_mark: |
| < 0.4   | :x:                |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Use [GitHub Security Advisories](https://github.com/ismailsaoulaj/reddit-mcp-server/security/advisories/new) to report privately. We will acknowledge your report within 48 hours and aim to release a patch within 7 days for confirmed issues.

When reporting, please include:
- A description of the vulnerability and its potential impact
- Steps to reproduce or a proof-of-concept
- Any suggested mitigations, if known

## Scope

The following are in scope for vulnerability reports:

- Authentication bypass or credential leakage (e.g. OAuth tokens, session cookies, RSS feed tokens)
- Server-Side Request Forgery (SSRF) via user-controlled URLs (e.g. `REDDIT_SAVED_RSS_URL`)
- Denial of service via unbounded resource consumption in the rate limiter or concurrency semaphore
- Secrets exposed through logs or error messages

The following are **out of scope**: bugs in third-party dependencies (report those upstream), Reddit API behavior outside our control, and issues that require physical access to the machine.

## Secret Scanning

This repository uses GitHub's native Secret Scanning. Never commit API keys, Reddit credentials, session cookies, or RSS feed tokens. If a secret is accidentally pushed, consider it compromised immediately and revoke it — GitHub's notification may not arrive in time.
