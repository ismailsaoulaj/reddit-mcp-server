import asyncio
import logging
from typing import Annotated, Literal

from pydantic import Field

from reddit_mcp.application.utils import (
    build_meta_context,
    is_high_quality_comment,
    llm_timeout,
)
from reddit_mcp.domain.models import (
    PaginatedCommentResponse,
    PaginatedPostResponse,
)
from reddit_mcp.infrastructure.arctic_shift_client import (
    ArcticShiftClient,
    ArcticShiftError,
)
from reddit_mcp.infrastructure.auth import RedditAuthManager
from reddit_mcp.infrastructure.http import ResilientHTTPClient
from reddit_mcp.infrastructure.reddit_client import (
    RedditClient,
    RedditClientError,
)
from reddit_mcp.infrastructure.saved_feed_client import (
    SavedFeedClient,
    SavedFeedError,
    SavedFeedNotConfiguredError,
)
from reddit_mcp.infrastructure.search.base import SearchProviderError
from reddit_mcp.infrastructure.search.providers.duckduckgo import (
    DuckDuckGoSearchProvider,
)

logger = logging.getLogger(__name__)

DEGRADED_MESSAGE = (
    "Reddit retrieval failed and the fallback flow could not complete. Try again later."
)
ARCHIVE_LAG_MESSAGE = "Served via the Arctic Shift archive; scores may lag live Reddit."
PROVIDER_SWITCH_MESSAGE = (
    "The page_token cannot be continued right now: its provider is unavailable, "
    "or the live thread was re-sorted past the previous page. Tokens are "
    "provider-specific (a Reddit raw-stream cursor is not an Arctic Shift list "
    "offset), so continuing on the other provider would skip or repeat "
    "comments. Retry shortly, or restart pagination by calling this tool again "
    "without page_token."
)
_REDDIT_TOKEN_PREFIX = "reddit"
_ARCTIC_TOKEN_PREFIX = "arctic"
# Caps the per-request comment window; without it a crafted token could blow
# up downstream request sizes (Reddit limit / Arctic Shift 3x oversample).
_MAX_COMMENT_OFFSET = 10_000
_DEPTH_CAP_MESSAGE = "Pagination depth limit reached; deeper comments are unavailable."


def _parse_comment_page_token(page_token: str) -> tuple[str, int, str | None]:
    """Parse a provider-prefixed continuation token ('reddit:30:abc' or
    'arctic:30'), returning (provider, offset, anchor_comment_id)."""
    parts = page_token.split(":")
    provider = parts[0]
    if provider == _REDDIT_TOKEN_PREFIX and len(parts) == 3:
        anchor = parts[2]
        if not anchor:
            raise ValueError(
                "Invalid page_token; the anchor comment ID must be non-empty."
            )
    elif provider == _ARCTIC_TOKEN_PREFIX and len(parts) == 2:
        anchor = None
    else:
        raise ValueError(
            "Invalid page_token; expected a token as returned by next_page_token "
            "(e.g. 'reddit:30:abc' or 'arctic:30')."
        )
    try:
        offset = int(parts[1])
    except ValueError as e:
        raise ValueError("Invalid page_token; the offset must be an integer.") from e
    if not 0 <= offset <= _MAX_COMMENT_OFFSET:
        raise ValueError(
            f"Invalid page_token; the offset must be between 0 and "
            f"{_MAX_COMMENT_OFFSET}."
        )
    return provider, offset, anchor


