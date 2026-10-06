import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.methods import SendMessage
from aiogram.types import Chat, User
from openai import APIStatusError, APITimeoutError, AuthenticationError, RateLimitError

from app.config import ConfigurationError
from app.services.daily_digest import (
    MAX_ATTEMPTS,
    RETRY_SECONDS,
    DailyDigestService,
    day_bounds,
    select_digest_messages,
)
from app.services.local_history import LocalHistoryService
from app.services.openai_service import ModelRateLimited, ModelUnavailable, OpenAIService
from app.utils.reply_context import ContextMessage, ReplyContext
from tests.conftest import make_message
from tests.test_openai_service import api_error

DAY = date(2026, 10, 4)
STAMP = datetime(2026, 10, 4, 12, tzinfo=UTC)
NOW = datetime(2026, 10, 5, 10, tzinfo=UTC)
CHAT = -100123


@pytest.fixture
async def digest(local_history):
    settings = replace(
        local_history.settings,
        daily_digest_enabled=True,
        daily_digest_chat_ids=frozenset({CHAT}),
        daily_digest_timezone="UTC",
    )
    local_history.settings = settings
    await local_history.save_message(
        make_message("Discussed yesterday", date=STAMP, entities=False)
    )
    counter = 100

    async def send(**kwargs):
        nonlocal counter
        counter += 1
        return make_message(
            kwargs["text"],
            message_id=counter,
            date=NOW,
            chat=Chat(id=kwargs["chat_id"], type="supergroup", is_forum=True),
            from_user=User(id=123456, is_bot=True, first_name="Our bot"),
            entities=False,
        )

    bot = SimpleNamespace(send_chat_action=AsyncMock(), send_message=AsyncMock(side_effect=send))
    model = SimpleNamespace(generate_daily_digest=AsyncMock(return_value="Summary"))
    service = DailyDigestService(settings, bot, local_history, model)
    service._now = lambda: NOW
    yield service
    await service.shutdown()


async def state(digest):
    rows = await digest.history._db().execute_fetchall("SELECT * FROM daily_digests")
    return dict(rows[0]) if rows else None


def advance(digest, seconds=RETRY_SECONDS + 1):
    now = digest._now()
    digest._now = lambda: now + timedelta(seconds=seconds)


async def test_success_cached_once_and_restart_has_no_duplicate(digest):
    await digest.run_for_date(DAY)
    digest.model.generate_daily_digest.assert_awaited_once()
    digest.bot.send_message.assert_awaited_once()
    assert (await state(digest))["status"] == "sent"
    cached = await digest.history.get_recent_messages(CHAT, 102, 50, message_thread_id=1)
    assert cached[0].is_current_bot and "Summary" in cached[0].text
    settings = digest.settings
    await digest.history.close()
    history = LocalHistoryService(settings, 123456)
    await history.start()
    try:
        restarted = DailyDigestService(settings, digest.bot, history, digest.model)
        restarted._now = digest._now
        await restarted.run_for_date(DAY)
        assert digest.bot.send_message.await_count == 1
        assert digest.model.generate_daily_digest.await_count == 1
    finally:
        await history.close()


@pytest.mark.parametrize("gate", ["allowlist", "opt_in", "disabled", "history_disabled"])
async def test_old_cached_chat_not_allowed_never_read_or_sent(digest, gate):
    changes = {
        "allowlist": {"allowed_chat_ids": frozenset({-999})},
        "opt_in": {"daily_digest_chat_ids": frozenset({-999})},
        "disabled": {"daily_digest_enabled": False},
        "history_disabled": {"daily_digest_enabled": False, "local_history_enabled": False},
    }[gate]
    settings = replace(digest.settings, **changes)
    digest.settings = digest.history.settings = settings
    start, end = day_bounds(DAY, ZoneInfo("UTC"))
    assert CHAT not in await digest.history.get_daily_chat_ids(start, end)
    assert not await digest.history.get_daily_messages(CHAT, start, end)
    original = digest.history.get_daily_messages
    digest.history.get_daily_messages = AsyncMock(wraps=original)
    await digest.run_for_date(DAY)
    assert all(call.args[0] != CHAT for call in digest.history.get_daily_messages.call_args_list)
    digest.model.generate_daily_digest.assert_not_awaited()
    digest.bot.send_message.assert_not_awaited()


