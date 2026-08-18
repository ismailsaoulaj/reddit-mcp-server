import asyncio
import json

import pytest

from reddit_mcp.application.utils import (
    build_meta_context,
    is_high_quality_comment,
    llm_timeout,
)
from reddit_mcp.domain.models import MetaContext

LONG_BODY = "This is a sufficiently long comment body that adds real substance."
BOT_PHRASE_BODY = "I am a bot, and this action was performed automatically. " * 2
AUTO_PHRASE_BODY = "This action was performed automatically by a moderator. " * 2


def test_build_meta_context_returns_model_instance():
    ctx = build_meta_context()

    assert isinstance(ctx, MetaContext)
    assert ctx.current_server_date
    assert "instruction_note" in ctx.model_dump()


@pytest.mark.asyncio
async def test_llm_timeout_dict_fallback_is_json_serializable(monkeypatch):
    async def mock_wait_for(aw, timeout=None, **kwargs):
        aw.close()
        raise TimeoutError()

    monkeypatch.setattr(asyncio, "wait_for", mock_wait_for)

    @llm_timeout(timeout_seconds=1)
    async def legacy_tool():
        return "never reached"

    result = await legacy_tool()

    assert isinstance(result, dict)
    # The raw-dict fallback (no response_model) must survive JSON encoding;
    # a MetaContext instance under meta_context previously broke json.dumps.
    decoded = json.loads(json.dumps(result))
    assert decoded["status"] == "partial_timeout"
    assert isinstance(decoded["meta_context"]["current_server_date"], str)


def test_high_quality_comment_passes():
    assert is_high_quality_comment(author="human_user", body=LONG_BODY, score=10)


def test_short_body_filtered():
    assert not is_high_quality_comment(author="human_user", body="too short", score=10)


def test_automoderator_author_filtered():
    assert not is_high_quality_comment(author="AutoModerator", body=LONG_BODY, score=10)


def test_bot_suffix_author_filtered():
    assert not is_high_quality_comment(author="user_bot", body=LONG_BODY, score=10)


def test_bot_hyphen_suffix_author_filtered():
    assert not is_high_quality_comment(author="user-bot", body=LONG_BODY, score=10)


def test_bot_substring_in_legitimate_username_passes():
    assert is_high_quality_comment(author="robotics_fan", body=LONG_BODY, score=10)


def test_bottlerocket_username_passes():
    assert is_high_quality_comment(author="BottleRocket", body=LONG_BODY, score=10)


def test_abbotsford_resident_username_passes():
    assert is_high_quality_comment(
        author="abbotsford_resident", body=LONG_BODY, score=10
    )


def test_bot_phrase_body_filtered():
    assert not is_high_quality_comment(
        author="human_user", body=BOT_PHRASE_BODY, score=10
    )


def test_automated_action_phrase_body_filtered():
    assert not is_high_quality_comment(
        author="human_user", body=AUTO_PHRASE_BODY, score=10
    )


def test_young_thread_score_one_passes_age_zero():
    assert is_high_quality_comment(
        author="human_user", body=LONG_BODY, score=1, thread_age_in_days=0
    )


def test_young_thread_score_one_passes_age_one():
    assert is_high_quality_comment(
        author="human_user", body=LONG_BODY, score=1, thread_age_in_days=1
    )


def test_young_thread_score_one_passes_age_two():
    assert is_high_quality_comment(
        author="human_user", body=LONG_BODY, score=1, thread_age_in_days=2
    )


def test_old_thread_score_one_filtered():
    assert not is_high_quality_comment(
        author="human_user", body=LONG_BODY, score=1, thread_age_in_days=3
    )


def test_no_thread_age_score_one_filtered():
    assert not is_high_quality_comment(author="human_user", body=LONG_BODY, score=1)


def test_no_thread_age_score_two_passes():
    assert is_high_quality_comment(author="human_user", body=LONG_BODY, score=2)


def test_young_thread_min_score_below_one_wins():
    assert is_high_quality_comment(
        author="human_user",
        body=LONG_BODY,
        score=0,
        min_score=0,
        thread_age_in_days=1,
    )


def test_young_thread_explicit_min_score_floored_at_one():
    assert not is_high_quality_comment(
        author="human_user",
        body=LONG_BODY,
        score=0,
        min_score=5,
        thread_age_in_days=1,
    )
    assert is_high_quality_comment(
        author="human_user",
        body=LONG_BODY,
        score=1,
        min_score=5,
        thread_age_in_days=1,
    )


def test_empty_body_filtered():
    assert not is_high_quality_comment(author="human_user", body="", score=10)


def test_empty_author_filtered():
    assert not is_high_quality_comment(author="", body=LONG_BODY, score=10)
