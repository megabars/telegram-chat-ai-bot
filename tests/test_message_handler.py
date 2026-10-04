import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import Chat, Update, User

from app.handlers.messages import EMPTY_PROMPT, LOCAL_RATE_LIMIT
from app.utils.rate_limit import RateLimiter
from tests.conftest import make_message


@pytest.mark.parametrize(
    "text,entities",
    [
        ("Всем привет", True),
        ("Кто сегодня идёт на встречу?", True),
        ("@my_bot_test привет", True),
        ("текст @my_bot текст", False),
    ],
)
async def test_privacy_gate_zero_sdk_calls(handler, sdk, telegram, text, entities):
    await handler.handle(make_message(text, entities=entities), telegram)
    sdk.responses.create.assert_not_awaited()
    telegram.send_message.assert_not_awaited()
    telegram.send_chat_action.assert_not_awaited()


async def test_mention_makes_exactly_one_stateless_sdk_call(handler, sdk, telegram):
    message = make_message("@my_bot объясни Docker")
    await handler.handle(message, telegram)
    sdk.responses.create.assert_awaited_once()
    kwargs = sdk.responses.create.call_args.kwargs
    assert kwargs["input"] == "объясни Docker"
    assert kwargs["model"] == "gpt-6-luna"
    assert kwargs["store"] is False
    assert not {"tools", "conversation", "previous_response_id", "metadata"} & kwargs.keys()
    sent = telegram.send_message.call_args.kwargs
    assert sent["reply_parameters"].message_id == message.message_id
    assert sent["parse_mode"] is None


async def test_mention_only_is_local(handler, sdk, telegram):
    await handler.handle(make_message("@my_bot"), telegram)
    sdk.responses.create.assert_not_awaited()
    assert telegram.send_message.call_args.kwargs["text"] == EMPTY_PROMPT


@pytest.mark.parametrize("text", ["ептиль", "ЕПТИЛЬ", "ЁпТиЛь"])
async def test_eptil_reply_does_not_call_model(handler, sdk, telegram, text):
    await handler.handle(make_message(text, entities=False), telegram)
    sdk.responses.create.assert_not_awaited()
    assert telegram.send_message.call_args.kwargs["text"] == "ептиль бля 🙂"


async def test_disallowed_chat(handler, sdk, telegram, settings):
    handler.settings = replace(settings, allowed_chat_ids=frozenset({-999}))
    await handler.handle(make_message(), telegram)
    sdk.responses.create.assert_not_awaited()
    telegram.send_message.assert_not_awaited()


async def test_rate_limit_before_sdk(handler, sdk, telegram):
    handler.limiter = RateLimiter(1, 60)
    assert handler.limiter.allow(10)
    await handler.handle(make_message(), telegram)
    sdk.responses.create.assert_not_awaited()
    assert telegram.send_message.call_args.kwargs["text"] == LOCAL_RATE_LIMIT


async def test_long_prompt_not_truncated_or_sent(handler, sdk, telegram, settings):
    handler.settings = replace(settings, max_input_chars=3)
    await handler.handle(make_message("@my_bot abcd"), telegram)
    sdk.responses.create.assert_not_awaited()
    assert "3 символов" in telegram.send_message.call_args.kwargs["text"]


@pytest.mark.parametrize(
    "values",
    [
        {"chat": Chat(id=10, type="private")},
        {"chat": Chat(id=-100, type="channel")},
        {"from_user": User(id=20, is_bot=True, first_name="Bot")},
        {"from_user": None},
        {"text": None, "new_chat_title": "New title"},
        {"sender_chat": Chat(id=-100, type="supergroup")},
    ],
)
async def test_ignored_message_types(handler, sdk, telegram, values):
    await handler.handle(make_message(**values), telegram)
    sdk.responses.create.assert_not_awaited()


async def test_reply_context_not_sent_without_history_service(handler, sdk, telegram):
    await handler.handle(
        make_message(reply_to_message=make_message("SECRET ordinary message", entities=False)),
        telegram,
    )
    assert sdk.responses.create.call_args.kwargs["input"] == "вопрос"


@pytest.mark.parametrize("update_kind", ["edited_message", "channel_post", "edited_channel_post"])
async def test_dispatcher_ignores_other_updates(handler, sdk, update_kind):
    bot = Bot("123456:test-token")
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(handler.router())
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, **{update_kind: make_message()}))
        sdk.responses.create.assert_not_awaited()
    finally:
        await bot.session.close()


async def test_dispatcher_routes_actual_message(handler, sdk, telegram, monkeypatch):
    monkeypatch.setattr(Bot, "send_message", telegram.send_message)
    monkeypatch.setattr(Bot, "send_chat_action", telegram.send_chat_action)
    bot = Bot("123456:test-token")
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.include_router(handler.router())
    try:
        await dispatcher.feed_update(bot, Update(update_id=1, message=make_message()))
    finally:
        await bot.session.close()
    sdk.responses.create.assert_awaited_once()


async def test_split_response_preserves_forum_topic(handler, sdk, telegram):
    sdk.responses.create.return_value.output_text = "🙂" * 5000
    await handler.handle(make_message(message_thread_id=99), telegram)
    calls = telegram.send_message.call_args_list
    assert len(calls) == 3
    assert calls[0].kwargs["reply_parameters"].message_id == 7
    assert all(c.kwargs["reply_parameters"] is None for c in calls[1:])
    assert all(c.kwargs["message_thread_id"] == 99 for c in calls)


async def test_whitespace_only_response_chunks_are_skipped(handler, sdk, telegram):
    sdk.responses.create.return_value.output_text = "a" + " " * 12000 + "b"
    await handler.handle(make_message(), telegram)
    assert all(c.kwargs["text"].strip() for c in telegram.send_message.call_args_list)


@pytest.mark.parametrize(
    "error",
    [
        TelegramBadRequest(method=SendMessage(chat_id=1, text="x"), message="forbidden SECRET"),
        TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="network SECRET"),
    ],
)
async def test_telegram_send_failure_does_not_regenerate(handler, sdk, telegram, caplog, error):
    telegram.send_message.side_effect = error
    await handler.handle(make_message(), telegram)
    sdk.responses.create.assert_awaited_once()
    assert "SECRET" not in caplog.text


async def test_typing_runs_while_model_waits(handler, sdk, telegram):
    started, release = asyncio.Event(), asyncio.Event()

    async def answer(**kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(output_text="Ответ")

    sdk.responses.create.side_effect = answer
    task = asyncio.create_task(handler.handle(make_message(), telegram))
    await started.wait()
    await asyncio.sleep(0)
    telegram.send_chat_action.assert_awaited()
    release.set()
    await task


async def test_shutdown_cancels_requests(handler, sdk, telegram):
    started = asyncio.Event()

    async def wait_forever(**kwargs):
        started.set()
        await asyncio.Event().wait()

    sdk.responses.create.side_effect = wait_forever
    task = asyncio.create_task(handler.handle(make_message(), telegram))
    await started.wait()
    await handler.shutdown()
    assert task.cancelled()
    assert not handler._active
    telegram.send_message.assert_not_awaited()
