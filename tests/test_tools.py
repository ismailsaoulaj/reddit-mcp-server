import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from reddit_mcp.application import tools
from reddit_mcp.application.tools import DependencyContainer
from reddit_mcp.domain.enrichment import truncate_text
from reddit_mcp.domain.models import (
    PaginatedCommentResponse,
    PaginatedPostResponse,
    RedditComment,
    RedditPost,
    RedditThread,
)
from reddit_mcp.infrastructure.arctic_shift_client import ArcticShiftError
from reddit_mcp.infrastructure.reddit_client import (
    RedditAuthRequiredError,
    RedditClient,
    RedditClientError,
)
from reddit_mcp.infrastructure.saved_feed_client import (
    SavedFeedError,
    SavedFeedNotConfiguredError,
)
from reddit_mcp.infrastructure.search.base import SearchProviderError
from reddit_mcp.infrastructure.search.providers.duckduckgo import RedditSearchResult


@pytest.fixture
def sample_post():
    return RedditPost(
        id="123",
        title="Valid Test Post Title",
        subreddit="test",
        score=100,
        upvote_ratio=0.95,
        num_comments=10,
        url="https://reddit.com/r/test/comments/123",
        age_in_days=5,
        created_at_human="October 15, 2023",
        text_preview="Hello preview",
    )


@pytest.fixture(autouse=True)
def mock_reddit_client():
    DependencyContainer.reset()
    mock_client = MagicMock()
    mock_client.search = AsyncMock()
    mock_client.native_reddit_search = AsyncMock()
    mock_client.get_subreddit_trends = AsyncMock()
    mock_client.get_post_thread = AsyncMock()

    mock_arctic = MagicMock()
    mock_arctic.get_posts_by_ids = AsyncMock()
    mock_arctic.get_post_thread = AsyncMock()

    mock_search = MagicMock()
    mock_search.search = AsyncMock()

    mock_feed = MagicMock()
    mock_feed.get_saved_posts = AsyncMock()

    DependencyContainer._reddit_client = mock_client
    DependencyContainer._arctic_shift_client = mock_arctic
    DependencyContainer._search_provider = mock_search
    DependencyContainer._saved_feed_client = mock_feed

    yield mock_client

    DependencyContainer.reset()


@pytest.mark.asyncio
async def test_search_knowledge_filters_short_titles(mock_reddit_client, sample_post):
    # Create a post with a very short title
    bad_post = sample_post.model_copy()
    bad_post.title = "Hi"

    mock_reddit_client.search.return_value = ([sample_post, bad_post], None)

    result = await tools.search_knowledge("query")

    # Should only return the valid_post
    assert len(result.data) == 1
    assert result.data[0].title == "Valid Test Post Title"


@pytest.mark.asyncio
async def test_extract_public_opinion_logic(mock_reddit_client, sample_post):
    # One high quality, one low quality (short)
    good_comment = RedditComment(
        id="c1",
        author="user1",
        score=10,
        body="This is a long enough and high quality comment for testing.",
        comment_url="url1",
        created_at_human="date",
    )
    bad_comment = RedditComment(
        id="c2",
        author="bot",
        score=-5,
        body="short",
        comment_url="url2",
        created_at_human="date",
    )

    mock_thread = RedditThread(post=sample_post, comments=[good_comment, bad_comment])
    mock_reddit_client.get_post_thread.return_value = (mock_thread, None)

    result = await tools.extract_public_opinion("http://url")

    # Should filter out the bad comment at application layer
    assert len(result.data) == 1
    assert result.data[0].id == "c1"
    assert "instruction_note" in result.meta_context.model_dump()


def _make_quality_comment(cid: str) -> RedditComment:
    return RedditComment(
        id=cid,
        author=f"user_{cid}",
        score=10,
        body="This is a long enough and high quality comment for testing.",
        comment_url=f"url_{cid}",
        created_at_human="date",
    )


@pytest.mark.asyncio
async def test_extract_public_opinion_full_page_returns_next_token(
    mock_reddit_client, sample_post
):
    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(
            post=sample_post,
            comments=[_make_quality_comment("c1"), _make_quality_comment("c2")],
        ),
        (2, "c2"),
    )

    result = await tools.extract_public_opinion("http://url", max_comments=2)

    assert result.next_page_token == "reddit:2:c2"
    mock_reddit_client.get_post_thread.assert_awaited_with(
        post_url="http://url", max_comments=2, comment_offset=0, after_comment_id=None
    )


