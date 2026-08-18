from unittest.mock import AsyncMock, MagicMock

import pytest

from reddit_mcp.domain.models import RedditThread
from reddit_mcp.infrastructure.reddit_client import RedditClient, RedditClientError


@pytest.fixture
def mock_http_client():
    client = MagicMock()
    client.auth_manager = MagicMock()
    client.auth_manager.has_credentials = True
    client.get = AsyncMock()
    return client


@pytest.fixture
def mock_search_provider():
    provider = MagicMock()
    provider.search = AsyncMock()
    return provider


@pytest.fixture
def reddit_client(mock_http_client, mock_search_provider):
    return RedditClient(
        http_client=mock_http_client, search_provider=mock_search_provider
    )


@pytest.mark.asyncio
async def test_get_subreddit_trends_success(reddit_client, mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": {
            "after": "t3_abc",
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": "123",
                        "title": "Test Post",
                        "subreddit": "test",
                        "score": 100,
                        "upvote_ratio": 0.95,
                        "num_comments": 10,
                        "permalink": "/r/test/comments/123/",
                        "created_utc": 1700000000.0,
                        "selftext": "Hello world text",
                    },
                }
            ],
        }
    }
    mock_http_client.get.return_value = mock_response

    posts, _ = await reddit_client.get_subreddit_trends("test", "hot")

    assert len(posts) == 1
    post = posts[0]
    assert post.age_in_days >= 0
    assert "created_at_human" in post.model_dump()
    assert post.text_preview == "Hello world text"


@pytest.mark.asyncio
async def test_get_post_thread_success(reddit_client, mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "123",
                            "title": "Post",
                            "subreddit": "test",
                            "score": 10,
                            "upvote_ratio": 1.0,
                            "num_comments": 1,
                            "permalink": "/r/test/comments/123/",
                            "created_utc": 1700000000.0,
                            "selftext": "...",
                        },
                    }
                ]
            }
        },
        {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c1",
                            "author": "user1",
                            "score": 5,
                            "body": "Comment body",
                            "created_utc": 1700000050.0,
                        },
                    }
                ]
            }
        },
    ]
    mock_http_client.get.return_value = mock_response

    thread, next_offset = await reddit_client.get_post_thread(
        "http://reddit.com/r/test/comments/123"
    )

    assert isinstance(thread, RedditThread)
    assert thread.comments[0].created_at_human is not None
    assert next_offset is None  # short stream, no continuation


def _thread_payload(children):
    return [
        {
            "data": {
                "children": [
                    {
                        "kind": "t3",
                        "data": {
                            "id": "123",
                            "title": "Post",
                            "subreddit": "test",
                            "score": 10,
                            "upvote_ratio": 1.0,
                            "num_comments": len(children),
                            "permalink": "/r/test/comments/123/",
                            "created_utc": 1700000000.0,
                            "selftext": "...",
                        },
                    }
                ]
            }
        },
        {"data": {"children": children}},
    ]


def _raw_comment(
    n: int, body: str | None = "This is a sufficiently long comment body."
) -> dict:
    data = {
        "id": f"c{n}",
        "author": f"user{n}",
        "score": 5,
        "created_utc": 1700000050.0 + n,
    }
    if body is not None:
        data["body"] = f"{body} number {n}."
    return {"kind": "t1", "data": data}


@pytest.mark.asyncio
async def test_get_post_thread_comment_offset_paginates_raw_stream(
    reddit_client, mock_http_client
):
    children = [_raw_comment(n + 1) for n in range(5)]

    mock_response = MagicMock()
    mock_response.json.return_value = _thread_payload(children)
    mock_http_client.get.return_value = mock_response

    url = "http://reddit.com/r/test/comments/123"

    thread, next_cursor = await reddit_client.get_post_thread(url, max_comments=2)
    assert [c.id for c in thread.comments] == ["c1", "c2"]
    assert next_cursor == (2, "c2")

    thread, next_cursor = await reddit_client.get_post_thread(
        url, max_comments=2, comment_offset=2, after_comment_id="c2"
    )
    assert [c.id for c in thread.comments] == ["c3", "c4"]
    assert next_cursor == (4, "c4")


@pytest.mark.asyncio
async def test_get_post_thread_offset_counts_unmapped_raw_comments(
    reddit_client, mock_http_client
):
    # c1 fails mapping (empty body) but still consumes a raw-stream slot; the
    # cursor must reflect the raw count so page 2 has no duplicate.
    children = [
        _raw_comment(1, body=None),
        _raw_comment(2),
        _raw_comment(3),
        _raw_comment(4),
    ]

    mock_response = MagicMock()
    mock_response.json.return_value = _thread_payload(children)
    mock_http_client.get.return_value = mock_response

    url = "http://reddit.com/r/test/comments/123"

    page_one, next_cursor = await reddit_client.get_post_thread(url, max_comments=2)
    assert [c.id for c in page_one.comments] == ["c2", "c3"]
    assert next_cursor == (3, "c3")  # c1 was consumed even though it failed mapping

    page_two, next_cursor = await reddit_client.get_post_thread(
        url, max_comments=2, comment_offset=3, after_comment_id="c3"
    )
    assert [c.id for c in page_two.comments] == ["c4"]
    assert next_cursor is None
    assert not {c.id for c in page_one.comments} & {c.id for c in page_two.comments}