class DependencyContainer:
    """Simple container for lazy-loading and injecting dependencies."""

    _reddit_client: RedditClient | None = None
    _arctic_shift_client: ArcticShiftClient | None = None
    _search_provider: DuckDuckGoSearchProvider | None = None
    _saved_feed_client: SavedFeedClient | None = None

    @classmethod
    def _init_dependencies(cls):
        if cls._reddit_client is None:
            http_client = None
            try:
                from reddit_mcp.infrastructure.settings import get_settings

                settings = get_settings()
                user_agent = settings.reddit_user_agent
                auth_manager = RedditAuthManager(user_agent=user_agent)
                http_client = ResilientHTTPClient(
                    auth_manager=auth_manager, user_agent=user_agent
                )
                search_provider = DuckDuckGoSearchProvider()
                reddit_client = RedditClient(
                    http_client=http_client, search_provider=search_provider
                )
                arctic_shift_client = ArcticShiftClient(http_client=http_client)
                saved_feed_client = SavedFeedClient(
                    feed_url=settings.reddit_saved_rss_url, user_agent=user_agent
                )
            except Exception:
                cls.reset()
                if http_client is not None:
                    close = http_client.close()
                    try:
                        asyncio.get_running_loop().create_task(close)
                    except RuntimeError:
                        asyncio.run(close)
                raise
            cls._search_provider = search_provider
            cls._reddit_client = reddit_client
            cls._arctic_shift_client = arctic_shift_client
            cls._saved_feed_client = saved_feed_client

    @classmethod
    def get_reddit_client(cls) -> RedditClient:
        cls._init_dependencies()
        return cls._reddit_client

    @classmethod
    def get_arctic_shift_client(cls) -> ArcticShiftClient:
        cls._init_dependencies()
        return cls._arctic_shift_client

    @classmethod
    def get_search_provider(cls) -> DuckDuckGoSearchProvider:
        cls._init_dependencies()
        return cls._search_provider

    @classmethod
    def get_saved_feed_client(cls) -> SavedFeedClient:
        cls._init_dependencies()
        return cls._saved_feed_client

    @classmethod
    def is_initialized(cls) -> bool:
        return cls._reddit_client is not None

    @classmethod
    def reset(cls) -> None:
        """Drop all dependency references WITHOUT closing them. Test seam only;
        production shutdown must use aclose() to release HTTP resources."""
        cls._reddit_client = None
        cls._arctic_shift_client = None
        cls._search_provider = None
        cls._saved_feed_client = None

    @classmethod
    async def aclose(cls) -> None:
        """Close initialized clients and reset the container."""
        try:
            if cls._reddit_client is not None:
                await cls._reddit_client.close()
        finally:
            try:
                if cls._saved_feed_client is not None:
                    await cls._saved_feed_client.close()
            finally:
                cls.reset()

    @classmethod
    def override_reddit_client(cls, client: RedditClient) -> None:
        """Used for injecting mock clients during testing."""
        cls._reddit_client = client


@llm_timeout(timeout_seconds=15, response_model=PaginatedPostResponse)
async def search_knowledge(
    query: str,
    subreddit: str | None = None,
    time_filter: Literal["all", "day", "week", "month", "year"] = "all",
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
) -> PaginatedPostResponse:
    """
    STEP 1: FOUNDATION SEARCH. Use this to find factual threads or technical explanations.
    This uses a broad web-search (DuckDuckGo) to find Reddit threads that Reddit's own search might miss.
    Note: Pagination is not supported for this specific tool.
    """
    logger.info(f"search_knowledge: query='{query}'")
    client = DependencyContainer.get_reddit_client()
    posts = []
    data_source = None
    message = None

    try:
        posts, _ = await client.search(
            query=query, subreddit=subreddit, time_filter=time_filter, limit=limit
        )
    except RedditClientError as e:
        logger.warning(
            f"Reddit API failed or credentials missing ({e}); "
            "falling back to DDG + Arctic Shift for search_knowledge."
        )
        search_provider = DependencyContainer.get_search_provider()
        arctic_client = DependencyContainer.get_arctic_shift_client()

        try:
            search_results = await search_provider.search(
                query=query, subreddit=subreddit, time_filter=time_filter, limit=limit
            )
            post_ids = [res.post_id for res in search_results if res.post_id]
            if post_ids:
                posts = await arctic_client.get_posts_by_ids(post_ids)
                data_source = "arctic_shift"
                message = ARCHIVE_LAG_MESSAGE
        except (SearchProviderError, ArcticShiftError) as e:
            logger.warning(f"Fallback providers failed for search_knowledge: {e}")
            return PaginatedPostResponse(
                meta_context=build_meta_context(),
                data=[],
                next_page_token=None,
                status="degraded",
                message=DEGRADED_MESSAGE,
            )

    # Filter: Ensure we don't send posts with empty titles or very low quality
    valid_posts = [p for p in posts if len(p.title) > 5]

    return PaginatedPostResponse(
        meta_context=build_meta_context(),
        data=valid_posts,
        next_page_token=None,
        data_source=data_source,
        message=message,
    )


