import asyncio
import sqlite3
import stat
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from aiogram.types import Chat, User

from tests.conftest import make_message


async def rows(service):
    return await service._db().execute_fetchall(
        "SELECT * FROM telegram_messages ORDER BY chat_id, message_id"
    )


async def test_wal_schema_permissions_and_persistence(local_history):
    service = local_history
    path = service.settings.local_history_db_path
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        indexes = {r[1] for r in db.execute("PRAGMA index_list(telegram_messages)")}
        assert "idx_telegram_messages_chat_message" in indexes
        assert "idx_telegram_messages_chat_topic_message" in indexes
    assert stat.S_IMODE(service._prepare_path().stat().st_mode) == 0o600
    assert stat.S_IMODE(service._prepare_path().parent.stat().st_mode) == 0o700
    await service.save_message(make_message("Hello", entities=False))
    await service.close()
    await service.start()
    assert (await service.get_recent_messages(-100123, 8, 10))[0].text == "Hello"


async def test_duplicate_and_edit_do_not_increase_count_or_restore_old_text(local_history):
    original = make_message("Old", entities=False)
    await local_history.save_message(original)
    await local_history.save_message(original)
    edited = original.model_copy(
        update={"text": "New", "edit_date": int(datetime.now(UTC).timestamp())}
    )
    await local_history.update_message(edited)
    await local_history.save_message(original)  # Late original update cannot undo edit.
    result = await rows(local_history)
    assert len(result) == 1 and result[0]["text"] == "New"
    assert local_history._counts == {-100123: 1}


async def test_edit_versions_and_missing_original(local_history):
    newer = make_message("Newer", edit_date=int(datetime.now(UTC).timestamp()), entities=False)
    older = newer.model_copy(update={"text": "Older", "edit_date": newer.edit_date - 86400})
    await local_history.update_message(newer)
    await local_history.update_message(older)
    assert (await rows(local_history))[0]["text"] == "Newer"


async def test_exact_snapshot_ids_chronological_and_cross_chat(local_history):
    for message_id in [1, 5, 3, 9, 7]:
        await local_history.save_message(
            make_message(f"text-{message_id}", message_id=message_id, entities=False)
        )
    await local_history.save_message(
        make_message("Other chat", message_id=6, chat_id=-777, entities=False)
    )
    result = await local_history.get_recent_messages(-100123, 7, 2)
    assert [m.message_id for m in result] == [3, 5]
    # Tuple is independent of subsequent inserts/edits, not a live DB cursor.
    await local_history.update_message(make_message("Changed", message_id=5, entities=False))
    assert result[-1].text == "text-5"


async def test_topics_general_and_ordinary_reply_threads(local_history):
    forum = Chat(id=-100123, type="supergroup", is_forum=True)
    for i, topic in [(1, 99), (2, 88), (3, 99), (4, None)]:
        await local_history.save_message(
            make_message(
                str(i),
                message_id=i,
                chat=forum,
                message_thread_id=topic,
                is_topic_message=topic is not None,
                entities=False,
            )
        )
    assert [m.message_id for m in await local_history.get_recent_messages(-100123, 10, 50, 99)] == [
        1,
        3,
    ]
    assert [m.message_id for m in await local_history.get_recent_messages(-100123, 10, 50, 1)] == [
        4
    ]
    await local_history.save_message(
        make_message(
            "Ordinary thread",
            chat_id=-999,
            message_id=5,
            message_thread_id=123,
            is_topic_message=False,
            entities=False,
        )
    )
    assert len(await local_history.get_recent_messages(-999, 10, 50)) == 1
    local_history.settings = replace(local_history.settings, context_respect_topics=False)
    assert len(await local_history.get_recent_messages(-100123, 10, 50, 99)) == 4


async def test_actual_5100_cleanup_retains_5000_and_other_chat(local_history):
    for i in range(1, 5100):
        await local_history.save_message(make_message(str(i), message_id=i, entities=False))
    assert local_history._counts[-100123] == 5099
    result = await rows(local_history)
    assert len(result) == 5099 and result[0]["message_id"] == 1
    await local_history.save_message(make_message("Other", chat_id=-777, entities=False))
    await local_history.save_message(make_message("5100", message_id=5100, entities=False))
    result = await rows(local_history)
    chat = [r for r in result if r["chat_id"] == -100123]
    assert len(chat) == 5000
    assert chat[0]["message_id"] == 101 and chat[-1]["message_id"] == 5100
    assert len([r for r in result if r["chat_id"] == -777]) == 1
    assert local_history._counts[-100123] == 5000


async def test_cleanup_limit_is_per_chat_not_per_topic(local_history):
    local_history.settings = replace(
        local_history.settings, local_history_max_messages=3, local_history_cleanup_threshold=5
    )
    for i in range(1, 6):
        await local_history.save_message(
            make_message(
                str(i),
                message_id=i,
                message_thread_id=99 if i % 2 else 88,
                is_topic_message=True,
                entities=False,
            )
        )
    assert [r["message_id"] for r in await rows(local_history)] == [3, 4, 5]
    assert local_history._counts[-100123] == 3


