import asyncio
import functools
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import BaseModel

from reddit_mcp.domain.models import MetaContext

logger = logging.getLogger(__name__)

_KNOWN_BOT_AUTHORS = {"automoderator"}
_BOT_NAME_SUFFIXES = ("_bot", "-bot", "bot_")


def build_meta_context() -> MetaContext:
    """Builds a rich temporal and operational context for the AI."""
    now = datetime.now(UTC)
    return MetaContext(
        current_server_date=now.strftime("%A, %B %d, %Y"),
        instruction_note=(
            "1. Use age_in_days for freshness analysis. 2. Use comment_url for citations. "
            "3. If next_page_token is present, you can request the next page. "
            "4. Only high-quality data is returned."
        ),
    )


def is_high_quality_comment(
    author: str,
    body: str,
    score: int,
    min_score: int = 2,
    min_length: int = 40,
    thread_age_in_days: int | None = None,
) -> bool:
    """Smart heuristics to filter out bots, low-effort replies, and heavily downvoted opinions.

    Args:
        thread_age_in_days: Optional age of the thread in days. For young threads
            (<= 2 days), the effective minimum score drops to 1 (or min_score if
            explicitly passed lower than 1), since fresh/rising threads rarely
            have comments above 1 point yet. When None (default), min_score
            applies unchanged.
    """
    if not body or not author:
        return False

    author_lower = author.lower()
    if author_lower in _KNOWN_BOT_AUTHORS or author_lower.endswith(_BOT_NAME_SUFFIXES):
        return False

    if (
        "i am a bot" in body.lower()
        or "action was performed automatically" in body.lower()
    ):
        return False

    if len(body.strip()) < min_length:
        return False

    effective_min_score = min_score
    if thread_age_in_days is not None and thread_age_in_days <= 2:
        effective_min_score = min(min_score, 1)

    return score >= effective_min_score


def llm_timeout(
    timeout_seconds: int = 15, response_model: type[BaseModel] | None = None
):
    """
    Decorator that enforces a strict timeout on tool execution to prevent LLM client disconnects.
    Returns a graceful JSON fallback message for the LLM instead of throwing an unhandled exception.
    """

    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await asyncio.wait_for(
                    func(*args, **kwargs), timeout=timeout_seconds
                )
            except TimeoutError:
                logger.warning(
                    f"Tool {func.__name__} timed out after {timeout_seconds}s."
                )
                if response_model is not None:
                    return response_model(
                        meta_context=build_meta_context(),
                        data=[],
                        status="partial_timeout",
                        message=(
                            "Request paused to prevent timeout. "
                            "Use available data or retry."
                        ),
                    )
                return {
                    "meta_context": build_meta_context().model_dump(),
                    "data": [],
                    "next_page_token": None,
                    "status": "partial_timeout",
                    "message": "Request paused to prevent timeout. Use available data or retry.",
                }

        return wrapper

    return decorator
