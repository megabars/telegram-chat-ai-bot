import asyncio
import base64
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import PhotoSize, User

from app.handlers.messages import MAX_PHOTO_BYTES, PHOTO_CHAT_LIMIT, PHOTO_USER_LIMIT
from app.services.openai_service import ModelRateLimited
from tests.conftest import make_message

PHOTO = PhotoSize(
    file_id="photo-file",
    file_unique_id="unique-photo",
    width=1024,
    height=768,
    file_size=16,
)
JPEG = b"\xff\xd8\xffSECRET-photo-bytes"


def caption_photo(message_id=7, user_id=10):
    caption = "@my_bot что на фото?"
    entities = make_message(caption).entities
    return make_message(
        None,
        message_id=message_id,
        from_user=User(id=user_id, is_bot=False, first_name="Human"),
        caption=caption,
        caption_entities=entities,
        photo=[PHOTO],
    )


@pytest.fixture
def photo_bot(telegram):
    telegram.get_file = AsyncMock(
        return_value=SimpleNamespace(file_path="photos/file.jpg", file_size=len(JPEG))
    )

    async def download_file(_path, *, destination, **_kwargs):
        destination.write(JPEG)
        return destination

    telegram.download_file = AsyncMock(side_effect=download_file)
    return telegram


@pytest.fixture
def photo_handler(handler, settings, local_history):
    config = replace(settings, local_history_enabled=True, photo_analysis_enabled=True)
    handler.settings = local_history.settings = config
    handler.local_history = local_history
    return handler


async def test_caption_question_sends_only_selected_photo_and_text(
    photo_handler, sdk, photo_bot, caplog
):
    message = caption_photo()
    with caplog.at_level("INFO"):
        await photo_handler.handle(message, photo_bot)
    sdk.responses.create.assert_awaited_once()
    request = sdk.responses.create.call_args.kwargs
    assert request["store"] is False
    assert request["input"] == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "что на фото?"},
                {
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64," + base64.b64encode(JPEG).decode("ascii"),
                    "detail": "high",
                },
            ],
        }
    ]
    assert "SECRET" not in caplog.text
    assert "input_image_bytes=" in caplog.text
    assert photo_bot.send_message.call_args.kwargs["reply_parameters"].message_id == 7


async def test_reply_to_photo_uses_parent_and_unmentioned_photo_stays_local(
    photo_handler, sdk, photo_bot
):
    parent = make_message(None, entities=False, message_id=3, photo=[PHOTO])
    await photo_handler.handle(
        make_message("@my_bot что изображено?", reply_to_message=parent), photo_bot
    )
    assert sdk.responses.create.await_count == 1
    assert photo_bot.get_file.await_count == 1
    await photo_handler.handle(
        make_message("просто фото", entities=False, message_id=8, photo=[PHOTO]), photo_bot
    )
    assert sdk.responses.create.await_count == 1
    assert photo_bot.get_file.await_count == 1


async def test_photo_analysis_is_explicitly_opted_in(handler, sdk, photo_bot):
    await handler.handle(caption_photo(), photo_bot)
    sdk.responses.create.assert_not_awaited()
    photo_bot.get_file.assert_not_awaited()


async def test_disallowed_or_external_photo_never_downloaded(
    photo_handler, settings, sdk, photo_bot
):
    photo_handler.settings = replace(photo_handler.settings, allowed_chat_ids=frozenset({-999}))
    await photo_handler.handle(caption_photo(), photo_bot)
    photo_bot.get_file.assert_not_awaited()
    sdk.responses.create.assert_not_awaited()

    photo_handler.settings = replace(
        settings, local_history_enabled=True, photo_analysis_enabled=True
    )
    parent = make_message(None, entities=False, message_id=3, chat_id=-999, photo=[PHOTO])
    await photo_handler.handle(make_message(reply_to_message=parent), photo_bot)
    photo_bot.get_file.assert_not_awaited()
    sdk.responses.create.assert_not_awaited()


async def test_large_photo_rejected_without_spending_quota(photo_handler, sdk, photo_bot):
    photo_bot.get_file.return_value.file_size = MAX_PHOTO_BYTES + 1
    await photo_handler.handle(caption_photo(), photo_bot)
    sdk.responses.create.assert_not_awaited()
    rows = await photo_handler.local_history._db().execute_fetchall(
        "SELECT * FROM photo_analysis_requests"
    )
    assert rows == []


async def test_daily_quotas_atomic_and_persist_across_restart(local_history):
    local_history.settings = replace(local_history.settings, photo_analysis_enabled=True)

    async def claim(message_id, user_id, day="2026-10-07"):
        return await local_history.claim_photo_request(
            -100123,
            message_id,
            user_id,
            day,
            user_limit=2,
            chat_limit=3,
        )

    assert await asyncio.gather(claim(1, 10), claim(2, 10)) == ["accepted", "accepted"]
    assert await claim(2, 10) == "duplicate"
    assert await claim(3, 10) == "user_limit"
    await local_history.close()
    await local_history.start()
    assert await claim(4, 11) == "accepted"
    assert await claim(5, 11) == "chat_limit"
    assert await claim(6, 10, "2026-10-08") == "accepted"


async def test_handler_enforces_photo_daily_limits(photo_handler, sdk, photo_bot):
    config = replace(
        photo_handler.settings,
        photo_daily_user_limit=1,
        photo_daily_chat_limit=2,
    )
    photo_handler.settings = photo_handler.local_history.settings = config
    await photo_handler.handle(caption_photo(7, 10), photo_bot)
    await photo_handler.handle(caption_photo(8, 10), photo_bot)
    assert photo_bot.send_message.call_args.kwargs["text"] == PHOTO_USER_LIMIT
    await photo_handler.handle(caption_photo(9, 11), photo_bot)
    await photo_handler.handle(caption_photo(10, 12), photo_bot)
    assert photo_bot.send_message.call_args.kwargs["text"] == PHOTO_CHAT_LIMIT
    assert sdk.responses.create.await_count == 2


async def test_failed_model_attempt_consumes_daily_photo_quota(photo_handler, photo_bot):
    config = replace(photo_handler.settings, photo_daily_user_limit=1)
    photo_handler.settings = photo_handler.local_history.settings = config
    photo_handler.service.generate_photo_question = AsyncMock(side_effect=ModelRateLimited())
    await photo_handler.handle(caption_photo(7, 10), photo_bot)
    await photo_handler.handle(caption_photo(8, 10), photo_bot)
    assert photo_handler.service.generate_photo_question.await_count == 1
    assert photo_bot.send_message.call_args.kwargs["text"] == PHOTO_USER_LIMIT