@pytest.mark.asyncio
async def test_extract_public_opinion_page_token_advances_offset(
    mock_reddit_client, sample_post
):
    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(
            post=sample_post,
            comments=[_make_quality_comment("c4"), _make_quality_comment("c5")],
        ),
        (5, "c5"),
    )

    result = await tools.extract_public_opinion(
        "http://url", max_comments=2, page_token="reddit:3:c3"
    )

    assert result.next_page_token == "reddit:5:c5"
    mock_reddit_client.get_post_thread.assert_awaited_with(
        post_url="http://url", max_comments=2, comment_offset=3, after_comment_id="c3"
    )


@pytest.mark.asyncio
async def test_extract_public_opinion_client_offset_drives_token(
    mock_reddit_client, sample_post
):
    # If the client consumed extra raw entries (e.g. deleted comments), the
    # token must use the client's offset, not offset + max_comments.
    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(
            post=sample_post,
            comments=[_make_quality_comment("c4"), _make_quality_comment("c5")],
        ),
        (7, "c5"),
    )

    result = await tools.extract_public_opinion("http://url", max_comments=2)

    assert result.next_page_token == "reddit:7:c5"


@pytest.mark.asyncio
async def test_extract_public_opinion_short_page_returns_no_token(
    mock_reddit_client, sample_post
):
    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(post=sample_post, comments=[_make_quality_comment("c1")]),
        None,
    )

    result = await tools.extract_public_opinion("http://url", max_comments=2)

    assert len(result.data) == 1
    assert result.next_page_token is None


@pytest.mark.asyncio
async def test_extract_public_opinion_invalid_page_token_raises_value_error(
    mock_reddit_client,
):
    bad_tokens = [
        "abc",
        "2",
        "reddit",
        "reddit:abc",
        "reddit:3",  # missing anchor comment ID
        "reddit:3:",
        "reddit:3:c3:junk",
        "reddit:-1:c3",
        "bogus:5",
    ]
    for bad_token in bad_tokens:
        with pytest.raises(ValueError, match="Invalid page_token"):
            await tools.extract_public_opinion("http://url", page_token=bad_token)

    mock_reddit_client.get_post_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_public_opinion_page_token_offset_is_bounded(
    mock_reddit_client,
):
    with pytest.raises(ValueError, match="between 0 and"):
        await tools.extract_public_opinion(
            "http://url", page_token="reddit:99999999:c1"
        )

    mock_reddit_client.get_post_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_public_opinion_offset_cap_omits_token_and_marks_bounded(
    mock_reddit_client, sample_post
):
    # A full page starting at the maximum offset would emit a token beyond
    # the parseable range; the token must be omitted and the result marked.
    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(
            post=sample_post,
            comments=[_make_quality_comment("c1"), _make_quality_comment("c2")],
        ),
        (10_100, "c2"),
    )

    result = await tools.extract_public_opinion(
        "http://url", max_comments=2, page_token="reddit:10000:c0"
    )

    assert result.next_page_token is None
    assert "depth limit" in result.message


@pytest.mark.asyncio
async def test_extract_public_opinion_arctic_offset_cap_marks_bounded(
    mock_reddit_client, sample_post
):
    mock_arctic = DependencyContainer.get_arctic_shift_client()
    mock_arctic.get_post_thread.return_value = (
        RedditThread(
            post=sample_post,
            comments=[_make_quality_comment("c1"), _make_quality_comment("c2")],
        ),
        10_100,
    )

    result = await tools.extract_public_opinion(
        "http://url", max_comments=2, page_token="arctic:10000"
    )

    assert result.next_page_token is None
    assert "depth limit" in result.message


@pytest.mark.asyncio
async def test_extract_public_opinion_emitted_tokens_stay_parseable_at_cap(
    mock_reddit_client, sample_post
):
    # A cursor landing exactly on the cap is still emitted and must parse on
    # the next call (round-trip through the tool).
    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(
            post=sample_post,
            comments=[_make_quality_comment("c1"), _make_quality_comment("c2")],
        ),
        (10_000, "c2"),
    )

    result = await tools.extract_public_opinion(
        "http://url", max_comments=2, page_token="reddit:9950:c0"
    )
    assert result.next_page_token == "reddit:10000:c2"

    mock_reddit_client.get_post_thread.reset_mock()
    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(post=sample_post, comments=[_make_quality_comment("c3")]),
        None,
    )
    result = await tools.extract_public_opinion(
        "http://url", max_comments=2, page_token=result.next_page_token
    )

    mock_reddit_client.get_post_thread.assert_awaited_with(
        post_url="http://url",
        max_comments=2,
        comment_offset=10_000,
        after_comment_id="c2",
    )
    assert result.next_page_token is None


