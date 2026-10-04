import asyncio
import stat
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import TelegramClient, errors, functions, types, utils
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession, SQLiteSession

from app.config import ConfigurationError
from app.services import telegram_history
from app.services.telegram_history import TelegramHistoryService
from app.utils.reply_context import MARKER_RESERVE, TEXT_MARKER, render_context_message

BOT_ID = 123456
CHAT_ID = -1000000000123


def mt_message(
    message_id,
    parent=None,
    *,
    chat_id=CHAT_ID,
    text=None,
    sender_id=10,
    topic=None,
    media=None,
    **kwargs,
):
    header = (
        types.MessageReplyHeader(
            reply_to_msg_id=parent,
            forum_topic=topic is not None,
            reply_to_top_id=topic,
        )
        if parent is not None or topic is not None
        else None
    )
    peer = utils.get_peer(chat_id)
    values = dict(
        id=message_id,
        peer_id=peer,
        reply_to=header,
        date=datetime(2026, 10, 4, 10, message_id % 60, tzinfo=UTC),
        sender_id=sender_id,
        sender=types.User(
            id=sender_id,
            first_name="Ivan",
            last_name="Petrov",
            username="ivan",
            bot=sender_id == BOT_ID,
        ),
        message=text if text is not None else f"message-{message_id}",
        media=media,
    )
    values.update(kwargs)
    return SimpleNamespace(**values)


def fake_client(messages, chat_id=CHAT_ID):
    peer_id, peer_type = utils.resolve_id(chat_id)
    peer = (
        types.InputPeerChat(peer_id)
        if peer_type is types.PeerChat
        else types.InputPeerChannel(peer_id, access_hash=987)
    )
    return SimpleNamespace(
        get_input_entity=AsyncMock(return_value=peer),
        get_messages=AsyncMock(side_effect=lambda _, *, ids: messages.get(ids)),
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=types.User(id=BOT_ID, bot=True)),
    )


@pytest.mark.parametrize("chat_id", [-456, CHAT_ID])
async def test_exact_ancestor_ids_reverse_order_no_siblings(settings, chat_id):
    messages = {
        1: mt_message(1, chat_id=chat_id, text="A"),
        2: mt_message(2, 1, chat_id=chat_id, text="B"),
        3: mt_message(3, 2, chat_id=chat_id, text="C"),
        4: mt_message(4, 1, chat_id=chat_id, text="X"),
        5: mt_message(5, 4, chat_id=chat_id, text="Y"),
        6: mt_message(6, chat_id=chat_id, text="Z"),
    }
    client = fake_client(messages, chat_id)
    service = TelegramHistoryService(settings, BOT_ID, client)
    context = await service.get_reply_chain(chat_id, 7, 3)
    assert [m.text for m in context.messages] == ["A", "B", "C"]
    assert context.truncation_reason is None
    assert [c.kwargs["ids"] for c in client.get_messages.call_args_list] == [3, 2, 1]
    client.get_input_entity.assert_awaited_once_with(chat_id)
    assert all(utils.get_peer_id(c.args[0]) == chat_id for c in client.get_messages.call_args_list)
    assert context.messages[0].sender_name == "Ivan Petrov"
    assert context.messages[0].sender_username == "ivan"


async def test_depth_limit_30_of_100(settings):
    client = fake_client({i: mt_message(i, i - 1 if i > 1 else None) for i in range(1, 101)})
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(
        CHAT_ID, 101, 100
    )
    assert [m.message_id for m in context.messages] == list(range(71, 101))
    assert client.get_messages.await_count == 30
    assert context.truncation_reason == "depth_limit"


@pytest.mark.parametrize("parent", [1, 7])
async def test_cycles_include_current_message(settings, parent):
    client = fake_client({1: mt_message(1, 2), 2: mt_message(2, parent)})
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(CHAT_ID, 7, 1)
    assert [m.message_id for m in context.messages] == [2, 1]
    assert context.truncation_reason == "cycle"
    assert client.get_messages.await_count == 2


@pytest.mark.parametrize(
    "missing", [None, types.MessageEmpty(id=1, peer_id=types.PeerChannel(123))]
)
async def test_deleted_parent_preserves_partial_context(settings, missing, caplog):
    client = fake_client({3: mt_message(3, 2), 2: mt_message(2, 1), 1: missing})
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(CHAT_ID, 7, 3)
    assert [m.message_id for m in context.messages] == [2, 3]
    assert context.truncation_reason == "message_unavailable"
    assert "reason=message_unavailable" in caplog.text


