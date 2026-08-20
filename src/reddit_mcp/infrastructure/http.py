import asyncio
import logging
import random
import time
from typing import Any

import httpx


class _TokenBucketRateLimiter:
    """Token bucket rate limiter to prevent burst traffic and enforce safe RPM limits."""

    def __init__(self, rate_limit_per_minute: int):
        self.capacity = float(rate_limit_per_minute)
        self.tokens = float(rate_limit_per_minute)
        self.fill_rate = float(rate_limit_per_minute) / 60.0  # tokens per second
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.last_update = now
            self.tokens = min(self.capacity, self.tokens + (elapsed * self.fill_rate))

            if self.tokens < 1.0:
                needed = 1.0 - self.tokens
                wait_time = needed / self.fill_rate
                # Add subtle jitter (10-50ms) to avoid robotic periodicity
                jitter = random.uniform(0.01, 0.05)
                await asyncio.sleep(wait_time + jitter)
                self.last_update = time.monotonic()
                self.tokens = 0.0
            else:
                self.tokens -= 1.0


try:
    from curl_cffi.requests import AsyncSession as CurlAsyncSession

    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False

from reddit_mcp.infrastructure.auth import RedditAuthManager

logger = logging.getLogger(__name__)


class RedditRateLimitError(Exception):
    """Exception raised when the maximum number of rate limit retries is exceeded."""


