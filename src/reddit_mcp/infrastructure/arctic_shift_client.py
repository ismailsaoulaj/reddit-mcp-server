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

logger = logging.getLogger(__name__)


class ArcticShiftError(Exception):
    """Exception for Arctic Shift client errors."""


class ArcticShiftClient:
    """
    Unauthenticated HTTP client for fetching Reddit data via Arctic Shift.
    Serves as a fallback when OAuth credentials are not provided.
    """

    BASE_URL = "https://arctic-shift.photon-reddit.com/api"

    def __init__(self, http_client: ResilientHTTPClient):
        self.http_client = http_client

    async def close(self):
        await self.http_client.close()

    def _map_submission(self, data: dict[str, Any]) -> RedditPost:
        created_utc = data.get("created_utc")
        permalink = data.get(
            "permalink",
            f"/r/{data.get('subreddit', 'unknown')}/comments/{data.get('id', '')}/",
        )

        return RedditPost(
            id=data.get("id", ""),
            title=data.get("title", ""),
            subreddit=data.get("subreddit", ""),
            score=data.get("score", 0),
            upvote_ratio=data.get("upvote_ratio", 0.0),
            num_comments=data.get("num_comments", 0),
            url=f"https://www.reddit.com{permalink}",
            age_in_days=calculate_age_in_days(created_utc),
            created_at_human=format_timestamp(created_utc),
            text_preview=truncate_text(data.get("selftext", ""), 500),
        )

    def _map_comment(
        self, data: dict[str, Any], post_id: str, subreddit: str
    ) -> RedditComment | None:
        body = data.get("body")
        if not body or body == "[deleted]" or body == "[removed]":
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
        match = re.search(r"/comments/([a-z0-9]+)", url)
        return match.group(1) if match else None

    async def get_posts_by_ids(self, post_ids: list[str]) -> list[RedditPost]:
        """Fetch multiple posts by their IDs using Arctic Shift."""
        if not post_ids:
            return []

        clean_ids = [pid.replace("t3_", "") for pid in post_ids]
        url = f"{self.BASE_URL}/posts/search"
        params = {"ids": ",".join(clean_ids)}

        try:
            response = await self.http_client.get(url, params=params)
            data = response.json()

            posts = []
            for child in data.get("data", []):
                posts.append(self._map_submission(child))
            return posts
        except Exception as e:
            logger.error(f"Arctic Shift get_posts_by_ids error: {e}")
            raise ArcticShiftError(f"Arctic Shift posts fetch failed: {e}") from e

    async def get_post_thread(
        self, post_url_or_id: str, max_comments: int = 50, comment_offset: int = 0
    ) -> tuple[RedditThread, int | None]:
        """Fetch a specific post and its top comments from Arctic Shift.

        Returns the thread plus the next offset into the score-sorted, filtered
        comment list, or None when the archive's list is exhausted.
        """
        post_id = (
            self._extract_post_id(post_url_or_id)
            if "/" in post_url_or_id
            else post_url_or_id.replace("t3_", "")
        )
        if not post_id:
            raise ArcticShiftError("Invalid Reddit post URL or ID provided.")

        # 1. Fetch the post
        posts = await self.get_posts_by_ids([post_id])
        if not posts:
            raise ArcticShiftError(f"Post {post_id} not found in Arctic Shift archive.")
        post = posts[0]

        # 2. Fetch the comments (oversample, then filter/sort/slice to true top-N)
        url = f"{self.BASE_URL}/comments/search"
        params = {"link_id": post_id, "limit": (comment_offset + max_comments) * 3}

        try:
            response = await self.http_client.get(url, params=params)
            data = response.json()

            comments = []
            for child in data.get("data", []):
                mapped = self._map_comment(child, post.id, post.subreddit)
                if mapped:
                    comments.append(mapped)

            comments.sort(key=lambda x: x.score, reverse=True)
            page = comments[comment_offset : comment_offset + max_comments]
            next_offset = (
                comment_offset + len(page) if len(page) == max_comments else None
            )
            return RedditThread(post=post, comments=page), next_offset
        except Exception as e:
            raise ArcticShiftError(
                f"Error fetching thread from Arctic Shift: {e}"
            ) from e

    async def get_subreddit_trends(
        self, *args, **kwargs
    ) -> tuple[list[RedditPost], str | None]:
        """Intentionally short-circuited. Archive lag makes this unsuitable for hot/trending."""
        raise ArcticShiftError(
            "Trending data is unavailable without OAuth credentials due to archive lag."
        )