async def test_concurrent_duplicate_inserts_cleanup_and_sql_parameters(local_history):
    local_history.settings = replace(
        local_history.settings, local_history_max_messages=100, local_history_cleanup_threshold=110
    )
    tasks = [
        local_history.save_message(
            make_message("'; DROP TABLE telegram_messages; --", message_id=i, entities=False)
        )
        for i in range(1, 301)
    ]
    await asyncio.gather(*tasks)
    result = await rows(local_history)
    assert len(result) == 100
    assert [r["message_id"] for r in result] == list(range(201, 301))
    assert all("DROP TABLE" in r["text"] for r in result)


async def test_failure_rolls_back_insert_count_and_leaves_connection_usable(
    local_history, monkeypatch
):
    cleanup = local_history._cleanup_sql

    async def fail(*args):
        raise RuntimeError("Private text must not leak")

    monkeypatch.setattr(local_history, "_cleanup_sql", fail)
    with pytest.raises(RuntimeError):
        await local_history.save_message(make_message("Hello", entities=False))
    assert await rows(local_history) == []
    assert local_history._counts == {}
    monkeypatch.setattr(local_history, "_cleanup_sql", cleanup)
    await local_history.save_message(make_message("Recovered", entities=False))
    assert len(await rows(local_history)) == 1


async def test_cancellation_finishes_atomic_write_and_close(local_history, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    cleanup = local_history._cleanup_sql

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await cleanup(*args)

    monkeypatch.setattr(local_history, "_cleanup_sql", delayed)
    task = asyncio.create_task(local_history.save_message(make_message("Hello", entities=False)))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(await rows(local_history)) == 1
    assert local_history._counts[-100123] == 1
    assert not local_history._lock.locked()


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        (
            {
                "text": None,
                "caption": "Caption",
                "photo": [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}],
            },
            "Caption",
        ),
        (
            {
                "text": None,
                "photo": [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}],
            },
            "[photo]",
        ),
        (
            {"text": None, "voice": {"file_id": "f", "file_unique_id": "u", "duration": 1}},
            "[voice message]",
        ),
        ({"text": None, "document": {"file_id": "f", "file_unique_id": "u"}}, "[document]"),
        ({"text": None, "new_chat_title": "New title"}, None),
        ({"text": None, "new_chat_members": [User(id=1, is_bot=False, first_name="New")]}, None),
    ],
)
async def test_caption_placeholders_and_service_events(local_history, kwargs, expected):
    await local_history.save_message(make_message(entities=False, **kwargs))
    result = await rows(local_history)
    assert [r["text"] for r in result] == ([] if expected is None else [expected])


async def test_bots_anonymous_sender_and_redaction(local_history):
    for i, sender in [
        (1, User(id=123456, first_name="Our", is_bot=True)),
        (2, User(id=999, first_name="Other", is_bot=True)),
    ]:
        await local_history.save_message(
            make_message("Bot response", message_id=i, from_user=sender, entities=False)
        )
    await local_history.save_message(
        make_message(
            "Anonymous",
            message_id=3,
            sender_chat=Chat(id=-888, type="supergroup", title="Anonymous admin"),
            entities=False,
        )
    )
    await local_history.save_message(
        make_message("token " + local_history.settings.openai_api_key, message_id=4, entities=False)
    )
    result = await rows(local_history)
    assert result[0]["is_our_bot"] == 1 and result[1]["is_our_bot"] == 0
    assert result[1]["is_bot"] == 1 and result[2]["sender_name"] == "Anonymous admin"
    assert local_history.settings.openai_api_key not in result[3]["text"]


@pytest.mark.parametrize("mode", ["disallowed", "private", "disabled"])
async def test_privacy_scope_zero_rows(local_history, mode):
    kwargs = {}
    if mode == "disallowed":
        local_history.settings = replace(local_history.settings, allowed_chat_ids=frozenset({-999}))
    elif mode == "private":
        kwargs["chat"] = Chat(id=10, type="private")
    else:
        local_history.settings = replace(local_history.settings, local_history_enabled=False)
    assert not await local_history.save_message(make_message("Hello", entities=False, **kwargs))
    assert await rows(local_history) == []


async def test_startup_reduced_threshold_and_idempotent_schema(local_history):
    for i in range(1, 6):
        await local_history.save_message(make_message(str(i), message_id=i, entities=False))
    await local_history.close()
    local_history.settings = replace(
        local_history.settings, local_history_max_messages=3, local_history_cleanup_threshold=5
    )
    await local_history.start()
    await local_history.start()
    assert [r["message_id"] for r in await rows(local_history)] == [3, 4, 5]