@pytest.mark.parametrize("first_available", [True, False])
async def test_overall_timeout_keeps_completed_ancestors(settings, first_available):
    client = fake_client({})
    cancelled = asyncio.Event()

    async def fetch(_, *, ids):
        if first_available and ids == 3:
            return mt_message(3, 2)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    client.get_messages.side_effect = fetch
    service = TelegramHistoryService(
        replace(settings, reply_context_fetch_timeout_seconds=0.02), BOT_ID, client
    )
    context = await asyncio.wait_for(service.get_reply_chain(CHAT_ID, 7, 3), 1)
    assert [m.message_id for m in context.messages] == ([3] if first_available else [])
    assert context.truncation_reason == "timeout"
    assert cancelled.is_set() and not service._semaphore.locked()


@pytest.mark.parametrize(
    "error",
    [
        errors.ChatAdminRequiredError(request=None),
        errors.ChannelPrivateError(request=None),
        errors.MessageIdInvalidError(request=None),
        ValueError("SECRET conversation and token"),
    ],
)
async def test_rpc_errors_preserve_partial_and_safe_logs(settings, error, caplog):
    client = fake_client({})
    client.get_messages.side_effect = [mt_message(3, 2, text="SECRET"), error]
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(CHAT_ID, 7, 3)
    assert [m.message_id for m in context.messages] == [3]
    assert context.truncation_reason == "fetch_error"
    assert "SECRET" not in caplog.text


async def test_flood_wait_never_sleeps_or_retries_and_backs_off(settings):
    client = fake_client({})
    client.get_messages.side_effect = [
        mt_message(3, 2),
        errors.FloodWaitError(request=None, capture=99999),
    ]
    service = TelegramHistoryService(settings, BOT_ID, client)
    context = await asyncio.wait_for(service.get_reply_chain(CHAT_ID, 7, 3), 1)
    assert len(context.messages) == 1 and context.truncation_reason == "flood_wait"
    client.get_input_entity.reset_mock()
    client.get_messages.reset_mock()
    context = await service.get_reply_chain(CHAT_ID, 8, 3)
    assert not context.messages and context.truncation_reason == "flood_wait"
    client.get_input_entity.assert_not_awaited()
    client.get_messages.assert_not_awaited()


@pytest.mark.parametrize(
    "case,reason",
    [
        ("peer", "invalid_peer"),
        ("chat", "cross_chat"),
        ("message_id", "invalid_message_id"),
        ("negative_id", "invalid_message_id"),
    ],
)
async def test_fail_closed_on_wrong_ids(settings, case, reason):
    client = fake_client({3: mt_message(3)})
    direct = 3
    if case == "peer":
        client.get_input_entity.return_value = types.InputPeerChannel(999, 0)
    elif case == "chat":
        client.get_messages.return_value = None
        client.get_messages.side_effect = [mt_message(3, chat_id=-777)]
    elif case == "message_id":
        client.get_messages.side_effect = [mt_message(99)]
    else:
        direct = -1
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(
        CHAT_ID, 7, direct
    )
    assert not context.messages and context.truncation_reason == reason
    if case in {"peer", "negative_id"}:
        client.get_messages.assert_not_awaited()


@pytest.mark.parametrize(
    "expected,actual,accepted",
    [
        (99, 99, True),
        (99, 88, False),
        (99, None, False),
        (None, 99, False),
        (1, None, True),
        (1, 1, True),
        (1, 99, False),
    ],
)
async def test_topic_boundaries_including_general(settings, expected, actual, accepted):
    client = fake_client({3: mt_message(3, topic=actual)})
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(
        CHAT_ID, 7, 3, message_thread_id=expected
    )
    assert bool(context.messages) is accepted
    assert context.truncation_reason == (None if accepted else "cross_topic")


async def test_topic_root_service_message_is_included(settings):
    root = types.MessageService(
        id=99,
        peer_id=types.PeerChannel(123),
        date=datetime.now(UTC),
        action=types.MessageActionTopicCreate(title="Docker", icon_color=0),
        from_id=types.PeerUser(10),
    )
    client = fake_client({101: mt_message(101, 99, topic=99), 99: root})
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(
        CHAT_ID, 105, 101, message_thread_id=99
    )
    assert [m.message_id for m in context.messages] == [99, 101]
    assert context.messages[0].text == "[service message]"


