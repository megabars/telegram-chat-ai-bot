import socket
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.enums import ChatType
from aiogram.types import Chat, Message, MessageEntity, User

from app.config import Settings
from app.handlers.messages import MessageHandler
from app.services.local_history import LocalHistoryService
from app.services.openai_service import OpenAIService
from app.utils.rate_limit import RateLimiter


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must never make network requests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)


@pytest.fixture
def settings():
    # Legacy scenarios deliberately run without a local DB; new tests use temp SQLite.
    return Settings(
        telegram_bot_token="123456:test-token",
        openai_api_key="test-key",
        local_history_enabled=False,
    )


@pytest.fixture
def sdk():
    return SimpleNamespace(
        responses=SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(output_text="Ответ"))
        ),
        close=AsyncMock(),
    )


@pytest.fixture
def telegram():
    return SimpleNamespace(send_message=AsyncMock(), send_chat_action=AsyncMock())


@pytest.fixture
def handler(settings, sdk):
    return MessageHandler(
        settings,
        123456,
        "my_bot",
        OpenAIService(settings, sdk),
        RateLimiter(settings.rate_limit_requests, settings.rate_limit_period_seconds),
    )


@pytest.fixture
async def local_history(settings, tmp_path):
    config = replace(
        settings,
        local_history_enabled=True,
        local_history_db_path=str(tmp_path / "state" / "history.sqlite3"),
    )
    service = LocalHistoryService(config, 123456)
    await service.start()
    yield service
    await service.close()


def make_message(text="@my_bot вопрос", *, entities=True, chat_id=-100123, **kwargs):
    entity_list = []
    if entities is True and text and "@" in text:
        start = text.index("@")
        name = text[start:].split()[0].rstrip(",.!?")
        entity_list = [
            MessageEntity(
                type="mention",
                offset=len(text[:start].encode("utf-16-le")) // 2,
                length=len(name.encode("utf-16-le")) // 2,
            )
        ]
    elif isinstance(entities, list):
        entity_list = entities
    values = dict(
        message_id=7,
        date=datetime.now(UTC),
        chat=Chat(id=chat_id, type=ChatType.SUPERGROUP),
        from_user=User(id=10, is_bot=False, first_name="Human"),
        text=text,
        entities=entity_list,
    )
    values.update(kwargs)
    return Message(**values)
