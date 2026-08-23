import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from reddit_mcp.infrastructure.auth import RedditAuthManager
from reddit_mcp.infrastructure.http import RedditRateLimitError, ResilientHTTPClient


@pytest.fixture
def mock_auth_manager():
    manager = MagicMock(spec=RedditAuthManager)
    manager.has_credentials = True
    manager.get_token = AsyncMock(return_value="mock_token")
    manager.close = AsyncMock()
    return manager


@pytest.mark.asyncio
async def test_http_client_no_auth(mock_auth_manager):
    mock_auth_manager.has_credentials = False
    mock_auth_manager.get_token = AsyncMock(return_value=None)
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    client.client.get = AsyncMock(return_value=mock_response)

    await client.get("http://test.com")

    # Verify Authorization header was NOT included
    call_args = client.client.get.call_args[1]
    assert "Authorization" not in call_args["headers"]
    await client.close()


@pytest.mark.asyncio
async def test_http_client_success(mock_auth_manager):
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    client.client.get = AsyncMock(return_value=mock_response)

    response = await client.get("http://test.com")
    assert response.status_code == 200
    assert client.client.get.call_count == 1
    await client.close()


@pytest.mark.asyncio
async def test_http_client_429_retry_success(mock_auth_manager, monkeypatch):
    # Skip actual asyncio.sleep during tests to make them fast
    monkeypatch.setattr("asyncio.sleep", AsyncMock())

    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 429
    fail_response.headers = {"Retry-After": "1"}

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.raise_for_status = MagicMock()

    # First call returns 429, second call returns 200
    client.client.get = AsyncMock(side_effect=[fail_response, success_response])

    response = await client.get("http://test.com", max_retries=3)
    assert response.status_code == 200
    assert client.client.get.call_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_http_client_401_retry_success(mock_auth_manager, monkeypatch):
    # Skip actual asyncio.sleep during tests to make them fast
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 401

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.raise_for_status = MagicMock()

    # First call returns 401, second call returns 200
    client.client.get = AsyncMock(side_effect=[fail_response, success_response])

    response = await client.get("https://oauth.reddit.com/test.json")
    assert response.status_code == 200
    assert client.client.get.call_count == 2
    mock_auth_manager.invalidate.assert_called_once()
    await client.close()


@pytest.mark.asyncio
async def test_http_client_401_retry_exhausted(mock_auth_manager, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 401
    fail_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Unauthorized", request=MagicMock(), response=fail_response
    )

    # Both calls return 401
    client.client.get = AsyncMock(side_effect=[fail_response, fail_response])

    with pytest.raises(httpx.HTTPStatusError):
        await client.get("https://oauth.reddit.com/test.json")

    assert client.client.get.call_count == 2
    mock_auth_manager.invalidate.assert_called_once()
    await client.close()


@pytest.mark.asyncio
async def test_http_client_401_retry_with_max_retries_1(mock_auth_manager):
    # The token-refresh retry must not consume the general retry budget
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 401

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.raise_for_status = MagicMock()

    client.client.get = AsyncMock(side_effect=[fail_response, success_response])

    response = await client.get("https://oauth.reddit.com/test.json", max_retries=1)
    assert response.status_code == 200
    assert client.client.get.call_count == 2
    mock_auth_manager.invalidate.assert_called_once()
    await client.close()


@pytest.mark.asyncio
async def test_http_client_no_bearer_for_non_reddit_host(mock_auth_manager):
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    client.client.get = AsyncMock(return_value=mock_response)

    await client.get("https://arctic-shift.photon-reddit.com/api/posts/search")

    call_args = client.client.get.call_args[1]
    assert "Authorization" not in call_args["headers"]
    mock_auth_manager.get_token.assert_not_called()
    await client.close()


@pytest.mark.asyncio
async def test_http_client_401_non_reddit_host_no_invalidate(mock_auth_manager):
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 401
    fail_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Unauthorized", request=MagicMock(), response=fail_response
    )

    client.client.get = AsyncMock(return_value=fail_response)

    with pytest.raises(httpx.HTTPStatusError):
        await client.get("https://arctic-shift.photon-reddit.com/api/posts/search")

    assert client.client.get.call_count == 1
    mock_auth_manager.invalidate.assert_not_called()
    await client.close()