async def test_generation_failure_retried_after_restart_and_delay(digest):
    digest.model.generate_daily_digest.side_effect = ModelUnavailable()
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "retry"
    await digest.history.close()
    digest.history = LocalHistoryService(digest.settings, 123456)
    await digest.history.start()
    try:
        await digest.run_for_date(DAY)
        assert digest.model.generate_daily_digest.await_count == 1
        digest.model.generate_daily_digest.side_effect = None
        advance(digest)
        await digest.run_for_date(DAY)
        assert digest.model.generate_daily_digest.await_count == 2
        assert (await state(digest))["status"] == "sent"
    finally:
        await digest.history.close()


async def test_generation_attempts_are_bounded(digest):
    digest.model.generate_daily_digest.side_effect = ModelUnavailable()
    for _ in range(MAX_ATTEMPTS + 2):
        await digest.run_for_date(DAY)
        advance(digest)
    assert digest.model.generate_daily_digest.await_count == MAX_ATTEMPTS
    digest.bot.send_message.assert_not_awaited()


async def test_database_failure_does_not_block_other_chat(digest):
    second = -456
    settings = replace(digest.settings, daily_digest_chat_ids=frozenset({CHAT, second}))
    digest.settings = digest.history.settings = settings
    await digest.history.save_message(make_message("Second chat", chat_id=second, date=STAMP))
    original = digest.history.claim_daily_digest

    async def claim(chat_id, *args, **kwargs):
        if chat_id == CHAT:
            raise RuntimeError("SECRET database error")
        return await original(chat_id, *args, **kwargs)

    digest.history.claim_daily_digest = claim
    await digest.run_for_date(DAY)
    assert digest.bot.send_message.call_args.kwargs["chat_id"] == second


async def test_scheduler_survives_exception_and_closes_cleanly(digest, monkeypatch, caplog):
    ticks = 0
    reached = asyncio.Event()

    async def tick():
        nonlocal ticks
        ticks += 1
        if ticks == 1:
            raise RuntimeError("SECRET transient database failure")
        reached.set()

    original_sleep = asyncio.sleep

    async def fast_sleep(_):
        await original_sleep(0)

    digest._tick = tick
    monkeypatch.setattr("app.services.daily_digest.asyncio.sleep", fast_sleep)
    await digest.start()
    await digest.start()
    await asyncio.wait_for(reached.wait(), 1)
    await digest.shutdown()
    assert ticks >= 2 and digest._task is None
    assert "SECRET" not in caplog.text
    assert "scheduler failed kind=RuntimeError" in caplog.text


async def test_known_send_rejection_reuses_persisted_model_output(digest):
    send = digest.bot.send_message.side_effect
    method = SendMessage(chat_id=CHAT, text="Test")
    digest.bot.send_message.side_effect = TelegramBadRequest(method=method, message="rejected")
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "retry"
    advance(digest)
    digest.bot.send_message.side_effect = send
    await digest.run_for_date(DAY)
    digest.model.generate_daily_digest.assert_awaited_once()
    assert (await state(digest))["status"] == "sent"


async def test_flood_wait_honors_retry_deadline(digest):
    method = SendMessage(chat_id=CHAT, text="Test")
    digest.bot.send_message.side_effect = TelegramRetryAfter(
        method=method, message="wait", retry_after=900
    )
    await digest.run_for_date(DAY)
    advance(digest)
    await digest.run_for_date(DAY)
    assert digest.bot.send_message.await_count == 1
    assert (await state(digest))["retry_at"] == int(NOW.timestamp()) + 900


@pytest.mark.parametrize("error", [TelegramNetworkError, TelegramServerError, TimeoutError])
async def test_ambiguous_send_is_not_automatically_duplicated(digest, error):
    digest.bot.send_message.side_effect = (
        TimeoutError()
        if error is TimeoutError
        else error(method=SendMessage(chat_id=CHAT, text="Test"), message="Lost acknowledgement")
    )
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "uncertain"
    advance(digest, 3600)
    await digest.run_for_date(DAY)
    assert digest.bot.send_message.await_count == 1
    digest.model.generate_daily_digest.assert_awaited_once()


