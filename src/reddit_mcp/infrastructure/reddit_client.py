import logging
import re
from typing import Any

from reddit_mcp.domain.enrichment import (
    build_comment_url,
    calculate_age_in_days,
    format_timestamp,
    truncate_text,
)
from reddit_mcp.domain.models import RedditComment, RedditPost, RedditThread
from reddit_mcp.infrastructure.http import ResilientHTTPClient
from reddit_mcp.infrastructure.search.base import BaseSearchProvider

logger = logging.getLogger(__name__)


class RedditClientError(Exception):
    """Base exception for Reddit client errors."""


class RedditAuthRequiredError(RedditClientError):
    """Raised when an operation requires OAuth credentials that are missing."""


class RedditClient:
    """
    Asynchronous client for interacting with the Reddit API using a resilient HTTP client.
    """

    # Reddit's .json listings never return more than 100 items per request,
    # regardless of the limit parameter.
    MAX_COMMENT_LIMIT = 100

    def __init__(
        self, http_client: ResilientHTTPClient, search_provider: BaseSearchProvider
    ):
        self.http_client = http_client
        self.search_provider = search_provider

    async def close(self):
        """Close underlying resources."""
        await self.http_client.close()

    def _map_submission(self, data: dict[str, Any]) -> RedditPost:
        """Map Reddit JSON submission data to our enriched RedditPost model."""
        created_utc = data.get("created_utc")

        return RedditPost(
            id=data.get("id", ""),
            title=data.get("title", ""),
            subreddit=data.get("subreddit", ""),
            score=data.get("score", 0),
            upvote_ratio=data.get("upvote_ratio", 0.0),
            num_comments=data.get("num_comments", 0),
            url=f"https://www.reddit.com{data.get('permalink', '')}",
            age_in_days=calculate_age_in_days(created_utc),
            created_at_human=format_timestamp(created_utc),
            text_preview=truncate_text(data.get("selftext", ""), 500),
        )

    def _map_comment(
        self, data: dict[str, Any], post_id: str, subreddit: str
    ) -> RedditComment | None:
        """Map Reddit JSON comment data to our refined RedditComment model."""
        body = data.get("body")
        if not body:
            return None

        comment_id = data.get("id", "")
        return RedditComment(
            id=comment_id,
            author=data.get("author", "[deleted]"),
            score=data.get("score", 0),
            body=truncate_text(body, 2000),
            comment_url=build_comment_url(subreddit, post_id, comment_id),
            created_at_human=format_timestamp(data.get("created_utc")),
        )

    def _extract_post_id(self, url: str) -> str | None:
        """Extract the Reddit post ID from a standard URL."""
        match = re.search(r"/comments/([a-z0-9]+)", url)
        return match.group(1) if match else None

    async def get_subreddit_trends(
        self,
        subreddit: str,
        category: str = "hot",
        time_filter: str = "all",
        limit: int = 10,
        after: str | None = None,
        before: str | None = None,
    ) -> tuple[list[RedditPost], str | None]:
        """Fetch trending posts from a subreddit."""
        if not self.http_client.auth_manager.has_credentials:
            raise RedditAuthRequiredError("OAuth credentials missing.")

        subreddit = subreddit.strip()
        if subreddit.startswith("/r/"):
            subreddit = subreddit[3:]
        elif subreddit.startswith("r/"):
            subreddit = subreddit[2:]

        url = f"https://oauth.reddit.com/r/{subreddit}/{category}.json"
        params = {"limit": limit, "t": time_filter}
        if after:
            params["after"] = after
        if before:
            params["before"] = before

        try:
            response = await self.http_client.get(url, params=params)
            data = response.json()

            posts = []
            for child in data.get("data", {}).get("children", []):
                if child.get("kind") == "t3":
                    posts.append(self._map_submission(child["data"]))

            new_after = data.get("data", {}).get("after")
            return posts, new_after
        except Exception as e:
            raise RedditClientError(f"Error fetching subreddit trends: {e}") from e

    async def get_post_thread(
        self,
        post_url: str,
        max_comments: int = 50,
        comment_offset: int = 0,
        after_comment_id: str | None = None,
    ) -> tuple[RedditThread, tuple[int, str] | None]:
        """Fetch a specific post and its top comments, parsing the comment tree.

        Returns the thread plus the continuation cursor for the next page —
        the raw-stream offset (for request sizing) and the ID of the last
        comment served — or None when the raw comment stream is exhausted.

        Continuation anchors to `after_comment_id` (the last-served comment)
        instead of skipping by count, so a live re-sort between requests can
        neither duplicate already-served comments nor skip unseen ones after
        the anchor. If the anchor fell out of the fetched window (heavy
        re-sort), a RedditClientError is raised rather than silently
        misaligned pages.
        """
        if not self.http_client.auth_manager.has_credentials:
            raise RedditAuthRequiredError("OAuth credentials missing.")

        post_id = self._extract_post_id(post_url)
        if not post_id:
            raise RedditClientError("Invalid Reddit post URL provided.")

        url = f"https://oauth.reddit.com/comments/{post_id}.json"
        # Buffer for 'more' items; offset shifts the window into the raw stream.
        # Reddit caps limit at 100 server-side; clamping client-side lets us
        # tell "window was truncated" apart from a genuine re-sort.
        requested_limit = max_comments + 20 + comment_offset
        window_clamped = requested_limit > self.MAX_COMMENT_LIMIT
        params = {"limit": min(requested_limit, self.MAX_COMMENT_LIMIT)}

        try:
            response = await self.http_client.get(url, params=params)
            data = response.json()

            if not isinstance(data, list) or len(data) < 2:
                raise RedditClientError("Unexpected response format from Reddit API.")

            post_data = data[0]["data"]["children"][0]["data"]
            post = self._map_submission(post_data)

            comments = []
            raw_count = 0
            anchor_found = after_comment_id is None
            comment_children = data[1].get("data", {}).get("children", [])

            def parse_comments(children: list[dict[str, Any]]):
                nonlocal raw_count, anchor_found
                for child in children:
                    if len(comments) >= max_comments:
                        return

                    kind = child.get("kind")
                    c_data = child.get("data", {})

                    if kind == "t1":  # Comment
                        if not anchor_found and c_data.get("id") == after_comment_id:
                            # Everything before the anchor was already served.
                            anchor_found = True
                        elif anchor_found:
                            mapped = self._map_comment(c_data, post.id, post.subreddit)
                            if mapped:
                                comments.append(mapped)
                        raw_count += 1

                        # Recursively parse replies if they exist
                        replies = c_data.get("replies")
                        if isinstance(replies, dict):
                            parse_comments(replies.get("data", {}).get("children", []))

                    elif kind == "more":
                        # We ignore 'more' comments to avoid excessive API requests.
                        # This guarantees we only use the comments returned in the initial payload.
                        continue

            parse_comments(comment_children)

            if not anchor_found:
                if window_clamped:
                    # The requested window was truncated by Reddit's per-request
                    # cap, so the anchor may simply lie beyond what could be
                    # fetched — a bounded result, not a misalignment error.
                    return RedditThread(post=post, comments=[]), None
                raise RedditClientError(
                    f"Continuation anchor {after_comment_id} not found in the "
                    "fetched comment window; the live thread was re-sorted past "
                    "the previous page. Restart pagination without a page_token."
                )

            next_cursor = (
                (raw_count, comments[-1].id) if len(comments) >= max_comments else None
            )
            return RedditThread(post=post, comments=comments), next_cursor
        except Exception as e:
            raise RedditClientError(f"Error fetching thread: {e}") from e

    async def native_reddit_search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "all",
        limit: int = 10,
        after: str | None = None,
    ) -> tuple[list[RedditPost], str | None]:
        """Search using Reddit's official API. Ideal for metrics like upvote_ratio and native sorting."""
        if not self.http_client.auth_manager.has_credentials:
            raise RedditAuthRequiredError("OAuth credentials missing.")

        url = "https://oauth.reddit.com/search.json"
        params = {"q": query, "sort": sort, "t": time_filter, "limit": limit}
        if subreddit:
            url = f"https://oauth.reddit.com/r/{subreddit}/search.json"
            params["restrict_sr"] = True
        if after:
            params["after"] = after

        try:
            response = await self.http_client.get(url, params=params)
            data = response.json()

            posts = []
            for child in data.get("data", {}).get("children", []):
                if child.get("kind") == "t3":
                    posts.append(self._map_submission(child["data"]))

            new_after = data.get("data", {}).get("after")
            return posts, new_after
        except Exception as e:
            raise RedditClientError(f"Error during native Reddit search: {e}") from e

    async def search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "all",
        limit: int = 10,
        after: str | None = None,
        before: str | None = None,
    ) -> tuple[list[RedditPost], str | None]:
        """
        Search Reddit using the injected SearchProvider (e.g. DDG).
        Useful for general knowledge finding where native search fails.
        """
        if not self.http_client.auth_manager.has_credentials:
            raise RedditAuthRequiredError("OAuth credentials missing.")

        try:
            search_results = await self.search_provider.search(
                query=query, subreddit=subreddit, time_filter=time_filter, limit=limit
            )

            if not search_results:
                return [], None

            post_ids = []
            for res in search_results:
                if hasattr(res, "post_id") and res.post_id:
                    post_ids.append(f"t3_{res.post_id}")

            if not post_ids:
                return [], None

            url = "https://oauth.reddit.com/api/info.json"
            params = {"id": ",".join(post_ids)}

            response = await self.http_client.get(url, params=params)
            data = response.json()

            posts = []
            for child in data.get("data", {}).get("children", []):
                if child.get("kind") == "t3":
                    posts.append(self._map_submission(child["data"]))

            # Search providers like DDG don't natively return Reddit pagination tokens
            return posts, None

        except Exception as e:
            raise RedditClientError(f"Error during web search: {e}") from e