@pytest.mark.parametrize(
    "header",
    [
        types.MessageReplyHeader(reply_to_msg_id=2, reply_to_peer_id=types.PeerChat(999)),
        types.MessageReplyHeader(reply_to_msg_id=2, reply_to_scheduled=True),
        types.MessageReplyHeader(
            reply_to_msg_id=2, reply_from=types.MessageFwdHeader(date=datetime.now(UTC))
        ),
    ],
)
async def test_external_reply_stops_at_selected_local_message(settings, header):
    client = fake_client({3: mt_message(3, reply_to=header)})
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(CHAT_ID, 7, 3)
    assert [m.message_id for m in context.messages] == [3]
    assert context.truncation_reason == "external_reply"
    assert client.get_messages.await_count == 1


async def test_character_budget_keeps_closest_ancestor_and_marks_clipping(settings):
    client = fake_client(
        {3: mt_message(3, 2, text='🙂"\\' * 1000), 2: mt_message(2, 1, text="older")}
    )
    context = await TelegramHistoryService(
        replace(settings, reply_context_max_chars=350), BOT_ID, client
    ).get_reply_chain(CHAT_ID, 7, 3)
    assert len(context.messages) == 1
    assert TEXT_MARKER in context.messages[0].text
    assert context.truncation_reason == "char_limit"
    assert len(render_context_message(context.messages[0])) + MARKER_RESERVE <= 350
    client.get_messages.assert_awaited_once()


@pytest.mark.parametrize(
    "media,placeholder",
    [
        (types.MessageMediaPhoto(photo=types.PhotoEmpty(id=1)), "[photo]"),
        (types.MessageMediaDocument(document=types.DocumentEmpty(id=1)), "[document]"),
        (
            types.MessageMediaDocument(
                document=SimpleNamespace(
                    attributes=[types.DocumentAttributeAudio(duration=1, voice=True)]
                )
            ),
            "[voice message]",
        ),
        (
            types.MessageMediaDocument(
                document=SimpleNamespace(
                    attributes=[types.DocumentAttributeVideo(duration=1, w=1, h=1)]
                )
            ),
            "[video]",
        ),
        (
            types.MessageMediaDocument(
                document=SimpleNamespace(
                    attributes=[
                        types.DocumentAttributeSticker(
                            alt="x", stickerset=types.InputStickerSetEmpty()
                        )
                    ]
                )
            ),
            "[sticker]",
        ),
        (types.MessageMediaGeo(geo=types.GeoPointEmpty()), "[media]"),
    ],
)
async def test_media_caption_or_placeholder_without_download(settings, media, placeholder):
    client = fake_client(
        {
            3: mt_message(3, 2, text="", media=media),
            2: mt_message(2, text="Caption @my_bot", media=media),
        }
    )
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(CHAT_ID, 7, 3)
    assert [m.text for m in context.messages] == ["Caption @my_bot", placeholder]
    # Fake exposes no media download, get_sender, iter_messages or getHistory methods.


@pytest.mark.parametrize("mode", ["no_reply", "disabled", "disallowed"])
async def test_no_rpc_when_context_not_allowed(settings, mode):
    direct = None if mode == "no_reply" else 3
    if mode == "disabled":
        settings = replace(settings, reply_context_enabled=False)
    if mode == "disallowed":
        settings = replace(settings, allowed_chat_ids=frozenset({-999}))
    client = fake_client({})
    assert not (
        await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(CHAT_ID, 7, direct)
    ).messages
    client.get_input_entity.assert_not_awaited()
    client.get_messages.assert_not_awaited()


async def test_concurrency_cancellation_and_shutdown(settings):
    active = peak = 0
    entered, release = asyncio.Event(), asyncio.Event()
    client = fake_client({})

    async def fetch(_, *, ids):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        try:
            await release.wait()
            return mt_message(ids)
        finally:
            active -= 1

    client.get_messages.side_effect = fetch
    service = TelegramHistoryService(replace(settings, max_concurrent_requests=2), BOT_ID, client)
    await service.start()
    tasks = [asyncio.create_task(service.get_reply_chain(CHAT_ID, 99, i)) for i in range(1, 7)]
    await asyncio.wait_for(entered.wait(), 1)
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    release.set()
    assert all(len(c.messages) == 1 for c in await asyncio.gather(*tasks[1:]))
    assert peak == 2 and active == 0
    await service.close()
    await service.close()
    client.disconnect.assert_awaited_once()