class ResilientHTTPClient:
    """
    HTTP client wrapper using httpx with built-in resilience.
    Automatically injects Reddit OAuth tokens, enforces User-Agent,
    and handles rate limits (429) using exponential backoff.

    Bearer tokens are only attached to Reddit API hosts, so shared use with
    third-party clients (e.g. Arctic Shift) never leaks credentials.
    """

    MAX_RETRY_AFTER_SECONDS = 5
    TOTAL_BUDGET_SECONDS = 14.0

    def __init__(
        self,
        auth_manager: RedditAuthManager,
        user_agent: str,
        session_cookie: str | None = None,
        max_concurrency: int = 4,
        rate_limit_per_minute: int = 40,
    ):
        self.auth_manager = auth_manager
        self.user_agent = user_agent
        self._session_cookie = session_cookie
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._rate_limiter = _TokenBucketRateLimiter(rate_limit_per_minute)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
        self._curl_session = None
        if CURL_CFFI_AVAILABLE:
            try:
                self._curl_session = CurlAsyncSession(impersonate="chrome131")
            except Exception:  # noqa: BLE001
                try:
                    self._curl_session = CurlAsyncSession(impersonate="chrome110")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Could not initialize curl_cffi session: {e}")
        if self._session_cookie:
            logger.info(
                "Cookie auth mode active. Using reddit_session cookie for requests."
            )

    async def close(self):
        """Close underlying HTTP clients."""
        await self.client.aclose()
        if self._curl_session is not None:
            try:
                await self._curl_session.close()
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Error closing curl session: {e}")

    @staticmethod
    def _get_browser_headers() -> dict[str, str]:
        """Generate authentic Chrome browser profile headers to bypass Fastly/Cloudflare WAF."""
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.reddit.com/",
            "Sec-Ch-Ua": '"Chromium";v="131", "Google Chrome";v="131", "Not-A.Brand";v="24"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }

    async def get_public_web(
        self, url: str, params: dict[str, Any] | None = None, max_retries: int = 2
    ) -> dict[str, Any]:
        """
        Fetch public JSON from www.reddit.com with browser impersonation and retries.
        Serves as Tier 2 direct fallback when OAuth/Guest API is unavailable.
        If a session cookie is configured, it is injected as Tier 1.5 before curl_cffi.
        """
        async with self._semaphore:
            await self._rate_limiter.acquire()
            target_url = url.replace(
                "https://oauth.reddit.com", "https://www.reddit.com"
            )
            target_url = target_url.replace(
                "https://old.reddit.com", "https://www.reddit.com"
            )

        # Tier 1.5: Cookie-based auth — bypasses WAF as a real logged-in browser session
        if self._session_cookie:
            try:
                headers = {
                    **self._get_browser_headers(),
                    "Cookie": f"reddit_session={self._session_cookie}",
                }
                if self._curl_session is not None:
                    res = await self._curl_session.get(
                        target_url, params=params, headers=headers, timeout=8.0
                    )
                    if res.status_code == 200:
                        text = res.text.strip()
                        if text.startswith(("{", "[")):
                            return res.json()
                        logger.warning(
                            f"Cookie auth returned non-JSON for {target_url} (session may be expired)."
                        )
                    else:
                        logger.warning(
                            f"Cookie auth HTTP {res.status_code} on {target_url}. Falling through."
                        )
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Cookie auth attempt failed for {target_url}: {e}")

        attempt = 0
        while attempt < max_retries:
            # 1. Try curl_cffi if available (bypasses bot protection via full browser emulation)
            if self._curl_session is not None:
                try:
                    headers = self._get_browser_headers()
                    res = await self._curl_session.get(
                        target_url, params=params, headers=headers, timeout=8.0
                    )
                    if res.status_code == 200:
                        text = res.text.strip()
                        if text.startswith(("{", "[")):
                            return res.json()
                        logger.warning(
                            f"curl_cffi received non-JSON response for {target_url} (status=200, likely login wall). Falling through to httpx."
                        )
                        # Do NOT retry curl_cffi — Reddit is serving HTML wall, drop to httpx fallback
                        break

                    if res.status_code == 403:
                        logger.warning(
                            f"curl_cffi HTTP 403 on {target_url}. Reddit blocked the request."
                        )
                        break

                    if res.status_code == 429 or res.status_code >= 500:
                        retry_after = (
                            res.headers.get("Retry-After")
                            if hasattr(res, "headers")
                            else None
                        )
                        wait_seconds = (
                            min(int(retry_after), self.MAX_RETRY_AFTER_SECONDS)
                            if (retry_after and retry_after.isdigit())
                            else (2**attempt)
                        )
                        logger.warning(
                            f"curl_cffi HTTP {res.status_code} on {target_url}. Retrying in {wait_seconds}s..."
                        )
                        await asyncio.sleep(wait_seconds)
                        attempt += 1
                        continue
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"curl_cffi public web attempt failed for {target_url}: {e}"
                    )

            # 2. Fallback to standard httpx client with browser-aligned headers
            try:
                headers = {
                    "User-Agent": self.user_agent,
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "en-US,en;q=0.9",
                }
                response = await self.client.get(
                    target_url, params=params, headers=headers, follow_redirects=True
                )
                if response.status_code == 200:
                    text = response.text.strip()
                    if text.startswith(("{", "[")):
                        return response.json()

                if response.status_code == 429 or response.status_code >= 500:
                    retry_after = response.headers.get("Retry-After")
                    wait_seconds = (
                        min(int(retry_after), self.MAX_RETRY_AFTER_SECONDS)
                        if (retry_after and retry_after.isdigit())
                        else (2**attempt)
                    )
                    logger.warning(
                        f"httpx public web HTTP {response.status_code} on {target_url}. Retrying in {wait_seconds}s..."
                    )
                    await asyncio.sleep(wait_seconds)
                    attempt += 1
                    continue

                response.raise_for_status()
            except httpx.RequestError as e:
                logger.warning(f"httpx public web network error on {target_url}: {e}")
                wait_seconds = 2**attempt
                await asyncio.sleep(wait_seconds)
                attempt += 1
                continue
            except Exception:
                raise

            attempt += 1

        raise RedditRateLimitError(
            f"Failed to fetch public web data after {max_retries} attempts: {target_url}"
        )

    async def get_public_text(
        self, url: str, params: dict[str, Any] | None = None
    ) -> str:
        """Fetch raw XML/text from public Reddit endpoints (such as public RSS feeds)."""
        async with self._semaphore:
            await self._rate_limiter.acquire()
            target_url = url.replace(
                "https://oauth.reddit.com", "https://www.reddit.com"
            )
            target_url = target_url.replace(
                "https://old.reddit.com", "https://www.reddit.com"
            )

        if self._curl_session is not None:
            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
                }
                res = await self._curl_session.get(
                    target_url, params=params, headers=headers, timeout=6.0
                )
                if res.status_code == 200 and "<feed" in res.text:
                    return res.text
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    f"curl_cffi public text fetch failed for {target_url}: {e}"
                )

        headers = {"User-Agent": self.user_agent}
        response = await self.client.get(
            target_url, params=params, headers=headers, follow_redirects=True
        )
        response.raise_for_status()
        return response.text

    @staticmethod
    def _is_reddit_url(url: str) -> bool:
        host = httpx.URL(url).host or ""
        return host == "reddit.com" or host.endswith(".reddit.com")

    async def get(
        self, url: str, params: dict[str, Any] | None = None, max_retries: int = 2
    ) -> httpx.Response:
        """
        Perform a GET request with automatic token injection and rate limit retries.
        The one-shot 401 token-refresh retry does not consume the retry budget.
        The whole flow is bounded by TOTAL_BUDGET_SECONDS; expiry raises
        TimeoutError (callers wrap it into their error handling).
        """
        is_reddit = self._is_reddit_url(url)
        attempt = 0
        auth_retry_done = False
        async with (
            self._semaphore,
            asyncio.timeout(self.TOTAL_BUDGET_SECONDS),
        ):
            await self._rate_limiter.acquire()
            while True:
                headers = {"User-Agent": self.user_agent}
                token = await self.auth_manager.get_token() if is_reddit else None

                if token:
                    headers["Authorization"] = f"Bearer {token}"

                try:
                    response = await self.client.get(
                        url, params=params, headers=headers
                    )

                    # Check for rate limit (429) or Server Errors (500, 502, 503, 504)
                    if response.status_code == 429 or response.status_code >= 500:
                        retry_after = response.headers.get("Retry-After")

                        if (
                            response.status_code == 429
                            and retry_after
                            and retry_after.isdigit()
                        ):
                            requested = int(retry_after)
                            wait_seconds = min(requested, self.MAX_RETRY_AFTER_SECONDS)
                            if requested > self.MAX_RETRY_AFTER_SECONDS:
                                logger.warning(
                                    f"Retry-After {requested}s exceeds cap of "
                                    f"{self.MAX_RETRY_AFTER_SECONDS}s; waiting "
                                    f"{wait_seconds}s instead."
                                )
                        else:
                            # Exponential backoff for 5xx or missing Retry-After
                            wait_seconds = 2**attempt

                        logger.warning(
                            f"HTTP {response.status_code} on {url}. Retrying in {wait_seconds} seconds. "
                            f"(Attempt {attempt + 1}/{max_retries})"
                        )

                        if attempt < max_retries - 1:
                            await asyncio.sleep(wait_seconds)
                            attempt += 1
                            continue
                        else:
                            if response.status_code == 429:
                                raise RedditRateLimitError(
                                    "Max retries exceeded due to rate limiting."
                                )
                            else:
                                response.raise_for_status()

                    # Stale token: invalidate and retry once with a fresh token.
                    # This retry is independent of the general retry budget.
                    if response.status_code == 401 and token and not auth_retry_done:
                        auth_retry_done = True
                        logger.warning(
                            f"HTTP 401 on {url}. Invalidating token and retrying."
                        )
                        self.auth_manager.invalidate()
                        continue

                    # Raise for other HTTP errors (4xx)
                    response.raise_for_status()
                    return response

                except httpx.HTTPStatusError:
                    # Handled retries above, raise if we get here
                    raise
                except httpx.RequestError as e:
                    logger.error(f"Network error on {url}: {e}")
                    if attempt < max_retries - 1:
                        wait_seconds = 2**attempt
                        await asyncio.sleep(wait_seconds)
                        attempt += 1
                        continue
                    raise
