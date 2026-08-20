import os
from unittest.mock import patch

import httpx
import pytest

from reddit_mcp.infrastructure.auth import RedditAuthError, RedditAuthManager
from reddit_mcp.infrastructure.settings import get_settings


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "dummy_id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "dummy_secret")
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_auth_manager_missing_env_acquires_guest_token():
    # When credentials are missing, it acquires an anonymous guest token
    with patch.dict(os.environ, clear=True):
        get_settings.cache_clear()
        with patch("pathlib.Path.is_file", return_value=False):
            manager = RedditAuthManager(user_agent="test")
            assert manager.has_credentials is False
            assert manager.auth_mode == "guest_oauth"

            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "access_token": "guest_token_123",
                "expires_in": 3600,
            }
            with patch("httpx.AsyncClient.post", return_value=mock_response):
                token = await manager.get_token()
            assert token == "guest_token_123"


@pytest.mark.asyncio
async def test_auth_manager_missing_env_handles_guest_failure():
    with patch.dict(os.environ, clear=True):
        get_settings.cache_clear()
        with patch("pathlib.Path.is_file", return_value=False):
            manager = RedditAuthManager(user_agent="test")
            mock_response = MagicMock()
            mock_response.status_code = 403
            with patch("httpx.AsyncClient.post", return_value=mock_response):
                token = await manager.get_token()
            assert token is None


from unittest.mock import MagicMock


@pytest.mark.asyncio
async def test_auth_manager_success(auth_env):
    manager = RedditAuthManager(user_agent="test")
    assert manager.has_credentials is True

    mock_response = MagicMock()
    mock_response.json.return_value = {"access_token": "mock_token", "expires_in": 3600}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=mock_response):
        token = await manager.get_token()

    assert token == "mock_token"


@pytest.mark.asyncio
async def test_auth_manager_http_error(auth_env):
    manager = RedditAuthManager(user_agent="test")

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Unauthorized", request=MagicMock(), response=mock_response
    )

    with (
        patch("httpx.AsyncClient.post", return_value=mock_response),
        pytest.raises(RedditAuthError, match="HTTP 401"),
    ):
        await manager.get_token()


@pytest.mark.asyncio
async def test_auth_manager_invalidate_refetches_token(auth_env):
    manager = RedditAuthManager(user_agent="test")

    first_response = MagicMock()
    first_response.json.return_value = {"access_token": "token1", "expires_in": 3600}
    first_response.raise_for_status = MagicMock()

    second_response = MagicMock()
    second_response.json.return_value = {"access_token": "token2", "expires_in": 3600}
    second_response.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", return_value=first_response):
        token = await manager.get_token()
    assert token == "token1"

    manager.invalidate()

    with patch("httpx.AsyncClient.post", return_value=second_response):
        token = await manager.get_token()
    assert token == "token2"