async def test_bot_auth_never_requests_phone(settings):
    client = fake_client({})
    client.is_user_authorized.return_value = False
    service = TelegramHistoryService(settings, BOT_ID, client)
    await service.start()
    client.sign_in.assert_awaited_once_with(bot_token=settings.telegram_bot_token)
    client.get_me.assert_awaited_once()
    await service.close()


@pytest.mark.parametrize(
    "identity", [types.User(id=BOT_ID, bot=False), types.User(id=999, bot=True), None]
)
async def test_existing_session_must_be_same_bot(settings, identity):
    client = fake_client({})
    client.get_me.return_value = identity
    service = TelegramHistoryService(settings, BOT_ID, client)
    with pytest.raises(ConfigurationError):
        await service.start()
    client.sign_in.assert_not_awaited()
    client.disconnect.assert_awaited_once()
    assert service.client is None


async def test_session_persists_auth_across_restart_and_rpc_only_flags(
    settings, tmp_path, monkeypatch
):
    path = tmp_path / "state" / "telegram.session"
    config = replace(
        settings,
        telegram_api_id=123,
        telegram_api_hash="fake-hash",
        telegram_mtproto_session_path=str(path),
    )
    constructor = Mock()
    clients = []

    def construct(session_path, api_id, api_hash, **kwargs):
        client = fake_client({})
        client.session = SQLiteSession(session_path)
        clients.append(client)
        return client

    constructor.side_effect = construct
    monkeypatch.setattr(telegram_history, "TelegramClient", constructor)
    first = TelegramHistoryService(config, BOT_ID)
    await first.start()
    clients[0].session.auth_key = AuthKey(b"a" * 256)
    clients[0].session.save()
    clients[0].session.close()
    await first.close()
    second = TelegramHistoryService(config, BOT_ID)
    await second.start()
    assert clients[1].session.auth_key.key == b"a" * 256
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert constructor.call_args.kwargs["receive_updates"] is False
    assert constructor.call_args.kwargs["catch_up"] is False
    assert constructor.call_args.kwargs["flood_sleep_threshold"] == 0
    assert constructor.call_args.kwargs["request_retries"] == 0
    clients[1].session.close()
    await second.close()


def test_session_path_rejects_symlink(settings, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    service = TelegramHistoryService(
        replace(settings, telegram_mtproto_session_path=str(link / "telegram.session")), BOT_ID
    )
    with pytest.raises(PermissionError):
        service._prepare_session_path()


@pytest.mark.parametrize("chat_id", [-456, CHAT_ID])
async def test_real_telethon_resolves_bot_api_ids_and_uses_exact_get_messages(
    settings, monkeypatch, chat_id
):
    requests = []
    channel = types.Channel(
        id=123,
        title="Private supergroup",
        photo=types.ChatPhotoEmpty(),
        date=datetime.now(UTC),
        access_hash=987,
        megagroup=True,
    )
    user = types.User(id=BOT_ID, first_name="Our bot", bot=True)
    message = types.Message(
        id=3,
        peer_id=utils.get_peer(chat_id),
        date=datetime.now(UTC),
        message="Actual TL response",
        from_id=types.PeerUser(BOT_ID),
    )

    async def rpc(self, request, *args, **kwargs):
        requests.append(request)
        if isinstance(request, functions.channels.GetChannelsRequest):
            assert request.id[0].channel_id == 123
            assert request.id[0].access_hash == 0
            return types.messages.Chats(chats=[channel])
        expected = (
            functions.messages.GetMessagesRequest
            if chat_id == -456
            else functions.channels.GetMessagesRequest
        )
        assert isinstance(request, expected)  # Fail on getHistory or any other RPC.
        assert request.id == [3]
        return types.messages.Messages(messages=[message], topics=[], chats=[channel], users=[user])

    monkeypatch.setattr(TelegramClient, "__call__", rpc)
    client = TelegramClient(MemorySession(), 123, "fake-hash", receive_updates=False)
    context = await TelegramHistoryService(settings, BOT_ID, client).get_reply_chain(chat_id, 7, 3)
    assert len(context.messages) == 1
    assert context.messages[0].text == "Actual TL response"
    assert context.messages[0].sender_name == "Our bot"
    assert context.messages[0].is_current_bot
    assert len(requests) == (1 if chat_id == -456 else 2)