@pytest.mark.asyncio
async def test_extract_public_opinion_rejects_cross_provider_continuation(
    mock_reddit_client, sample_post
):
    # Page 1 served by Reddit (token 'reddit:3'); Reddit now failing — the
    # token must NOT be replayed against Arctic Shift's index space.
    mock_reddit_client.get_post_thread.side_effect = RedditClientError("HTTP 429")

    mock_arctic = DependencyContainer.get_arctic_shift_client()

    result = await tools.extract_public_opinion("http://url", page_token="reddit:3:c3")

    assert result.status == "degraded"
    assert "provider-specific" in result.message
    assert result.data == []
    assert result.next_page_token is None
    mock_arctic.get_post_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_public_opinion_arctic_token_continues_on_arctic(
    mock_reddit_client, sample_post
):
    # An arctic-issued token continues on the archive even though Reddit is
    # healthy: the cursor only has meaning in the archive's index space.
    mock_arctic = DependencyContainer.get_arctic_shift_client()
    mock_arctic.get_post_thread.return_value = (
        RedditThread(
            post=sample_post,
            comments=[_make_quality_comment("c4"), _make_quality_comment("c5")],
        ),
        6,
    )

    result = await tools.extract_public_opinion(
        "http://url", max_comments=2, page_token="arctic:3"
    )

    assert result.status == "success"
    assert result.data_source == "arctic_shift"
    assert result.next_page_token == "arctic:6"
    mock_reddit_client.get_post_thread.assert_not_awaited()
    mock_arctic.get_post_thread.assert_awaited_with(
        post_url_or_id="http://url", max_comments=2, comment_offset=3
    )


@pytest.mark.asyncio
async def test_explore_reddit_discussions_pagination(mock_reddit_client, sample_post):
    # Simulate reddit client returning a next_page_token
    mock_reddit_client.native_reddit_search.return_value = (
        [sample_post],
        "after_token_123",
    )

    result = await tools.explore_reddit_discussions("keyword")

    assert len(result.data) == 1
    assert result.next_page_token == "after_token_123"


@pytest.mark.asyncio
async def test_search_knowledge_empty_results(mock_reddit_client):
    # Simulate an empty search result
    mock_reddit_client.search.return_value = ([], None)

    result = await tools.search_knowledge("nonexistent_query")

    assert len(result.data) == 0
    assert result.next_page_token is None


@pytest.mark.asyncio
async def test_tool_llm_timeout(monkeypatch):
    # We test the timeout by mocking asyncio.wait_for to raise a TimeoutError
    async def mock_wait_for(aw, timeout=None, **kwargs):
        aw.close()  # Close the unawaited coroutine to prevent RuntimeWarning
        raise TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)

    result = await tools.search_knowledge("query")

    assert isinstance(result, PaginatedPostResponse)
    assert result.status == "partial_timeout"
    assert "Request paused" in result.message
    assert len(result.data) == 0


@pytest.mark.asyncio
async def test_tool_llm_timeout_comment_model_has_no_extra_fields(monkeypatch):
    async def mock_wait_for(aw, timeout=None, **kwargs):
        aw.close()
        raise TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)

    result = await tools.extract_public_opinion("http://url")

    assert isinstance(result, PaginatedCommentResponse)
    assert result.status == "partial_timeout"
    assert "Request paused" in result.message
    assert len(result.data) == 0
    assert set(result.model_dump().keys()) == set(PaginatedCommentResponse.model_fields)


def test_truncate_text_util():
    # Phase 4 Utils test
    long_text = "A" * 3000
    truncated = truncate_text(long_text, 2000)

    assert len(truncated) == 2000 + len("... (truncated)")
    assert truncated.endswith("... (truncated)")

    # Test empty
    assert truncate_text(None) == ""
    assert truncate_text("") == ""