@pytest.mark.asyncio
async def test_http_client_429_max_retries(mock_auth_manager, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 429
    fail_response.headers = {"Retry-After": "1"}

    # Always return 429
    client.client.get = AsyncMock(return_value=fail_response)

    with pytest.raises(RedditRateLimitError, match="Max retries exceeded"):
        await client.get("http://test.com", max_retries=2)

    assert client.client.get.call_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_http_client_5xx_retry_success(mock_auth_manager, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 503
    fail_response.headers = {}

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.raise_for_status = MagicMock()

    # First call returns 503, second call returns 200
    client.client.get = AsyncMock(side_effect=[fail_response, success_response])

    response = await client.get("http://test.com")
    assert response.status_code == 200
    assert client.client.get.call_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_http_client_5xx_max_retries_raises(mock_auth_manager, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 503
    fail_response.headers = {}
    fail_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Service Unavailable", request=MagicMock(), response=fail_response
    )

    # Always return 503
    client.client.get = AsyncMock(return_value=fail_response)

    with pytest.raises(httpx.HTTPStatusError):
        await client.get("http://test.com")

    assert client.client.get.call_count == 2
    await client.close()


@pytest.mark.asyncio
async def test_http_client_retry_after_capped(mock_auth_manager, monkeypatch):
    sleep_mock = AsyncMock()
    monkeypatch.setattr("asyncio.sleep", sleep_mock)
    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    fail_response = MagicMock()
    fail_response.status_code = 429
    fail_response.headers = {"Retry-After": "900"}

    success_response = MagicMock()
    success_response.status_code = 200
    success_response.raise_for_status = MagicMock()

    # First call returns 429 with a huge Retry-After, second returns 200
    client.client.get = AsyncMock(side_effect=[fail_response, success_response])

    response = await client.get("http://test.com")
    assert response.status_code == 200
    assert client.client.get.call_count == 2

    slept = [call.args[0] for call in sleep_mock.call_args_list]
    assert slept
    sleep_mock.assert_awaited_once_with(ResilientHTTPClient.MAX_RETRY_AFTER_SECONDS)
    await client.close()


@pytest.mark.asyncio
async def test_http_client_total_budget_deadline(mock_auth_manager, monkeypatch):
    # The aggregate deadline must expire even though each individual
    # operation would succeed in isolation
    monkeypatch.setattr(ResilientHTTPClient, "TOTAL_BUDGET_SECONDS", 0.05)

    client = ResilientHTTPClient(auth_manager=mock_auth_manager, user_agent="test")

    async def slow_get(*args, **kwargs):
        import asyncio as real_asyncio

        await real_asyncio.sleep(0.5)
        raise AssertionError("should have been cancelled before completing")

    client.client.get = slow_get

    with pytest.raises(TimeoutError):
        await client.get("https://oauth.reddit.com/test.json")
    await client.close()


@pytest.mark.asyncio
async def test_token_bucket_rate_limiter():
    from reddit_mcp.infrastructure.http import _TokenBucketRateLimiter

    limiter = _TokenBucketRateLimiter(rate_limit_per_minute=60)
    assert limiter.tokens == 60.0

    # Consume 1 token
    await limiter.acquire()
    assert limiter.tokens < 60.0


@pytest.mark.asyncio
async def test_http_client_concurrency_semaphore(mock_auth_manager):
    client = ResilientHTTPClient(
        auth_manager=mock_auth_manager,
        user_agent="test",
        max_concurrency=2,
        rate_limit_per_minute=120,
    )

    in_flight = 0
    max_seen = 0

    async def mock_get(*args, **kwargs):
        nonlocal in_flight, max_seen
        in_flight += 1
        max_seen = max(max_seen, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        mock_res = MagicMock()
        mock_res.status_code = 200
        return mock_res

    client.client.get = mock_get

    # Fire 6 concurrent requests
    await asyncio.gather(*[client.get("http://test.com") for _ in range(6)])

    # Max simultaneous requests should never exceed max_concurrency=2
    assert max_seen <= 2
    await client.close()


@pytest.mark.asyncio
async def test_http_client_cookie_auth_success(mock_auth_manager):
    client = ResilientHTTPClient(
        auth_manager=mock_auth_manager,
        user_agent="test",
        session_cookie="dummy_cookie_val",
    )

    mock_curl = MagicMock()
    mock_res = MagicMock()
    mock_res.status_code = 200
    mock_res.text = '{"data": {"children": []}}'
    mock_res.json.return_value = {"data": {"children": []}}
    mock_curl.get = AsyncMock(return_value=mock_res)
    mock_curl.close = AsyncMock()
    client._curl_session = mock_curl

    result = await client.get_public_web("https://www.reddit.com/r/saas/rising.json")

    assert result == {"data": {"children": []}}
    call_kwargs = mock_curl.get.call_args[1]
    assert "Cookie" in call_kwargs["headers"]
    assert call_kwargs["headers"]["Cookie"] == "reddit_session=dummy_cookie_val"
    await client.close()