async def test_typing_failure_does_not_abort_digest(digest):
    digest.bot.send_chat_action.side_effect = TelegramNetworkError(
        method=SendMessage(chat_id=CHAT, text="Test"), message="typing failed"
    )
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "sent"


async def test_cached_partial_send_resumes_without_regenerating_or_resending(digest):
    digest.model.generate_daily_digest.return_value = "x" * 8000
    original_send = digest.bot.send_message.side_effect
    sent = []

    async def send(**kwargs):
        if sent:
            raise TelegramBadRequest(
                method=SendMessage(chat_id=CHAT, text="Test"), message="rejected"
            )
        message = await original_send(**kwargs)
        sent.append(message)
        return message

    digest.bot.send_message.side_effect = send
    await digest.run_for_date(DAY)
    first = await state(digest)
    assert first["part_index"] == 1 and first["status"] == "retry"
    advance(digest)
    digest.bot.send_message.side_effect = original_send
    await digest.run_for_date(DAY)
    digest.model.generate_daily_digest.assert_awaited_once()
    assert (await state(digest))["status"] == "sent"
    assert digest.bot.send_message.call_args.kwargs["text"] != sent[0].text


async def test_cache_write_failure_repaired_without_duplicate_send(digest):
    original_save = digest.history.save_message
    digest.history.save_message = AsyncMock(side_effect=RuntimeError("local write failure"))
    await digest.run_for_date(DAY)
    assert (await state(digest))["part_index"] == 1
    digest.history.save_message = original_save
    advance(digest)
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "sent"
    digest.bot.send_message.assert_awaited_once()
    digest.model.generate_daily_digest.assert_awaited_once()


async def test_failed_ack_storage_does_not_duplicate_actual_send(digest):
    original_update = digest.history.update_daily_digest

    async def update(*args, **kwargs):
        if kwargs.get("part_index"):
            raise RuntimeError("SQLite failure after delivery")
        return await original_update(*args, **kwargs)

    digest.history.update_daily_digest = update
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "uncertain"
    digest.history.update_daily_digest = original_update
    advance(digest, 3600)
    await digest.run_for_date(DAY)
    digest.bot.send_message.assert_awaited_once()


async def test_prepared_text_survives_eviction_and_restart(digest):
    original_send = digest.bot.send_message.side_effect
    digest.bot.send_message.side_effect = TelegramBadRequest(
        method=SendMessage(chat_id=CHAT, text="Test"), message="Rejected"
    )
    await digest.run_for_date(DAY)
    await digest.history._db().execute("DELETE FROM telegram_messages")
    await digest.history.close()
    digest.history = LocalHistoryService(digest.settings, 123456)
    await digest.history.start()
    try:
        digest.bot.send_message.side_effect = original_send
        advance(digest)
        await digest.run_for_date(DAY)
        assert (await state(digest))["status"] == "sent"
        digest.model.generate_daily_digest.assert_awaited_once()
    finally:
        await digest.history.close()


async def test_cached_and_prepared_output_redacts_configured_secrets(digest):
    secrets = [digest.settings.telegram_bot_token, digest.settings.openai_api_key]
    digest.model.generate_daily_digest.return_value = " ".join(secrets)
    await digest.run_for_date(DAY)
    record = await state(digest)
    sent = digest.bot.send_message.call_args.kwargs["text"]
    for secret in secrets:
        assert secret not in sent + record["payload"] + record["sent_messages"]
    assert "[REDACTED]" in sent


async def test_two_connections_cannot_claim_the_same_live_job(digest):
    second = LocalHistoryService(digest.settings, 123456)
    await second.start()
    try:
        claims = await asyncio.gather(
            *(
                history.claim_daily_digest(
                    CHAT, DAY.isoformat(), int(NOW.timestamp()), lease_seconds=180, max_attempts=3
                )
                for history in (digest.history, second)
            )
        )
        assert sum(claim is not None for claim in claims) == 1
        assert (await state(digest))["attempts"] == 1
    finally:
        await second.close()


async def test_storage_error_after_claim_recovers_on_expired_lease(digest):
    original_get = digest.history.get_daily_messages
    original_update = digest.history.update_daily_digest
    digest.history.get_daily_messages = AsyncMock(side_effect=RuntimeError("SQL read failure"))
    digest.history.update_daily_digest = AsyncMock(side_effect=RuntimeError("SQL write failure"))
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "preparing"
    digest.history.get_daily_messages = original_get
    digest.history.update_daily_digest = original_update
    advance(digest, 181)
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "sent"


