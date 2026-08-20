import logging
import re
import time
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
    Supports multi-tier cascading: Official OAuth -> Guest OAuth -> Public Web JSON.
    """

    # Reddit's .json listings never return more than 100 items per request,
    # regardless of the limit parameter.
    MAX_COMMENT_LIMIT = 100

    def __init__(
        self, http_client: ResilientHTTPClient, search_provider: BaseSearchProvider
    ):
        self.http_client = http_client
        self.search_provider = search_provider
        self._cache: dict[str, tuple[float, Any]] = {}
        self._cache_ttl = 180.0  # 3 minutes cache to avoid rate limits

    async def close(self):
        """Close underlying resources."""
        await self.http_client.close()

    def _prune_expired_cache(self) -> None:
        """Prune only expired items from cache instead of wiping everything."""
        now = time.time()
        expired_keys = [
            k for k, (ts, _) in self._cache.items() if (now - ts) >= self._cache_ttl
        ]
        for k in expired_keys:
            self._cache.pop(k, None)

    def _get_from_cache(self, key: str) -> Any | None:
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                return data
            del self._cache[key]
        return None

    def _set_cache(self, key: str, data: Any) -> None:
        self._prune_expired_cache()
        # If still at max capacity after pruning expired items, evict oldest 25% (LRU style)
        if len(self._cache) >= 200:
            sorted_keys = sorted(self._cache.keys(), key=lambda k: self._cache[k][0])
            for old_key in sorted_keys[:50]:
                self._cache.pop(old_key, None)
        self._cache[key] = (time.time(), data)

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
        """Fetch live trending posts from a subreddit using cascading fallback and caching."""
        subreddit = subreddit.strip()
        if subreddit.startswith("/r/"):
            subreddit = subreddit[3:]
        elif subreddit.startswith("r/"):
            subreddit = subreddit[2:]

        cache_key = f"trends:{subreddit}:{category}:{time_filter}:{limit}:{after}"
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached

        params = {"limit": limit, "t": time_filter}
        if after:
            params["after"] = after
        if before:
            params["before"] = before

        data = None
        # Tier 1: Try OAuth / Guest API
        try:
            url = f"https://oauth.reddit.com/r/{subreddit}/{category}.json"
            response = await self.http_client.get(url, params=params)
            data = response.json()
        except Exception as e:  # noqa: BLE001
            logger.info(
                f"OAuth/Guest trends fetch failed ({e}); falling back to public web JSON."
            )

        # Tier 2: Fallback to Public Web JSON
        if data is None:
            try:
                public_url = f"https://old.reddit.com/r/{subreddit}/{category}.json"
                data = await self.http_client.get_public_web(public_url, params=params)
            except Exception as e:
                raise RedditClientError(
                    f"Error fetching subreddit trends from all tiers: {e}"
                ) from e

        posts = []
        for child in data.get("data", {}).get("children", []):
            if child.get("kind") == "t3":
                posts.append(self._map_submission(child["data"]))

        new_after = data.get("data", {}).get("after")
        result = (posts, new_after)
        self._set_cache(cache_key, result)
        return result

    async def get_post_thread(
        self,
        post_url: str,
        max_comments: int = 50,
        comment_offset: int = 0,
        after_comment_id: str | None = None,
    ) -> tuple[RedditThread, tuple[int, str] | None]:
        """Fetch a specific post and its top comments via cascading fallback and caching."""
        post_id = self._extract_post_id(post_url)
        if not post_id:
            raise RedditClientError("Invalid Reddit post URL provided.")

        cache_key = (
            f"thread:{post_id}:{max_comments}:{comment_offset}:{after_comment_id}"
        )
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached

        requested_limit = max_comments + 20 + comment_offset
        window_clamped = requested_limit > self.MAX_COMMENT_LIMIT
        params = {"limit": min(requested_limit, self.MAX_COMMENT_LIMIT)}

        data = None
        # Tier 1: Try OAuth / Guest API
        try:
            url = f"https://oauth.reddit.com/comments/{post_id}.json"
            response = await self.http_client.get(url, params=params)
            data = response.json()
        except Exception as e:  # noqa: BLE001
            logger.info(
                f"OAuth/Guest thread fetch failed ({e}); falling back to public web JSON."
            )

        # Tier 2: Fallback to Public Web JSON
        if data is None or not isinstance(data, list) or len(data) < 2:
            try:
                public_url = f"https://old.reddit.com/comments/{post_id}.json"
                data = await self.http_client.get_public_web(public_url, params=params)
            except Exception as e:
                raise RedditClientError(
                    f"Error fetching thread from all tiers: {e}"
                ) from e

        if not isinstance(data, list) or len(data) < 2:
            raise RedditClientError("Unexpected response format from Reddit endpoints.")

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
                        anchor_found = True
                    elif anchor_found:
                        mapped = self._map_comment(c_data, post.id, post.subreddit)
                        if mapped:
                            comments.append(mapped)
                    raw_count += 1

                    replies = c_data.get("replies")
                    if isinstance(replies, dict):
                        parse_comments(replies.get("data", {}).get("children", []))

                elif kind == "more":
                    continue

        parse_comments(comment_children)

        if not anchor_found:
            if window_clamped:
                result = (RedditThread(post=post, comments=[]), None)
                self._set_cache(cache_key, result)
                return result
            raise RedditClientError(
                f"Continuation anchor {after_comment_id} not found in the "
                "fetched comment window; the live thread was re-sorted past "
                "the previous page. Restart pagination without a page_token."
            )

        next_cursor = (
            (raw_count, comments[-1].id) if len(comments) >= max_comments else None
        )
        result = (RedditThread(post=post, comments=comments), next_cursor)
        self._set_cache(cache_key, result)
        return result

    async def native_reddit_search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "all",
        limit: int = 10,
        after: str | None = None,
    ) -> tuple[list[RedditPost], str | None]:
        """Search using Reddit API / Web JSON with native sorting and pagination."""
        cache_key = f"search:{query}:{subreddit}:{sort}:{time_filter}:{limit}:{after}"
        cached = self._get_from_cache(cache_key)
        if cached is not None:
            return cached

        params = {"q": query, "sort": sort, "t": time_filter, "limit": limit}
        if subreddit:
            params["restrict_sr"] = True
        if after:
            params["after"] = after

        data = None
        # Tier 1: Try OAuth / Guest API
        try:
            url = "https://oauth.reddit.com/search.json"
            if subreddit:
                url = f"https://oauth.reddit.com/r/{subreddit}/search.json"
            response = await self.http_client.get(url, params=params)
            data = response.json()
        except Exception as e:  # noqa: BLE001
            logger.info(
                f"OAuth/Guest search failed ({e}); falling back to public web JSON."
            )

        # Tier 2: Fallback to Public Web JSON
        if data is None:
            try:
                public_url = "https://old.reddit.com/search.json"
                if subreddit:
                    public_url = f"https://old.reddit.com/r/{subreddit}/search.json"
                data = await self.http_client.get_public_web(public_url, params=params)
            except Exception as e:
                raise RedditClientError(
                    f"Error during native search from all tiers: {e}"
                ) from e

        posts = []
        for child in data.get("data", {}).get("children", []):
            if child.get("kind") == "t3":
                posts.append(self._map_submission(child["data"]))

        new_after = data.get("data", {}).get("after")
        result = (posts, new_after)
        self._set_cache(cache_key, result)
        return result

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
        Search Reddit using the injected SearchProvider (e.g. DDG) and resolve post data directly.
        """
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

            params = {"id": ",".join(post_ids)}
            data = None

            # Tier 1: Try OAuth / Guest API info.json
            try:
                url = "https://oauth.reddit.com/api/info.json"
                response = await self.http_client.get(url, params=params)
                data = response.json()
            except Exception as e:  # noqa: BLE001
                logger.info(
                    f"OAuth info.json failed ({e}); attempting public web info."
                )

            # Tier 2: Fallback to public web info
            if data is None:
                try:
                    public_url = "https://old.reddit.com/api/info.json"
                    data = await self.http_client.get_public_web(
                        public_url, params=params
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Public web info.json failed ({e}).")

            if data and "data" in data and "children" in data["data"]:
                posts = []
                for child in data["data"]["children"]:
                    if child.get("kind") == "t3":
                        posts.append(self._map_submission(child["data"]))
                return posts, None

            # If info.json is completely blocked, fallback to native search
            return await self.native_reddit_search(
                query=query,
                subreddit=subreddit,
                sort=sort,
                time_filter=time_filter,
                limit=limit,
            )

        except Exception as e:
            raise RedditClientError(f"Error during search resolution: {e}") from e