@pytest.mark.asyncio
async def test_fallback_search_knowledge(sample_post):
    # Simulate missing OAuth credentials (subclass of RedditClientError)
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.search.side_effect = RedditAuthRequiredError()

    # Mock DuckDuckGo returning a URL
    mock_search = DependencyContainer.get_search_provider()
    mock_search.search.return_value = [
        RedditSearchResult(url="http://reddit.com/comments/123", title="t", snippet="s")
    ]

    # Mock Arctic Shift returning the parsed post
    mock_arctic = DependencyContainer.get_arctic_shift_client()
    mock_arctic.get_posts_by_ids.return_value = [sample_post]

    result = await tools.search_knowledge("query")
    assert len(result.data) == 1
    assert result.data[0].id == "123"
    assert result.status == "success"
    assert result.data_source == "arctic_shift"
    assert "Arctic Shift" in result.message
    assert "lag" in result.message


@pytest.mark.asyncio
async def test_fallback_search_knowledge_on_client_error(sample_post):
    # Simulate failed credentials (generic RedditClientError, e.g. HTTP 401)
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.search.side_effect = RedditClientError("HTTP 401")

    mock_search = DependencyContainer.get_search_provider()
    mock_search.search.return_value = [
        RedditSearchResult(url="http://reddit.com/comments/123", title="t", snippet="s")
    ]

    mock_arctic = DependencyContainer.get_arctic_shift_client()
    mock_arctic.get_posts_by_ids.return_value = [sample_post]

    result = await tools.search_knowledge("query")
    assert len(result.data) == 1
    assert result.data[0].id == "123"
    assert result.status == "success"
    assert result.data_source == "arctic_shift"
    assert "Arctic Shift" in result.message


@pytest.mark.asyncio
async def test_fallback_search_knowledge_no_post_ids_no_provenance():
    # DDG results without parsable post IDs -> Arctic Shift never consulted,
    # so the response must not claim archive provenance/lag
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.search.side_effect = RedditClientError("HTTP 401")

    mock_search = DependencyContainer.get_search_provider()
    mock_search.search.return_value = [
        RedditSearchResult(url="http://reddit.com/r/test", title="t", snippet="s")
    ]

    mock_arctic = DependencyContainer.get_arctic_shift_client()

    result = await tools.search_knowledge("query")
    assert result.status == "success"
    assert len(result.data) == 0
    assert result.data_source is None
    assert result.message is None
    mock_arctic.get_posts_by_ids.assert_not_called()


@pytest.mark.asyncio
async def test_fallback_explore_on_client_error(sample_post):
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.native_reddit_search.side_effect = RedditClientError("HTTP 401")

    mock_search = DependencyContainer.get_search_provider()
    mock_search.search.return_value = [
        RedditSearchResult(url="http://reddit.com/comments/123", title="t", snippet="s")
    ]

    mock_arctic = DependencyContainer.get_arctic_shift_client()
    mock_arctic.get_posts_by_ids.return_value = [sample_post]

    result = await tools.explore_reddit_discussions("keyword")
    assert len(result.data) == 1
    assert result.data[0].id == "123"
    assert result.status == "success"
    assert result.data_source == "arctic_shift"
    assert "Arctic Shift" in result.message
    assert result.next_page_token is None
    assert "Sort and pagination are unavailable" in result.message


@pytest.mark.asyncio
async def test_fallback_extract_public_opinion_on_client_error(sample_post):
    good_comment = RedditComment(
        id="c1",
        author="user1",
        score=10,
        body="This is a long enough and high quality comment for testing.",
        comment_url="url1",
        created_at_human="date",
    )

    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.get_post_thread.side_effect = RedditClientError("HTTP 401")

    mock_arctic = DependencyContainer.get_arctic_shift_client()
    mock_arctic.get_post_thread.return_value = (
        RedditThread(post=sample_post, comments=[good_comment]),
        None,
    )

    result = await tools.extract_public_opinion("http://url")
    assert len(result.data) == 1
    assert result.data[0].id == "c1"
    assert result.status == "success"
    assert result.data_source == "arctic_shift"
    assert "Arctic Shift" in result.message


@pytest.mark.asyncio
async def test_degraded_search_knowledge_when_ddg_fails():
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.search.side_effect = RedditClientError("HTTP 401")

    mock_search = DependencyContainer.get_search_provider()
    mock_search.search.side_effect = SearchProviderError("DDG is down")

    result = await tools.search_knowledge("query")
    assert result.status == "degraded"
    assert len(result.data) == 0
    assert result.message
    assert "Try again later" in result.message