async def test_claim_recovery_and_live_lease_exclusion(digest):
    now = int(NOW.timestamp())
    claim = await digest.history.claim_daily_digest(
        CHAT, DAY.isoformat(), now, lease_seconds=120, max_attempts=3
    )
    assert claim["status"] == "preparing"
    assert (
        await digest.history.claim_daily_digest(
            CHAT, DAY.isoformat(), now, lease_seconds=120, max_attempts=3
        )
        is None
    )
    advance(digest, 181)
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "sent"


async def test_cancel_during_send_recovers_as_uncertain_not_duplicate(digest):
    entered = asyncio.Event()

    async def send(**kwargs):
        entered.set()
        await asyncio.Event().wait()

    digest.bot.send_message.side_effect = send
    task = asyncio.create_task(digest.run_for_date(DAY))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await state(digest))["status"] == "sending"
    advance(digest, 3600)
    await digest.run_for_date(DAY)
    assert (await state(digest))["status"] == "uncertain"
    digest.bot.send_message.assert_awaited_once()


async def test_empty_history_has_no_ai_and_no_send(digest):
    await digest.run_for_date(DAY - timedelta(days=1))
    assert (await state(digest))["status"] == "empty"
    digest.model.generate_daily_digest.assert_not_awaited()
    digest.bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("hour,calls", [(8, 0), (9, 1), (23, 1)])
async def test_schedule_only_after_configured_hour(digest, hour, calls):
    digest._now = lambda: NOW.replace(hour=hour)
    digest.run_for_date = AsyncMock()
    await digest._tick()
    assert digest.run_for_date.await_count == calls
    if calls:
        digest.run_for_date.assert_awaited_once_with(DAY)


@pytest.mark.parametrize("day,hours", [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)])
def test_local_day_bounds_handle_dst(day, hours):
    start, end = day_bounds(day, ZoneInfo("Europe/Berlin"))
    assert end - start == hours * 3600


async def test_topic_labels_and_no_internal_ids_in_model_context(digest):
    for topic in (99991, 99992):
        await digest.history.save_message(
            make_message(
                "Topic text",
                message_id=topic,
                date=STAMP,
                is_topic_message=True,
                message_thread_id=topic,
                chat=Chat(id=CHAT, type="supergroup", is_forum=True),
            )
        )
    await digest.run_for_date(DAY)
    context = digest.model.generate_daily_digest.call_args.args[1]
    assert "[Conversation topic 1]" in context.messages[1].text
    assert "[Conversation topic 2]" in context.messages[2].text
    assert all("99991" not in m.text and "99992" not in m.text for m in context.messages)


def test_selection_keeps_answer_before_older_question():
    def message(i, text):
        return ContextMessage(i, 1, "A", None, STAMP, text)

    messages = (
        message(1, "Переходим на PostgreSQL?"),
        message(2, "Да, согласовано."),
        message(3, "x" * 250),
    )
    context = select_digest_messages(messages, 600)
    ids = [m.message_id for m in context.messages]
    assert 3 in ids
    assert 1 not in ids or 2 in ids


async def test_sdk_budget_roles_and_untrusted_instructions(settings, sdk):
    context = ReplyContext((ContextMessage(1, 1, "A", None, STAMP, "x" * 500),))
    model = OpenAIService(settings, sdk)
    await model.generate_daily_digest(
        "Summarize", context, context_chars=500, max_output_tokens=600
    )
    request = sdk.responses.create.call_args.kwargs
    assert sum(len(item["content"]) for item in request["input"]) <= 500
    assert json.loads(request["input"][-1]["content"])["text"] == "Summarize"
    assert "untrusted" in request["instructions"] and request["store"] is False
    assert request["max_output_tokens"] == 600
    assert not {"tools", "conversation", "previous_response_id"} & request.keys()


async def test_tiny_context_budget_does_not_call_sdk(settings, sdk):
    context = ReplyContext((ContextMessage(1, 1, "A", None, STAMP, "text"),))
    with pytest.raises(ValueError):
        await OpenAIService(settings, sdk).generate_daily_digest(
            "Summarize", context, context_chars=1, max_output_tokens=600
        )
    sdk.responses.create.assert_not_awaited()


