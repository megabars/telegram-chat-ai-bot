import asyncio
import socket
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import MessageEntity, User

from app.handlers.messages import LINK_CHAT_LIMIT, LINK_USER_LIMIT
from app.services.openai_service import OpenAIService
from app.services.url_reader import UrlFetchError, _extract_text, _public_addresses, fetch_page
from tests.conftest import make_message


def command(text="/link https://example.com what is this?", message_id=101, **kwargs):
    token = text.split()[0]
    return make_message(
        text,
        message_id=message_id,
        entities=[MessageEntity(type="bot_command", offset=0, length=len(token))],
        **kwargs,
    )


@pytest.fixture
def link_handler(handler, local_history, telegram):
    config = replace(
        local_history.settings,
        link_read_enabled=True,
        link_daily_user_limit=3,
        link_daily_chat_limit=10,
    )
    handler.settings = handler.service.settings = local_history.settings = config
    handler.local_history = local_history
    return handler


async def test_link_command_fetches_page_and_uses_one_normal_model_call(
    link_handler, sdk, telegram, monkeypatch
):
    fetch = AsyncMock(return_value=("https://example.com/page", "Page content"))
    monkeypatch.setattr("app.handlers.messages.fetch_page", fetch)
    message = command()
    await link_handler.handle(message, telegram)
    fetch.assert_awaited_once_with("https://example.com")
    sdk.responses.create.assert_awaited_once()
    request = sdk.responses.create.call_args.kwargs
    assert "what is this?" in request["input"]
    assert "Page content" in request["input"]
    assert "https://example.com/page" in request["input"]
    assert "tools" not in request
    assert telegram.send_message.call_args.kwargs["text"] == (
        "Ответ\n\nИсточник: https://example.com/page"
    )


async def test_link_command_applies_daily_user_quota(link_handler, sdk, telegram, monkeypatch):
    monkeypatch.setattr(
        "app.handlers.messages.fetch_page",
        AsyncMock(return_value=("https://example.com", "Page content")),
    )
    for message_id in range(1, 5):
        await link_handler.handle(command(message_id=message_id), telegram)
    assert sdk.responses.create.await_count == 3
    assert telegram.send_message.call_args.kwargs["text"] == LINK_USER_LIMIT


async def test_link_command_applies_daily_chat_quota(link_handler, sdk, telegram, monkeypatch):
    monkeypatch.setattr(
        "app.handlers.messages.fetch_page",
        AsyncMock(return_value=("https://example.com", "Page content")),
    )
    for message_id in range(1, 11):
        await link_handler.handle(
            command(
                message_id=message_id,
                from_user=User(id=100 + message_id, is_bot=False, first_name="User"),
            ),
            telegram,
        )
    await link_handler.handle(
        command(message_id=11, from_user=User(id=999, is_bot=False, first_name="Last")),
        telegram,
    )
    assert sdk.responses.create.await_count == 10
    assert telegram.send_message.call_args.kwargs["text"] == LINK_CHAT_LIMIT


async def test_link_command_ignores_command_for_another_bot(link_handler, sdk, telegram):
    message = command("/link@other_bot https://example.com", message_id=31)
    await link_handler.handle(message, telegram)
    sdk.responses.create.assert_not_awaited()


async def test_link_quota_is_persistent_and_independent(local_history):
    local_history.settings = replace(local_history.settings, link_read_enabled=True)

    async def claim(message_id, user_id, day="2026-10-07"):
        return await local_history.claim_link_request(
            -100123, message_id, user_id, day, user_limit=2, chat_limit=3
        )

    assert await claim(1, 10) == "accepted"
    assert await claim(2, 10) == "accepted"
    assert await claim(3, 10) == "user_limit"
    assert await claim(1, 10) == "duplicate"
    await local_history.close()
    await local_history.start()
    assert await claim(4, 11) == "accepted"
    assert await claim(5, 12) == "chat_limit"
    photo_rows = await local_history._db().execute_fetchall(
        "SELECT * FROM photo_analysis_requests"
    )
    assert photo_rows == []


async def test_url_reader_blocks_private_hosts_before_connecting():
    for url in (
        "http://127.0.0.1/private",
        "http://[::1]/private",
        "http://localhost/private",
        "https://user:password@example.com/",
        "https://example.com:8443/",
    ):
        with pytest.raises(UrlFetchError):
            await fetch_page(url)


async def test_url_reader_rejects_private_dns_answers(monkeypatch):
    async def private_dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 80))]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", private_dns)
    with pytest.raises(UrlFetchError, match="локальные"):
        await _public_addresses("attacker.example", 80)


def test_url_reader_extracts_visible_text_and_caps_content():
    result = _extract_text(
        b"<html><script>secret()</script><nav>menu</nav><h1>Title</h1><p>Hello</p></html>",
        "text/html",
        "utf-8",
    )
    assert "Title" in result and "Hello" in result
    assert "secret" not in result and "menu" not in result


async def test_openai_link_request_contains_page_as_untrusted_text(settings, sdk):
    await OpenAIService(settings, sdk).generate_link_answer(
        "question", "https://example.com", "untrusted page", chat_id=-100, message_id=42
    )
    request = sdk.responses.create.call_args.kwargs
    assert "untrusted page" in request["input"]
    assert "https://example.com" in request["input"]
    assert "Do not claim to have searched the web" in request["instructions"]
    assert "tools" not in request
    assert request["store"] is False