@pytest.mark.asyncio
async def test_degraded_search_knowledge_when_arctic_fails():
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.search.side_effect = RedditClientError("rate limited")

    mock_search = DependencyContainer.get_search_provider()
    mock_search.search.return_value = [
        RedditSearchResult(url="http://reddit.com/comments/123", title="t", snippet="s")
    ]

    mock_arctic = DependencyContainer.get_arctic_shift_client()
    mock_arctic.get_posts_by_ids.side_effect = ArcticShiftError("archive down")

    result = await tools.search_knowledge("query")
    assert result.status == "degraded"
    assert len(result.data) == 0
    assert result.message
    assert "Try again later" in result.message


@pytest.mark.asyncio
async def test_degraded_extract_public_opinion_when_arctic_fails():
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.get_post_thread.side_effect = RedditClientError("network error")

    mock_arctic = DependencyContainer.get_arctic_shift_client()
    mock_arctic.get_post_thread.side_effect = ArcticShiftError("archive down")

    result = await tools.extract_public_opinion("http://url")
    assert result.status == "degraded"
    assert len(result.data) == 0
    assert result.message
    assert "Try again later" in result.message


@pytest.mark.asyncio
async def test_extract_public_opinion_young_thread_keeps_low_score_comments(
    mock_reddit_client, sample_post
):
    young_post = sample_post.model_copy(update={"age_in_days": 0})
    fresh_comment = RedditComment(
        id="c1",
        author="user1",
        score=1,
        body="This is a long enough comment that just has not been voted on yet.",
        comment_url="url1",
        created_at_human="date",
    )

    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(post=young_post, comments=[fresh_comment]),
        None,
    )

    result = await tools.extract_public_opinion("http://url")

    assert len(result.data) == 1
    assert result.data[0].id == "c1"


@pytest.mark.asyncio
async def test_extract_public_opinion_old_thread_filters_low_score_comments(
    mock_reddit_client, sample_post
):
    old_post = sample_post.model_copy(update={"age_in_days": 5})
    stale_comment = RedditComment(
        id="c1",
        author="user1",
        score=1,
        body="This is a long enough comment that never earned any upvotes.",
        comment_url="url1",
        created_at_human="date",
    )

    mock_reddit_client.get_post_thread.return_value = (
        RedditThread(post=old_post, comments=[stale_comment]),
        None,
    )

    result = await tools.extract_public_opinion("http://url")

    assert len(result.data) == 0


@pytest.mark.asyncio
async def test_fallback_analyze_niche_trends():
    # Simulate missing OAuth credentials (subclass of RedditClientError)
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.get_subreddit_trends.side_effect = RedditAuthRequiredError()

    result = await tools.analyze_niche_trends("python")

    # Trending tool should fail gracefully with a warning
    assert result.status == "warning"
    assert "Trending data is unavailable" in result.message
    assert len(result.data) == 0


@pytest.mark.asyncio
async def test_fallback_analyze_niche_trends_on_client_error():
    mock_reddit = DependencyContainer.get_reddit_client()
    mock_reddit.get_subreddit_trends.side_effect = RedditClientError("HTTP 401")

    result = await tools.analyze_niche_trends("python")

    assert result.status == "warning"
    assert "Trending data is unavailable" in result.message
    assert len(result.data) == 0


@pytest.mark.asyncio
async def test_container_lazy_init_and_reset():
    DependencyContainer.reset()
    try:
        assert DependencyContainer.is_initialized() is False

        client = DependencyContainer.get_reddit_client()
        assert isinstance(client, RedditClient)
        assert DependencyContainer.is_initialized() is True

        # aclose() closes the shared HTTP client while the container owns it
        await DependencyContainer.aclose()
        assert DependencyContainer.is_initialized() is False
    finally:
        DependencyContainer.reset()