@llm_timeout(timeout_seconds=15, response_model=PaginatedPostResponse)
async def explore_reddit_discussions(
    keyword: str,
    subreddit: str | None = None,
    sort: Literal["relevance", "hot", "top", "new", "comments"] = "relevance",
    time_filter: Literal["all", "day", "week", "month", "year"] = "year",
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
    page_token: str | None = None,
) -> PaginatedPostResponse:
    """
    STEP 2: SENTIMENT EXPLORATION. Use this to gauge public opinion and market acceptance.
    Always check `upvote_ratio`: >0.8 = Positive, ~0.5 = Controversial.
    Check `age_in_days` to ensure relevance. Use `next_page_token` to see more results.
    """
    logger.info(f"explore_reddit_discussions: keyword='{keyword}'")
    client = DependencyContainer.get_reddit_client()
    posts = []
    next_token = None
    data_source = None
    message = None

    try:
        posts, next_token = await client.native_reddit_search(
            query=keyword,
            subreddit=subreddit,
            sort=sort,
            time_filter=time_filter,
            limit=limit,
            after=page_token,
        )
    except RedditClientError as e:
        logger.warning(
            f"Reddit API failed or credentials missing ({e}); "
            "falling back to DDG + Arctic Shift for explore_reddit_discussions."
        )
        search_provider = DependencyContainer.get_search_provider()
        arctic_client = DependencyContainer.get_arctic_shift_client()

        try:
            search_results = await search_provider.search(
                query=keyword, subreddit=subreddit, time_filter=time_filter, limit=limit
            )
            post_ids = [res.post_id for res in search_results if res.post_id]
            if post_ids:
                posts = await arctic_client.get_posts_by_ids(post_ids)
                data_source = "arctic_shift"
                message = ARCHIVE_LAG_MESSAGE + (
                    " Sort and pagination are unavailable in fallback mode."
                )
        except (SearchProviderError, ArcticShiftError) as e:
            logger.warning(
                f"Fallback providers failed for explore_reddit_discussions: {e}"
            )
            return PaginatedPostResponse(
                meta_context=build_meta_context(),
                data=[],
                next_page_token=None,
                status="degraded",
                message=DEGRADED_MESSAGE,
            )

    return PaginatedPostResponse(
        meta_context=build_meta_context(),
        data=posts,
        next_page_token=next_token,
        data_source=data_source,
        message=message,
    )