@pytest.mark.parametrize(
    "error,expected",
    [
        (api_error(APITimeoutError), ModelUnavailable),
        (api_error(AuthenticationError, 401), ModelUnavailable),
        (api_error(RateLimitError, 429), ModelRateLimited),
        (api_error(APIStatusError, 503), ModelUnavailable),
    ],
)
async def test_digest_sdk_error_has_no_hidden_retry_or_secret_logs(
    settings, sdk, caplog, error, expected
):
    sdk.responses.create.side_effect = error
    context = ReplyContext((ContextMessage(1, 1, "A", None, STAMP, "text"),))
    with pytest.raises(expected):
        await OpenAIService(settings, sdk).generate_daily_digest(
            "Summarize", context, context_chars=1000, max_output_tokens=600
        )
    sdk.responses.create.assert_awaited_once()
    assert "SECRET" not in caplog.text


async def test_digest_timeout_includes_queue_shared_with_manual_requests(settings, sdk):
    entered, release = asyncio.Event(), asyncio.Event()

    async def answer(**kwargs):
        entered.set()
        await release.wait()
        return SimpleNamespace(output_text="Answer")

    sdk.responses.create.side_effect = answer
    service = OpenAIService(
        replace(settings, max_concurrent_requests=1, openai_timeout_seconds=1), sdk
    )
    manual = asyncio.create_task(service.generate("Manual"))
    await asyncio.wait_for(entered.wait(), 1)
    # The existing request keeps its original deadline; the digest times out waiting behind it.
    service.settings = replace(service.settings, openai_timeout_seconds=0.02)
    context = ReplyContext((ContextMessage(1, 1, "A", None, STAMP, "text"),))
    try:
        with pytest.raises(ModelUnavailable):
            await service.generate_daily_digest(
                "Summarize", context, context_chars=1000, max_output_tokens=600
            )
        sdk.responses.create.assert_awaited_once()
    finally:
        release.set()
        await manual


@pytest.mark.parametrize("timezone", ["", "Invalid/Timezone", "../UTC"])
def test_enabled_timezone_validation_is_safe(settings, timezone):
    with pytest.raises(ConfigurationError, match="TIMEZONE"):
        replace(
            settings,
            local_history_enabled=True,
            daily_digest_enabled=True,
            daily_digest_chat_ids=frozenset({CHAT}),
            daily_digest_timezone=timezone,
        )


def test_defaults_off_and_disabled_invalid_timezone_does_not_construct_zone(settings):
    assert not settings.daily_digest_enabled
    service = DailyDigestService(
        replace(settings, daily_digest_timezone="Invalid/Timezone"), None, None, None
    )
    assert service._timezone is None


def test_opt_in_and_history_are_required(settings):
    with pytest.raises(ConfigurationError, match="requires"):
        replace(settings, daily_digest_enabled=True)


@pytest.mark.parametrize("old_status", ["sent", "sending"])
async def test_old_schema_migrates_idempotently_and_preserves_delivery_state(
    settings, tmp_path, old_status
):
    import sqlite3

    path = tmp_path / "state" / "history.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE daily_digests(chat_id INTEGER,digest_date TEXT,status TEXT,"
            "message_id INTEGER,PRIMARY KEY(chat_id,digest_date))"
        )
        db.execute(
            "INSERT INTO daily_digests VALUES (?,?,?,100)", (CHAT, DAY.isoformat(), old_status)
        )
    settings = replace(
        settings,
        local_history_enabled=True,
        daily_digest_enabled=True,
        daily_digest_chat_ids=frozenset({CHAT}),
        local_history_db_path=str(path),
    )
    for _ in range(2):
        history = LocalHistoryService(settings, 123456)
        await history.start()
        try:
            assert (
                await history.claim_daily_digest(
                    CHAT, DAY.isoformat(), int(NOW.timestamp()), lease_seconds=120, max_attempts=3
                )
                is None
            )
            await history.prune_daily_digests("2026-09-01")
            rows = await history._db().execute_fetchall("SELECT * FROM daily_digests")
            assert len(rows) == 1
            assert rows[0]["status"] == ("sent" if old_status == "sent" else "uncertain")
            assert rows[0]["message_id"] == 100
        finally:
            await history.close()