@pytest.mark.asyncio
async def test_container_init_failure_clears_partial_state(monkeypatch):
    # If any constructor fails, no partially-initialized state survives and
    # the already-constructed HTTP client is closed
    closed = []

    class FakeHTTPClient:
        def __init__(self, *args, **kwargs):
            pass

        async def close(self):
            closed.append(True)

    def boom(*args, **kwargs):
        raise RuntimeError("construction failed")

    monkeypatch.setattr(
        "reddit_mcp.application.tools.ResilientHTTPClient", FakeHTTPClient
    )
    monkeypatch.setattr("reddit_mcp.application.tools.ArcticShiftClient", boom)
    DependencyContainer.reset()
    try:
        with pytest.raises(RuntimeError, match="construction failed"):
            DependencyContainer.get_reddit_client()

        await asyncio.sleep(0)  # let the scheduled close task run

        assert closed == [True]
        assert DependencyContainer.is_initialized() is False
        assert DependencyContainer._reddit_client is None
        assert DependencyContainer._arctic_shift_client is None
        assert DependencyContainer._search_provider is None
    finally:
        DependencyContainer.reset()


@pytest.mark.asyncio
async def test_tool_schema_bounds():
    from reddit_mcp.interface.server import create_server

    mcp = create_server()

    search_tool = await mcp.get_tool("search_knowledge")
    limit_schema = search_tool.parameters["properties"]["limit"]
    assert limit_schema["minimum"] == 1
    assert limit_schema["maximum"] == 100

    opinion_tool = await mcp.get_tool("extract_public_opinion")
    max_comments_schema = opinion_tool.parameters["properties"]["max_comments"]
    assert max_comments_schema["minimum"] == 1
    assert max_comments_schema["maximum"] == 100

    saved_tool = await mcp.get_tool("get_saved_posts")
    assert saved_tool is not None
    saved_limit_schema = saved_tool.parameters["properties"]["limit"]
    assert saved_limit_schema["minimum"] == 1
    assert saved_limit_schema["maximum"] == 100


def _make_saved_post(pid: str, title: str | None = None) -> RedditPost:
    return RedditPost(
        id=pid,
        title=title if title is not None else f"Saved post title {pid}",
        subreddit="test",
        score=0,
        upvote_ratio=0.0,
        num_comments=0,
        url=f"https://reddit.com/r/test/comments/{pid}",
        age_in_days=3,
        created_at_human="August 15, 2026",
        text_preview="Saved preview",
    )


@pytest.mark.asyncio
async def test_get_saved_posts_success_applies_quality_filter_and_provenance():
    mock_feed = DependencyContainer.get_saved_feed_client()
    mock_feed.get_saved_posts.return_value = (
        [_make_saved_post("a"), _make_saved_post("b", title="Hi")],
        2,
    )
    mock_feed.MAX_FEED_ITEMS = 100

    result = await tools.get_saved_posts(time_filter="week")

    assert result.status == "success"
    assert [p.id for p in result.data] == ["a"]  # short title filtered
    assert result.data_source == "saved_rss"
    assert result.next_page_token is None
    assert "scores and comment counts" in result.message
    assert "2 saved comment(s)" in result.message
    assert "instruction_note" in result.meta_context.model_dump()
    # Full feed window requested so the filter can consider every entry;
    # the caller's limit applies after filtering.
    mock_feed.get_saved_posts.assert_awaited_with(
        time_filter="week", limit=mock_feed.MAX_FEED_ITEMS
    )


@pytest.mark.asyncio
async def test_get_saved_posts_short_title_does_not_starve_the_limit():
    # A short-title entry at the top of the feed must not push valid posts
    # out of the requested page.
    mock_feed = DependencyContainer.get_saved_feed_client()
    mock_feed.MAX_FEED_ITEMS = 100
    mock_feed.get_saved_posts.return_value = (
        [
            _make_saved_post("short", title="Hi"),
            _make_saved_post("v1"),
            _make_saved_post("v2"),
        ],
        0,
    )

    result = await tools.get_saved_posts(time_filter="week", limit=2)

    assert [p.id for p in result.data] == ["v1", "v2"]


@pytest.mark.asyncio
async def test_get_saved_posts_unconfigured_returns_warning():
    mock_feed = DependencyContainer.get_saved_feed_client()
    mock_feed.get_saved_posts.side_effect = SavedFeedNotConfiguredError("no url")

    result = await tools.get_saved_posts()

    assert result.status == "warning"
    assert result.data == []
    assert "REDDIT_SAVED_RSS_URL" in result.message


@pytest.mark.asyncio
async def test_get_saved_posts_feed_error_returns_degraded():
    mock_feed = DependencyContainer.get_saved_feed_client()
    mock_feed.get_saved_posts.side_effect = SavedFeedError("feed is down")

    result = await tools.get_saved_posts(time_filter="day")

    assert result.status == "degraded"
    assert result.data == []
    assert result.message == "feed is down"