@llm_timeout(timeout_seconds=20, response_model=PaginatedCommentResponse)
async def extract_public_opinion(
    post_url: str,
    max_comments: Annotated[int, Field(ge=1, le=100)] = 30,
    page_token: str | None = None,
) -> PaginatedCommentResponse:
    """
    DEEP DIVE TOOL: Use this ONLY after finding a relevant post via search tools.
    This tool extracts PURE human opinions, filtering out noise, bots, and low-effort content.
    Citations: You MUST use the `comment_url` for each specific quote in your final report.
    Pagination: pass `next_page_token` to continue reading deeper comments.
    Tokens are provider-prefixed (e.g. 'reddit:30:abc') and only the provider
    that issued one can continue it.
    """
    logger.info(f"extract_public_opinion: url='{post_url}'")
    token_provider = None
    comment_offset = 0
    anchor_id = None
    if page_token:
        token_provider, comment_offset, anchor_id = _parse_comment_page_token(
            page_token
        )
    client = DependencyContainer.get_reddit_client()
    data_source = None
    message = None
    next_cursor = None

    if token_provider == _ARCTIC_TOKEN_PREFIX:
        # An Arctic Shift cursor indexes the archive's score-sorted list; it
        # cannot be replayed against Reddit's raw DFS stream, so continue on
        # the archive directly.
        arctic_client = DependencyContainer.get_arctic_shift_client()
        try:
            thread, next_cursor = await arctic_client.get_post_thread(
                post_url_or_id=post_url,
                max_comments=max_comments,
                comment_offset=comment_offset,
            )
        except ArcticShiftError as e:
            logger.warning(f"Arctic Shift continuation failed: {e}")
            return PaginatedCommentResponse(
                meta_context=build_meta_context(),
                data=[],
                status="degraded",
                message=DEGRADED_MESSAGE,
            )
        data_source = "arctic_shift"
        message = ARCHIVE_LAG_MESSAGE
    else:
        # Fetch thread (The client already maps basic data)
        try:
            thread, next_cursor = await client.get_post_thread(
                post_url=post_url,
                max_comments=max_comments,
                comment_offset=comment_offset,
                after_comment_id=anchor_id,
            )
        except RedditClientError as e:
            if token_provider == _REDDIT_TOKEN_PREFIX:
                # A Reddit raw-stream offset is meaningless to the archive's
                # index space; refuse rather than skip/repeat comments.
                logger.warning(
                    f"Reddit failed mid-pagination ({e}); refusing cross-provider "
                    "continuation."
                )
                return PaginatedCommentResponse(
                    meta_context=build_meta_context(),
                    data=[],
                    status="degraded",
                    message=PROVIDER_SWITCH_MESSAGE,
                )
            logger.warning(
                f"Reddit API failed or credentials missing ({e}); "
                "falling back to Arctic Shift for extract_public_opinion."
            )
            arctic_client = DependencyContainer.get_arctic_shift_client()
            try:
                thread, next_cursor = await arctic_client.get_post_thread(
                    post_url_or_id=post_url,
                    max_comments=max_comments,
                    comment_offset=comment_offset,
                )
            except ArcticShiftError as e:
                logger.warning(
                    f"Fallback providers failed for extract_public_opinion: {e}"
                )
                return PaginatedCommentResponse(
                    meta_context=build_meta_context(),
                    data=[],
                    status="degraded",
                    message=DEGRADED_MESSAGE,
                )
            data_source = "arctic_shift"
            message = ARCHIVE_LAG_MESSAGE

    # Application Layer Filtering: Drop low quality before responding
    # This saves tokens and ensures the LLM only sees valuable input.
    refined_comments = [
        c
        for c in thread.comments
        if is_high_quality_comment(
            author=c.author,
            body=c.body,
            score=c.score,
            thread_age_in_days=thread.post.age_in_days,
        )
    ]

    # The serving client reports its own continuation cursor (None when its
    # stream/list is exhausted); the prefix binds the cursor to that provider
    # so it can never be replayed on the other index space. Emission is capped
    # at _MAX_COMMENT_OFFSET so every emitted token is parseable on the next
    # call: a page that would run past the cap omits the token and says so.
    next_page_token = None
    depth_capped = False
    if data_source == "arctic_shift":
        if next_cursor is not None:
            if next_cursor <= _MAX_COMMENT_OFFSET:
                next_page_token = f"{_ARCTIC_TOKEN_PREFIX}:{next_cursor}"
            else:
                depth_capped = True
    elif next_cursor is not None:
        next_raw_offset, next_anchor = next_cursor
        if next_raw_offset <= _MAX_COMMENT_OFFSET:
            next_page_token = f"{_REDDIT_TOKEN_PREFIX}:{next_raw_offset}:{next_anchor}"
        else:
            depth_capped = True
    if depth_capped:
        message = f"{message} {_DEPTH_CAP_MESSAGE}" if message else _DEPTH_CAP_MESSAGE

    return PaginatedCommentResponse(
        meta_context=build_meta_context(),
        data=refined_comments,
        next_page_token=next_page_token,
        data_source=data_source,
        message=message,
    )


