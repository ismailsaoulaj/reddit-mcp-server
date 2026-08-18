# 🤖 Reddit MCP Server (AI-Native Edition)

[![CI Status](https://github.com/ismailsaoulaj/reddit-mcp-server/actions/workflows/ci.yml/badge.svg)](https://github.com/ismailsaoulaj/reddit-mcp-server/actions)
[![Python Version](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Zero Config](https://img.shields.io/badge/Setup-Zero_Config-success.svg)](#-prerequisites--setup)

A highly resilient, open-source [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server. It empowers AI models (such as Claude and Cursor) to search, fetch, read, and deep-dive into Reddit content with robust rate-limiting recovery and smart comment-filtering.

Built in Python using `FastMCP`, this project adheres to a strict **4-Layer Architecture** designed for high modularity, testability, and painless contributions.

---

## 🗺️ How it Works (Data Flow Sequence)

Here is a visual sequence diagram showing how the AI model interacts with this server, including our **Zero-Config Fallback** system:

```mermaid
sequenceDiagram
    autonumber
    actor AI as AI Assistant (Claude/Cursor)
    participant MCP as FastMCP Server (STDIO)
    participant Tools as Application Tools
    participant Reddit as Reddit API (OAuth)
    participant Fallback as DDG & Arctic Shift

    AI->>MCP: Request (e.g., search_knowledge)
    MCP->>Tools: Route request
    Tools->>Reddit: Attempt Fetch (Resilient HTTP)
    alt Has OAuth Credentials & API Healthy
        Note over Reddit,Tools: Handles 429 (Rate Limits) with Retry-After backoff!
        Reddit-->>Tools: Return Official JSON payload
    else Zero-Config OR Reddit API Fails
        Note over Tools,Fallback: Graceful Degradation Active
        Tools->>Fallback: Execute Search / Fetch Archive
        Fallback-->>Tools: Return Alternative JSON payload
    end
    Tools->>Tools: Refine comments (filter bots & short noise)
    Tools-->>MCP: Map to Domain Models (Pydantic)
    MCP-->>AI: Return clean JSON-RPC Response (stdout-safe)
```

---

## ✨ Features

- 🚀 **Zero-Config Ready:** Works completely out of the box! No Reddit API keys required. If credentials are not provided, it seamlessly falls back to DuckDuckGo and the Arctic Shift archive.
- 🛡️ **Graceful Degradation:** Intelligently switches between the official Reddit API and unauthenticated fallback providers without crashing, ensuring the LLM always gets data.
- 📈 **Resilient HTTP Client:** Built-in exponential backoff and rate-limiting recovery. If Reddit says `429 Too Many Requests`, the server respects the `Retry-After` header and retries automatically.
- 🔍 **Strategic Search:** Integrates a decoupled search provider system (Strategy Pattern) allowing easy addition of custom search engines.
- 🤖 **LLM-Safe Filtering:** Cleans thread payloads by dropping auto-moderators, bot notifications, and low-quality comments, saving precious LLM token costs.
- ⏱️ **Strict LLM Timeout Protection:** Uses decorators to force safe API timeouts, returning clean graceful JSON-RPC fallbacks instead of hanging the AI client.

---

## 🧰 Available Tools

| Tool Name | Purpose | Best Used For |
| :--- | :--- | :--- |
| `search_knowledge` | Broad web search via DuckDuckGo | Finding technical explanations and factual discussions across Reddit. |
| `explore_reddit_discussions` | Discussion search with metrics | Gauging sentiment, upvote consensus, and topic exploration. |
| `extract_public_opinion` | Deep comment tree extraction & filtering | Reading high-quality community opinions with noise & bots removed. |
| `analyze_niche_trends` | Live trending & rising posts tracker | Identifying real-time problems, pain points, or new ideas in a niche. |
| `get_saved_posts` | The user's own saved posts over a time period | Revisiting, summarizing, or triaging bookmarked content (requires the saved-items feed URL). |

---

## ⚙️ Prerequisites & Setup

### Requirements

- Python 3.11 or higher
- Reddit API App credentials (Optional, but recommended for live trending data & better rate limits)

### Quick Start (Local Installation)

1. **Clone and Install:**

```bash
git clone https://github.com/ismailsaoulaj/reddit-mcp-server.git
cd reddit-mcp-server
pip install -e .
```

2. **Configure your environment (Optional):**

To unlock the official Reddit API, create a `.env` file in the root directory:

```env
REDDIT_CLIENT_ID="your_client_id_here"
REDDIT_CLIENT_SECRET="your_client_secret_here"
```

Consider also setting `REDDIT_USER_AGENT` to a descriptive, unique value — Reddit's API guidelines ask for this, even in zero-config mode. If unset, the server generates a default with a random per-install suffix (persisted under your [XDG state directory](https://specifications.freedesktop.org/basedir-spec/latest/) so it stays stable across restarts).

To enable the `get_saved_posts` tool, add your private saved-items feed URL:

```env
REDDIT_SAVED_RSS_URL="https://old.reddit.com/user/YOUR_USERNAME/saved.rss?feed=YOUR_FEED_TOKEN&user=YOUR_USERNAME"
```

While logged in, open `old.reddit.com/saved.rss` and copy the full URL you are redirected to (canonical form: `old.reddit.com/user/<name>/saved.rss?feed=...&user=...`). The `feed` token is a credential for your account — treat it like a password (the server never logs it and rejects non-Reddit hosts). The feed exposes the most recent ~100 saved items; scores and comment counts are not available through it.

---

## 🐳 Docker Installation

A multi-stage Dockerfile is provided for seamless execution.

```bash
docker build -t reddit-mcp-server .
```

> **Note:** If using Docker, replace the `command` in client configs with `docker` and arguments with `run -i --rm reddit-mcp-server`.

---

## 🛠️ Configuration for AI Clients

### 1. Claude Desktop

Edit your configuration file:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "reddit": {
      "command": "hatch",
      "args": [
        "run",
        "reddit-mcp"
      ],
      "cwd": "/absolute/path/to/reddit-mcp-server"
    }
  }
}
```

### 2. Cursor

Go to **Settings > Features > MCP** and add a new command-based server:
- **Type:** command
- **Name:** Reddit
- **Command:** `hatch run reddit-mcp` (using the absolute path to your python/hatch environment)

---

## 🧪 Developer Experience (DX) & Testing

We prioritize high test coverage. We mock all network traffic, ensuring tests run instantly and reliably.

### Run Tests

```bash
# Install development dependencies
pip install -e ".[dev]"

# Execute pytest
pytest tests/
```

### Manual Testing with the MCP Inspector

```bash
npx @modelcontextprotocol/inspector hatch run reddit-mcp
```

This will launch a web browser UI where you can invoke the `search_knowledge`, `explore_reddit_discussions`, `extract_public_opinion`, and `analyze_niche_trends` tools directly and inspect the JSON responses.

---

## 🤝 Contributing & Architecture

We love contributions! Please check out `docs/architecture.md` for architectural details and view `src/reddit_mcp/infrastructure/search/providers/README.md` to learn how to add a new search provider in seconds.

Please make sure your PR passes all linter checks (`ruff check .`) and unit tests (`pytest tests/`) before submitting.