@pytest.mark.asyncio
async def test_get_post_thread_anchor_survives_tree_changes(
    reddit_client, mock_http_client
):
    # Live threads re-sort between requests; the cursor anchors to the last
    # served comment ID so insertions above the continuation point can
    # neither duplicate served comments nor skip unseen ones after it.
    url = "http://reddit.com/r/test/comments/123"
    mock_response = MagicMock()
    mock_http_client.get.return_value = mock_response

    mock_response.json.return_value = _thread_payload(
        [_raw_comment(1), _raw_comment(2), _raw_comment(3)]
    )
    thread, next_cursor = await reddit_client.get_post_thread(url, max_comments=2)
    assert [c.id for c in thread.comments] == ["c1", "c2"]
    assert next_cursor == (2, "c2")

    # A new comment (c0) is inserted above; c4 is appended below.
    mock_response.json.return_value = _thread_payload(
        [
            _raw_comment(0),
            _raw_comment(1),
            _raw_comment(2),
            _raw_comment(3),
            _raw_comment(4),
        ]
    )
    thread, next_cursor = await reddit_client.get_post_thread(
        url,
        max_comments=2,
        comment_offset=next_cursor[0],
        after_comment_id=next_cursor[1],
    )
    assert [c.id for c in thread.comments] == ["c3", "c4"]
    assert next_cursor == (5, "c4")

    # Reordering below the anchor (unseen comments swap places) serves the
    # new order with no duplicates and nothing after the anchor skipped;
    # insertions above the anchor are ignored, matching Reddit's own
    # cursor (`after`) semantics for items that re-sort above the cursor.
    mock_response.json.return_value = _thread_payload(
        [
            _raw_comment(0),
            _raw_comment(1),
            _raw_comment(2),
            _raw_comment(3),
            _raw_comment(4),
            _raw_comment(6),
            _raw_comment(5),
            _raw_comment(7),
        ]
    )
    thread, next_cursor = await reddit_client.get_post_thread(
        url, max_comments=2, comment_offset=5, after_comment_id="c4"
    )
    assert [c.id for c in thread.comments] == ["c6", "c5"]
    assert next_cursor == (7, "c5")


@pytest.mark.asyncio
async def test_get_post_thread_missing_anchor_raises(reddit_client, mock_http_client):
    # If heavy re-sorting pushed the anchor out of the fetched window, fail
    # loudly instead of silently serving a misaligned page. (Window is not
    # clamped here: 2 + 20 + 2 <= 100.)
    mock_response = MagicMock()
    mock_response.json.return_value = _thread_payload(
        [_raw_comment(1), _raw_comment(3)]
    )
    mock_http_client.get.return_value = mock_response

    with pytest.raises(RedditClientError, match="anchor"):
        await reddit_client.get_post_thread(
            "http://reddit.com/r/test/comments/123",
            max_comments=2,
            comment_offset=2,
            after_comment_id="c2",
        )


@pytest.mark.asyncio
async def test_get_post_thread_request_limit_is_capped_at_100(
    reddit_client, mock_http_client
):
    # Reddit never returns more than 100 items regardless of limit; the client
    # must clamp its request so a truncated window is detectable.
    mock_response = MagicMock()
    mock_response.json.return_value = _thread_payload([_raw_comment(1)])
    mock_http_client.get.return_value = mock_response

    url = "http://reddit.com/r/test/comments/123"

    await reddit_client.get_post_thread(url, max_comments=30, comment_offset=200)
    sent_limit = mock_http_client.get.await_args.kwargs["params"]["limit"]
    assert sent_limit == 100  # 30 + 20 + 200 would be 250

    await reddit_client.get_post_thread(url, max_comments=30, comment_offset=10)
    sent_limit = mock_http_client.get.await_args.kwargs["params"]["limit"]
    assert sent_limit == 60  # unclamped windows keep the exact buffer math


@pytest.mark.asyncio
async def test_get_post_thread_clamped_window_anchor_missing_is_bounded(
    reddit_client, mock_http_client
):
    # Deep continuation: the requested window (30 + 20 + 500) is truncated by
    # Reddit's 100-item cap, so the anchor can legitimately lie beyond the
    # fetched window. That is a bounded end-of-results, not a re-sort error.
    mock_response = MagicMock()
    mock_response.json.return_value = _thread_payload(
        [_raw_comment(1), _raw_comment(2)]
    )
    mock_http_client.get.return_value = mock_response

    thread, next_cursor = await reddit_client.get_post_thread(
        "http://reddit.com/r/test/comments/123",
        max_comments=30,
        comment_offset=500,
        after_comment_id="c500",
    )

    assert thread.comments == []
    assert next_cursor is None


@pytest.mark.asyncio
async def test_get_post_thread_malformed_json(reddit_client, mock_http_client):
    # Simulate reddit returning an unexpected structure (e.g., dict instead of list)
    mock_response = MagicMock()
    mock_response.json.return_value = {"error": 404}
    mock_http_client.get.return_value = mock_response

    with pytest.raises(RedditClientError, match="Unexpected response format"):
        await reddit_client.get_post_thread("http://reddit.com/r/test/comments/123")


from reddit_mcp.infrastructure.reddit_client import RedditAuthRequiredError


@pytest.mark.asyncio
async def test_reddit_client_auth_required_errors(reddit_client, mock_http_client):
    # Test that operations fail fast when credentials are missing
    mock_http_client.auth_manager.has_credentials = False

    with pytest.raises(RedditAuthRequiredError):
        await reddit_client.get_subreddit_trends("test")

    with pytest.raises(RedditAuthRequiredError):
        await reddit_client.get_post_thread("http://reddit.com/r/test/comments/123")

    with pytest.raises(RedditAuthRequiredError):
        await reddit_client.native_reddit_search("query")

    with pytest.raises(RedditAuthRequiredError):
        await reddit_client.search("query")
