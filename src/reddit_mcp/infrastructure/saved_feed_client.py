import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from html import unescape
from urllib.parse import urlparse

import httpx

from reddit_mcp.domain.enrichment import (
    calculate_age_in_days,
    format_timestamp,
    truncate_text,
)
from reddit_mcp.domain.models import RedditPost

logger = logging.getLogger(__name__)

_ATOM = "{http://www.w3.org/2005/Atom}"
_TIME_FILTER_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
_ALLOWED_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com"}
_POST_ID_RE = re.compile(r"/comments/([a-z0-9]+)")
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_FEED_TOKEN_RE = re.compile(r"feed=[0-9a-f]+")
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


class SavedFeedError(Exception):
    """Raised when the saved-items feed is unavailable or unreadable."""


class SavedFeedNotConfiguredError(SavedFeedError):
    """Raised when the saved-items feed URL is not configured."""


def _redact(url: str) -> str:
    return _FEED_TOKEN_RE.sub("feed=***", url)


def _strip_html(html: str) -> str:
    return _WHITESPACE_RE.sub(" ", unescape(_TAG_RE.sub(" ", html))).strip()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Python >= 3.11 fromisoformat accepts the trailing 'Z' designator.
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # A naive timestamp would crash comparisons against the aware cutoff;
        # Atom timestamps without an offset are best read as UTC.
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _is_comment_link(link: str) -> bool:
    parts = urlparse(link).path.strip("/").split("/")
    # Post: /r/{sub}/comments/{id}/{slug} — 5 segments; a comment permalink
    # carries an extra trailing comment-id segment.
    return "comments" in parts and len(parts) >= 6


class SavedFeedClient:
    """Reads a user's saved Reddit items via their private RSS feed
    (https://old.reddit.com/saved.rss?feed=<token>&user=<name>).

    The feed token is the credential, so the client uses its own HTTP session
    (never the shared ResilientHTTPClient, which logs request URLs on retry
    failures) and never embeds the URL in errors or log records.
    """

    MAX_FEED_ITEMS = 100
    FEED_TIMEOUT_SECONDS = 10.0
    MAX_REDIRECTS = 3

    def __init__(self, feed_url: str | None, user_agent: str):
        self.feed_url = feed_url
        self.user_agent = user_agent
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # Redirects are followed manually so every hop's host is validated
            # before the request (and its secret-bearing query) is sent.
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.FEED_TIMEOUT_SECONDS),
                headers={"User-Agent": self.user_agent},
            )
        return self._client

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _validated_url(self, url: str) -> httpx.URL:
        """Assert the URL is an https Reddit link before any request to it."""
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or (parsed.hostname or "") not in _ALLOWED_HOSTS:
            raise SavedFeedError(
                "Saved-items feed redirected to a non-Reddit host; refusing to "
                "continue (the feed token must never leave reddit.com)."
            )
        return httpx.URL(str(url))

    def _validate_feed_url(self) -> None:
        if not self.feed_url:
            raise SavedFeedNotConfiguredError("Saved-items feed URL is not configured.")
        # Guards against leaking the feed token to a mistyped initial host.
        self._validated_url(self.feed_url)

    async def _fetch_feed_document(self, client: httpx.AsyncClient) -> str:
        # Merge (not replace) — the feed URL's own query carries the secret
        # token; httpx `params=` would discard it.
        url = httpx.URL(self.feed_url).copy_merge_params(
            params={"limit": self.MAX_FEED_ITEMS}
        )
        for _ in range(self.MAX_REDIRECTS + 1):
            response = await client.get(url)
            if response.status_code not in _REDIRECT_STATUSES:
                response.raise_for_status()
                return response.text
            location = response.headers.get("location")
            if not location:
                raise SavedFeedError(
                    "Saved-items feed redirected without a Location header."
                )
            target = url.join(location)
            if "/login" in target.path:
                # Reddit redirects to /login when the feed token is rejected.
                raise SavedFeedError(
                    "Saved-items feed token was rejected (login redirect). "
                    "Regenerate the feed URL: Reddit preferences -> feed "
                    "settings, then update REDDIT_SAVED_RSS_URL."
                )
            # Validate BEFORE sending anything to the redirect target.
            url = self._validated_url(target)
        raise SavedFeedError(
            f"Saved-items feed exceeded {self.MAX_REDIRECTS} redirects."
        )

    async def get_saved_posts(
        self, time_filter: str = "month", limit: int = 50
    ) -> tuple[list[RedditPost], int]:
        """Fetch saved posts within the time period.

        Returns (posts, skipped_comments) where skipped_comments counts saved
        comment entries (this tool surfaces posts only). Feed metrics such as
        score and comment counts are not exposed by the feed and default to 0.
        """
        self._validate_feed_url()

        days = _TIME_FILTER_DAYS.get(time_filter)
        cutoff = datetime.now(UTC) - timedelta(days=days) if days else None

        client = await self._ensure_client()
        try:
            root = ET.fromstring(await self._fetch_feed_document(client))
        except (httpx.HTTPError, ET.ParseError) as e:
            # Never include the URL (it carries the secret feed token).
            logger.warning(f"Saved-items feed request failed ({type(e).__name__}).")
            raise SavedFeedError(
                "The saved-items feed could not be read. Try again later."
            ) from e

        entries: list[tuple[datetime, RedditPost]] = []
        skipped_comments = 0
        for entry in root.findall(f"{_ATOM}entry"):
            link_el = entry.find(f"{_ATOM}link")
            link = link_el.get("href", "") if link_el is not None else ""
            match = _POST_ID_RE.search(link)
            if not match:
                continue

            if _is_comment_link(link):
                skipped_comments += 1
                continue

            created = _parse_iso(
                entry.findtext(f"{_ATOM}updated") or entry.findtext(f"{_ATOM}published")
            )
            if created is None or (cutoff is not None and created < cutoff):
                continue

            category = entry.find(f"{_ATOM}category")
            created_ts = created.timestamp()
            entries.append(
                (
                    created,
                    RedditPost(
                        id=match.group(1),
                        title=entry.findtext(f"{_ATOM}title") or "(untitled)",
                        subreddit=(
                            category.get("term", "") if category is not None else ""
                        ),
                        score=0,
                        upvote_ratio=0.0,
                        num_comments=0,
                        url=link,
                        age_in_days=calculate_age_in_days(created_ts),
                        created_at_human=format_timestamp(created_ts),
                        text_preview=truncate_text(
                            _strip_html(entry.findtext(f"{_ATOM}content") or ""), 500
                        ),
                    ),
                )
            )

        # Atom does not guarantee entry order; enforce the newest-first
        # contract ourselves before applying the limit.
        entries.sort(key=lambda pair: pair[0], reverse=True)
        return [post for _, post in entries[:limit]], skipped_comments