@llm_timeout(timeout_seconds=15, response_model=PaginatedPostResponse)
async def analyze_niche_trends(
    subreddit_name: str,
    trend_type: Literal["hot", "new", "top", "rising"] = "rising",
    limit: Annotated[int, Field(ge=1, le=100)] = 10,
    page_token: str | None = None,
) -> PaginatedPostResponse:
    """
    Use this tool when asked to suggest ideas, find pain points, or discover opportunities in a specific niche (e.g., 'SaaS', 'Entrepreneur').
    By looking at 'rising' or 'hot' posts, you can identify what problems users are actively struggling with RIGHT NOW.
    Always compare the post's `created_at` with the `current_server_date` provided in `meta_context`.
    """
    logger.info(f"analyze_niche_trends: subreddit='{subreddit_name}'")
    client = DependencyContainer.get_reddit_client()

    try:
        posts, next_token = await client.get_subreddit_trends(
            subreddit=subreddit_name, category=trend_type, limit=limit, after=page_token
        )
        return PaginatedPostResponse(
            meta_context=build_meta_context(), data=posts, next_page_token=next_token
        )
    except RedditClientError:
        logger.warning("Reddit API failed or credentials missing. Cannot fetch trends.")
        return PaginatedPostResponse(
            meta_context=build_meta_context(),
            data=[],
            next_page_token=None,
            status="warning",
            message=(
                "Trending data is unavailable (OAuth credentials missing or Reddit API "
                "unreachable) due to archive lag. Please use search tools instead."
            ),
        )


SAVED_FEED_UNCONFIGURED_MESSAGE = (
    "Saved posts are unavailable: set REDDIT_SAVED_RSS_URL to your private "
    "saved-items feed. While logged in, open old.reddit.com/saved.rss and copy "
    "the full URL (it contains a secret feed token — treat it like a password)."
)


@llm_timeout(timeout_seconds=15, response_model=PaginatedPostResponse)
async def get_saved_posts(
    time_filter: Literal["day", "week", "month", "year", "all"] = "month",
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> PaginatedPostResponse:
    """
    PERSONAL TOOL: fetches the USER'S saved Reddit posts from a defined time
    period (day/week/month/year/all), newest first. Use this to revisit,
    summarize, or triage content the user explicitly bookmarked.
    Note: the feed does not expose scores or comment counts; posts with thin
    titles are filtered out. Pagination is not supported for this tool.
    """
    logger.info(f"get_saved_posts: time_filter='{time_filter}'")
    feed_client = DependencyContainer.get_saved_feed_client()

    try:
        # Full feed window up front so the quality filter below can consider
        # every in-period entry; the caller's limit is applied after filtering.
        posts, skipped_comments = await feed_client.get_saved_posts(
            time_filter=time_filter, limit=feed_client.MAX_FEED_ITEMS
        )
    except SavedFeedNotConfiguredError:
        logger.warning("Saved-items feed is not configured.")
        return PaginatedPostResponse(
            meta_context=build_meta_context(),
            data=[],
            next_page_token=None,
            status="warning",
            message=SAVED_FEED_UNCONFIGURED_MESSAGE,
        )
    except SavedFeedError as e:
        logger.warning(f"Saved-items feed failed for get_saved_posts: {e}")
        return PaginatedPostResponse(
            meta_context=build_meta_context(),
            data=[],
            next_page_token=None,
            status="degraded",
            # Client messages are user-facing (and never contain the feed URL).
            message=str(e),
        )

    # Same quality gate as search_knowledge: drop empty/very short titles,
    # THEN honor the caller's limit (filtering after slicing would starve the
    # page whenever a short-title entry sits near the top of the feed).
    valid_posts = [p for p in posts if len(p.title) > 5][:limit]

    message = (
        "Sourced from the user's private saved-items feed; scores and comment "
        "counts are not exposed by the feed."
    )
    if skipped_comments:
        message += f" {skipped_comments} saved comment(s) were skipped (posts only)."

    return PaginatedPostResponse(
        meta_context=build_meta_context(),
        data=valid_posts,
        next_page_token=None,
        data_source="saved_rss",
        message=message,
    )
