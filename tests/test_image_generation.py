import asyncio
import base64
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import MessageEntity, PhotoSize, User

from app.handlers.messages import IMAGE_CHAT_LIMIT, IMAGE_USER_LIMIT
from tests.conftest import make_message

JPEG = b"\xff\xd8\xfftest-image"
PHOTO = PhotoSize(
    file_id="photo", file_unique_id="photo-unique", width=500, height=500, file_size=len(JPEG)
)


def command(text, **kwargs):
    command_text = text.split()[0]
    return make_message(
        text,
        entities=[MessageEntity(type="bot_command", offset=0, length=len(command_text))],
        **kwargs,
    )


def album_photo(message_id, album_id, *, caption=None):
    return make_message(
        None,
        message_id=message_id,
        caption=caption,
        caption_entities=[MessageEntity(type="bot_command", offset=0, length=4)] if caption else [],
        photo=[PHOTO],
        media_group_id=album_id,
    )


@pytest.fixture
def image_handler(handler, local_history, settings, sdk, telegram):
    config = replace(settings, local_history_enabled=True, image_generation_enabled=True)
    handler.settings = local_history.settings = handler.service.settings = config
    handler.local_history = local_history
    sdk.images = SimpleNamespace(
        generate=AsyncMock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(JPEG).decode())]
            )
        ),
        edit=AsyncMock(
            return_value=SimpleNamespace(
                data=[SimpleNamespace(b64_json=base64.b64encode(JPEG).decode())]
            )
        ),
    )
    telegram.send_photo = AsyncMock()
    telegram.get_file = AsyncMock(
        return_value=SimpleNamespace(file_path="photo.jpg", file_size=len(JPEG))
    )

    async def download_file(_path, *, destination, **_kwargs):
        destination.write(JPEG)

    telegram.download_file = AsyncMock(side_effect=download_file)
    return handler


async def test_image_command_is_explicit_and_sends_one_photo(image_handler, sdk, telegram):
    await image_handler.handle(command("/image рыжий кот"), telegram)
    sdk.images.generate.assert_awaited_once()
    kwargs = sdk.images.generate.call_args.kwargs
    assert kwargs["n"] == 1
    assert kwargs["quality"] == "low"
    assert kwargs["size"] == "1024x1024"
    assert kwargs["prompt"] == "рыжий кот"
    assert telegram.send_photo.await_count == 1
    assert telegram.send_photo.call_args.kwargs["reply_parameters"].message_id == 7
    await image_handler.handle(command("/image@other_bot кот", message_id=8), telegram)
    assert sdk.images.generate.await_count == 1


async def test_album_mix_uses_all_images_once(image_handler, sdk, telegram):
    await image_handler.handle(album_photo(11, "album-1"), telegram)
    await image_handler.handle(album_photo(12, "album-1"), telegram)
    await image_handler.handle(album_photo(13, "album-1", caption="/mix объедини сюжеты"), telegram)
    sdk.images.generate.assert_not_awaited()
    sdk.images.edit.assert_awaited_once()
    kwargs = sdk.images.edit.call_args.kwargs
    assert kwargs["prompt"] == "объедини сюжеты"
    assert len(kwargs["image"]) == 3
    assert all(item[1] == JPEG for item in kwargs["image"])
    assert telegram.send_photo.await_count == 1


async def test_reply_photo_mix_uses_two_images(image_handler, sdk, telegram):
    parent = make_message(None, message_id=5, photo=[PHOTO])
    second = make_message(
        None,
        caption="/mix объедини",
        caption_entities=[MessageEntity(type="bot_command", offset=0, length=4)],
        photo=[PHOTO],
        reply_to_message=parent,
    )
    await image_handler.handle(second, telegram)
    assert len(sdk.images.edit.call_args.kwargs["image"]) == 2


async def test_edit_photo_caption_and_text_reply(image_handler, sdk, telegram):
    captioned = make_message(
        None,
        message_id=17,
        caption="/edit замени фон",
        caption_entities=[MessageEntity(type="bot_command", offset=0, length=5)],
        photo=[PHOTO],
    )
    await image_handler.handle(captioned, telegram)
    assert sdk.images.edit.call_args.kwargs["prompt"] == "замени фон"
    assert len(sdk.images.edit.call_args.kwargs["image"]) == 1

    parent = make_message(None, message_id=18, photo=[PHOTO])
    await image_handler.handle(
        command("/edit добавь дерево", message_id=19, reply_to_message=parent), telegram
    )
    assert sdk.images.edit.await_count == 2
    assert sdk.images.edit.call_args.kwargs["prompt"] == "добавь дерево"


async def test_image_quotas_persist_and_are_separate_from_photo(local_history):
    local_history.settings = replace(local_history.settings, image_generation_enabled=True)

    async def claim(message_id, user_id, day="2026-10-07"):
        return await local_history.claim_image_request(
            -100123, message_id, user_id, day, user_limit=2, chat_limit=3
        )

    assert await asyncio.gather(claim(1, 10), claim(2, 10)) == ["accepted", "accepted"]
    assert await claim(2, 10) == "duplicate"
    assert await claim(3, 10) == "user_limit"
    await local_history.close()
    await local_history.start()
    assert await claim(4, 11) == "accepted"
    assert await claim(5, 11) == "chat_limit"
    assert await claim(6, 10, "2026-10-08") == "accepted"
    rows = await local_history._db().execute_fetchall("SELECT * FROM photo_analysis_requests")
    assert rows == []


async def test_handler_enforces_image_limits(image_handler, sdk, telegram):
    config = replace(image_handler.settings, image_daily_user_limit=1, image_daily_chat_limit=2)
    image_handler.settings = image_handler.local_history.settings = config
    await image_handler.handle(command("/image кот", message_id=1), telegram)
    await image_handler.handle(command("/image пёс", message_id=2), telegram)
    assert telegram.send_message.call_args.kwargs["text"] == IMAGE_USER_LIMIT
    await image_handler.handle(
        command("/image дом", message_id=3, from_user=User(id=11, is_bot=False, first_name="B")),
        telegram,
    )
    await image_handler.handle(
        command("/image сад", message_id=4, from_user=User(id=12, is_bot=False, first_name="C")),
        telegram,
    )
    assert telegram.send_message.call_args.kwargs["text"] == IMAGE_CHAT_LIMIT
    assert sdk.images.generate.await_count == 2


async def test_disallowed_chat_does_not_generate(image_handler, sdk, telegram):
    image_handler.settings = replace(image_handler.settings, allowed_chat_ids=frozenset({-999}))
    await image_handler.handle(command("/image кот"), telegram)
    sdk.images.generate.assert_not_awaited()
    telegram.send_photo.assert_not_awaited()
