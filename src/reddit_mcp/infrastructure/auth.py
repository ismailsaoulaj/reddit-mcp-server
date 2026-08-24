import asyncio
import base64
import logging
import time
import uuid

import httpx

from reddit_mcp.infrastructure.settings import get_settings

logger = logging.getLogger(__name__)


class RedditAuthError(Exception):
    """Exception raised for errors during Reddit authentication."""


class RedditAuthManager:
    """
    Manages OAuth 2.0 Access Tokens for Reddit via the client_credentials flow.
    Automatically fetches and caches the token, refreshing it before it expires.
    """

    def __init__(self, user_agent: str, client: httpx.AsyncClient | None = None):
        settings = get_settings()
        self.client_id = settings.reddit_client_id
        self.client_secret = settings.reddit_client_secret
        self.user_agent = user_agent
        self.device_id = uuid.uuid4().hex[:24]

        self._client: httpx.AsyncClient | None = client
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def close(self) -> None:
        """Close the underlying persistent HTTP client session."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    @property
    def has_credentials(self) -> bool:
        """Check if OAuth credentials are provided."""
        return bool(self.client_id and self.client_secret)

    @property
    def auth_mode(self) -> str:
        """Return the current authentication mode."""
        return "official_oauth" if self.has_credentials else "guest_oauth"

    async def get_token(self) -> str | None:
        """
        Get a valid access token (Official or Guest), refreshing it if necessary.
        """
        async with self._lock:
            # Refresh if token is missing or expires within the next 30 seconds
            if not self._token or time.time() >= (self._expires_at - 30):
                if self.has_credentials:
                    await self._refresh_official_token()
                else:
                    await self._refresh_guest_token()

            return self._token

    def invalidate(self) -> None:
        """Clear the cached token, forcing a refresh on the next get_token call."""
        self._token = None
        self._expires_at = 0.0

    async def _refresh_official_token(self) -> None:
        """
        Fetch a new token from Reddit API using client credentials.
        """
        logger.info("Fetching new official Reddit OAuth access token...")

        auth_string = f"{self.client_id}:{self.client_secret}"
        encoded_auth = base64.b64encode(auth_string.encode("utf-8")).decode("utf-8")

        headers = {
            "Authorization": f"Basic {encoded_auth}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {"grant_type": "client_credentials"}

        client = await self._ensure_client()
        try:
            response = await client.post(
                "https://www.reddit.com/api/v1/access_token",
                headers=headers,
                data=data,
            )
            response.raise_for_status()

            token_data = response.json()
            self._token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", 3600)

            if not self._token:
                raise RedditAuthError("Token response did not contain an access_token")

            self._expires_at = time.time() + expires_in
            logger.info("Successfully acquired new official Reddit OAuth access token.")

        except httpx.HTTPStatusError as e:
            logger.error(
                f"Failed to fetch official token. HTTP Status: {e.response.status_code}. Body: {e.response.text}"
            )
            raise RedditAuthError(
                f"HTTP {e.response.status_code} during token refresh"
            ) from e
        except httpx.RequestError as e:
            logger.error(f"Network error during official token refresh: {e}")
            raise RedditAuthError(f"Network error: {e}") from e
        except Exception as e:
            logger.error(f"Unexpected error during official token refresh: {e}")
            raise RedditAuthError(f"Unexpected error: {e}") from e

    async def _refresh_guest_token(self) -> None:
        """
        Fetch an anonymous guest/installed_client token if client_id is configured.
        """
        if not self.client_id:
            # Zero-config mode: public web/RSS endpoints are used directly.
            self._token = None
            self._expires_at = 0.0
            return

        logger.info("Acquiring anonymous Reddit Guest token...")
        encoded_auth = base64.b64encode(f"{self.client_id}:".encode()).decode("utf-8")

        headers = {
            "Authorization": f"Basic {encoded_auth}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        data = {
            "grant_type": "https://oauth.reddit.com/grants/installed_client",
            "device_id": self.device_id,
        }

        client = await self._ensure_client()
        try:
            response = await client.post(
                "https://www.reddit.com/api/v1/access_token",
                headers=headers,
                data=data,
            )
            if response.status_code == 200:
                token_data = response.json()
                self._token = token_data.get("access_token")
                expires_in = token_data.get("expires_in", 3600)
                if self._token:
                    self._expires_at = time.time() + expires_in
                    logger.info("Successfully acquired anonymous Reddit Guest token.")
                    return

            logger.warning(
                f"Guest token acquisition returned status {response.status_code}. "
                "Public web endpoints will be used as fallback."
            )
            self._token = None
            self._expires_at = 0.0
        except Exception as e:  # noqa: BLE001
            logger.warning(
                f"Could not acquire guest token ({e}). Falling back to public web."
            )
            self._token = None
            self._expires_at = 0.0
