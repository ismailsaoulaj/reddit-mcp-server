from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from reddit_mcp.domain.models import RedditThread
from reddit_mcp.infrastructure.arctic_shift_client import (
    ArcticShiftClient,
    ArcticShiftError,
)


@pytest.fixture
def mock_http_client():
    client = MagicMock()
    client.get = AsyncMock()
    return client


@pytest.fixture
def arctic_client(mock_http_client):
    return ArcticShiftClient(http_client=mock_http_client)


@pytest.mark.asyncio
async def test_get_posts_by_ids_success(arctic_client, mock_http_client):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "data": [
            {
                "id": "abc",
                "title": "Arctic Post",
                "subreddit": "test",
                "score": 50,
                "upvote_ratio": 0.9,
                "num_comments": 5,
                "permalink": "/r/test/comments/abc/",
                "created_utc": 1700000000.0,
                "selftext": "Archive body",
            }
        ]
    }
    mock_http_client.get.return_value = mock_response

    posts = await arctic_client.get_posts_by_ids(["t3_abc"])

    assert len(posts) == 1
    assert posts[0].id == "abc"
    assert posts[0].title == "Arctic Post"


@pytest.mark.asyncio
async def test_get_posts_by_ids_empty_ids_returns_empty(
    arctic_client, mock_http_client
):
    posts = await arctic_client.get_posts_by_ids([])

    assert posts == []
    mock_http_client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_posts_by_ids_http_error_raises(arctic_client, mock_http_client):
    mock_http_client.get = AsyncMock(side_effect=httpx.HTTPError("boom"))

    with pytest.raises(ArcticShiftError, match="posts fetch failed"):
        await arctic_client.get_posts_by_ids(["t3_abc"])


@pytest.mark.asyncio
async def test_get_post_thread_success(arctic_client, mock_http_client):
    # Mocking two HTTP calls: first for post, second for comments
    post_response = MagicMock()
    post_response.json.return_value = {
        "data": [
            {
                "id": "abc",
                "title": "Post",
                "subreddit": "test",
                "created_utc": 1700000000.0,
            }
        ]
    }

    comment_response = MagicMock()
    comment_response.json.return_value = {
        "data": [
            {
                "id": "c1",
                "author": "user",
                "score": 10,
                "body": "Comment body",
                "created_utc": 1700000050.0,
            }
        ]
    }

    mock_http_client.get.side_effect = [post_response, comment_response]

    thread, next_offset = await arctic_client.get_post_thread("abc")

    assert isinstance(thread, RedditThread)
    assert thread.post.id == "abc"
    assert len(thread.comments) == 1
    assert thread.comments[0].id == "c1"
    assert next_offset is None  # fewer comments than a full page


@pytest.mark.asyncio
async def test_get_post_thread_oversamples_filters_and_sorts(
    arctic_client, mock_http_client
):
    post_response = MagicMock()
    post_response.json.return_value = {
        "data": [
            {
                "id": "abc",
                "title": "Post",
                "subreddit": "test",
                "created_utc": 1700000000.0,
            }
        ]
    }

    comment_response = MagicMock()
    comment_response.json.return_value = {
        "data": [
            {
                "id": "c1",
                "author": "user1",
                "score": 1,
                "body": "Low score",
                "created_utc": 1700000050.0,
            },
            {
                "id": "c2",
                "author": "user2",
                "score": 10,
                "body": "High score",
                "created_utc": 1700000051.0,
            },
            {
                "id": "c3",
                "author": "user3",
                "score": 3,
                "body": "Mid score",
                "created_utc": 1700000052.0,
            },
            {
                "id": "c4",
                "author": "user4",
                "score": 99,
                "body": "[deleted]",
                "created_utc": 1700000053.0,
            },
            {
                "id": "c5",
                "author": "user5",
                "score": 98,
                "body": "[removed]",
                "created_utc": 1700000054.0,
            },
        ]
    }

    mock_http_client.get.side_effect = [post_response, comment_response]

    thread, next_offset = await arctic_client.get_post_thread("abc", max_comments=2)

    assert len(thread.comments) == 2
    assert [c.id for c in thread.comments] == ["c2", "c3"]
    assert [c.score for c in thread.comments] == [10, 3]
    assert all("[deleted]" not in c.body for c in thread.comments)
    assert all("[removed]" not in c.body for c in thread.comments)
    assert next_offset == 2  # full page within the sorted list

    comments_call = mock_http_client.get.await_args_list[1]
    assert comments_call.kwargs["params"]["limit"] == 6


@pytest.mark.asyncio
async def test_get_post_thread_comment_offset_slices_sorted_comments(
    arctic_client, mock_http_client
):
    post_response = MagicMock()
    post_response.json.return_value = {
        "data": [
            {
                "id": "abc",
                "title": "Post",
                "subreddit": "test",
                "created_utc": 1700000000.0,
            }
        ]
    }

    scores = [10, 8, 6, 4, 2]
    comment_response = MagicMock()
    comment_response.json.return_value = {
        "data": [
            {
                "id": f"c{i}",
                "author": f"user{i}",
                "score": score,
                "body": f"Comment with score {score} and a long enough body.",
                "created_utc": 1700000050.0 + i,
            }
            for i, score in enumerate(scores)
        ]
    }

    mock_http_client.get.side_effect = [post_response, comment_response]

    thread, next_offset = await arctic_client.get_post_thread(
        "abc", max_comments=2, comment_offset=1
    )

    assert [c.id for c in thread.comments] == ["c1", "c2"]
    assert [c.score for c in thread.comments] == [8, 6]
    assert next_offset == 3

    comments_call = mock_http_client.get.await_args_list[1]
    assert comments_call.kwargs["params"]["limit"] == 9


@pytest.mark.asyncio
async def test_get_subreddit_trends_raises_error(arctic_client):
    # Ensure it refuses to fetch trending data
    with pytest.raises(ArcticShiftError, match="unavailable without OAuth"):
        await arctic_client.get_subreddit_trends("python")
