import asyncio
import logging
import os
from pathlib import Path
from time import monotonic

from telethon import TelegramClient, errors, types, utils

from app.config import ConfigurationError, Settings
from app.utils.reply_context import (
    MARKER_RESERVE,
    ContextMessage,
    ReplyContext,
    fit_context_message,
    render_context_message,
)

logger = logging.getLogger(__name__)


class TelegramHistoryService:
    """Read-only exact-ID MTProto RPC, with no updates, getHistory, or message store."""

    def __init__(self, settings: Settings, bot_id: int, client: TelegramClient | None = None):
        self.settings = settings
        self.bot_id = bot_id
        self.client = client
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        self._flood_until = 0.0

    async def start(self) -> None:
        try:
            async with asyncio.timeout(self.settings.reply_context_fetch_timeout_seconds):
                if self.client is None:
                    if not self.settings.telegram_api_id or not self.settings.telegram_api_hash:
                        raise ConfigurationError(
                            "MTProto requires TELEGRAM_API_ID and TELEGRAM_API_HASH"
                        )
                    path = self._prepare_session_path()
                    self.client = TelegramClient(
                        str(path),
                        self.settings.telegram_api_id,
                        self.settings.telegram_api_hash,
                        receive_updates=False,
                        catch_up=False,
                        request_retries=0,
                        connection_retries=1,
                        flood_sleep_threshold=0,
                        raise_last_call_error=True,
                    )
                    # Cache peer identity/access hashes, never message bodies or media.
                    self.client.session.save_entities = True
                await self.client.connect()
                if not await self.client.is_user_authorized():
                    # No .start() phone/SMS prompts, no user-account authorization.
                    await self.client.sign_in(bot_token=self.settings.telegram_bot_token)
                me = await self.client.get_me()
                if me is None or not me.bot or me.id != self.bot_id:
                    raise ConfigurationError("MTProto session must belong to the current bot")
                logger.info("MTProto connected mode=rpc_only bot_id=%d", self.bot_id)
        except BaseException:
            await self.close()
            raise

    def _prepare_session_path(self) -> Path:
        path = Path(self.settings.telegram_mtproto_session_path).expanduser()
        if not str(path).endswith(".session"):
            path = path.with_name(path.name + ".session")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink():
            raise PermissionError("MTProto session directory must not be a symlink")
        if path.parent.stat().st_uid != os.geteuid():
            raise PermissionError("MTProto session directory must belong to service user")
        path.parent.chmod(0o700)
        if path.is_symlink():
            raise PermissionError("MTProto session must not be a symlink")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.geteuid():
                raise PermissionError("MTProto session must belong to service user")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return path

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    async def get_reply_chain(
        self,
        chat_id: int,
        message_id: int,
        direct_reply_message_id: int | None,
        *,
        message_thread_id: int | None = None,
    ) -> ReplyContext:
        if not self.settings.reply_context_enabled or direct_reply_message_id is None:
            return ReplyContext()
        if self.settings.allowed_chat_ids and chat_id not in self.settings.allowed_chat_ids:
            return ReplyContext()
        if self.client is None:
            return ReplyContext(truncation_reason="not_connected")

        started = monotonic()
        collected: list[ContextMessage] = []  # Nearest parent first.
        seen = {message_id}
        next_id = direct_reply_message_id
        reason = None
        remaining = max(0, self.settings.reply_context_max_chars - MARKER_RESERVE)
        try:
            # One overall deadline includes queueing, entity resolution, and all RPC.
            async with asyncio.timeout(self.settings.reply_context_fetch_timeout_seconds):
                async with self._semaphore:
                    if monotonic() < self._flood_until:
                        reason = "flood_wait"
                    else:
                        _, peer_type = utils.resolve_id(chat_id)
                        if peer_type not in (types.PeerChat, types.PeerChannel):
                            raise ValueError("Reply context requires a group peer")
                        # Telethon handles marked Bot API IDs and bot zero-hash resolution.
                        peer = await self.client.get_input_entity(chat_id)
                        if utils.get_peer_id(peer) != chat_id:
                            reason = "invalid_peer"
                        else:
                            while next_id is not None:
                                if next_id <= 0:
                                    reason = "invalid_message_id"
                                    break
                                if next_id in seen:
                                    reason = "cycle"
                                    break
                                if len(collected) >= self.settings.reply_context_max_depth:
                                    reason = "depth_limit"
                                    break
                                if remaining <= 0:
                                    reason = "char_limit"
                                    break
                                seen.add(next_id)
                                message = await self.client.get_messages(peer, ids=next_id)
                                if message is None or isinstance(message, types.MessageEmpty):
                                    reason = "message_unavailable"
                                    break
                                if message.id != next_id:
                                    reason = "invalid_message_id"
                                    break
                                if utils.get_peer_id(message.peer_id) != chat_id:
                                    reason = "cross_chat"
                                    break
                                if not self._same_topic(message, message_thread_id):
                                    reason = "cross_topic"
                                    break
                                context_message = self._context_message(message)
                                fitted = fit_context_message(context_message, remaining)
                                if fitted is None:
                                    reason = "char_limit"
                                    break
                                collected.append(fitted)
                                remaining -= len(render_context_message(fitted))
                                if fitted != context_message:
                                    reason = "char_limit"
                                    break
                                header = message.reply_to
                                if header and (
                                    header.reply_to_scheduled
                                    or header.reply_from is not None
                                    or (
                                        header.reply_to_peer_id is not None
                                        and utils.get_peer_id(header.reply_to_peer_id) != chat_id
                                    )
                                ):
                                    reason = "external_reply"
                                    break
                                next_id = context_message.reply_to_message_id
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            reason = "timeout"
        except errors.FloodWaitError as exc:
            # Stop even for short waits; future requests respect the server backoff.
            self._flood_until = monotonic() + max(0, exc.seconds)
            reason = "flood_wait"
        except Exception as exc:
            reason = "fetch_error"
            logger.warning("MTProto fetch failed kind=%s", type(exc).__name__)

        if reason:
            logger.warning(
                "Reply chain truncated chat_id=%d message_id=%d reason=%s",
                chat_id,
                message_id,
                reason,
            )
        logger.info(
            "Reply context collected chat_id=%d current_message_id=%d "
            "ancestor_count=%d context_chars=%d duration_ms=%d",
            chat_id,
            message_id,
            len(collected),
            sum(len(render_context_message(m)) for m in collected),
            (monotonic() - started) * 1000,
        )
        return ReplyContext(tuple(reversed(collected)), reason)

    @staticmethod
    def _same_topic(message, expected: int | None) -> bool:
        header = message.reply_to
        if isinstance(message, types.MessageService) and isinstance(
            message.action,
            types.MessageActionTopicCreate,
        ):
            actual = message.id
        elif header and header.forum_topic:
            actual = header.reply_to_top_id or header.reply_to_msg_id
            if actual is None:
                return False
        else:
            actual = None  # General topic is represented without forum_topic in MTProto.
        if expected == 1:
            return actual in (None, 1)
        return actual == expected

    def _context_message(self, message) -> ContextMessage:
        sender = message.sender  # Provided by getMessages users/chats; no get_sender RPC.
        sender_id = message.sender_id
        username = getattr(sender, "username", None)
        name = " ".join(
            value
            for value in (
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            )
            if value
        ).strip()
        name = (
            name
            or getattr(sender, "title", None)
            or (f"@{username}" if username else f"User {sender_id}")
        )
        return ContextMessage(
            message_id=message.id,
            sender_id=sender_id,
            sender_name=name,
            sender_username=username,
            timestamp=message.date,
            text=getattr(message, "message", None) or self._media_placeholder(message),
            is_bot=bool(getattr(sender, "bot", False)) or sender_id == self.bot_id,
            is_current_bot=sender_id == self.bot_id,
            reply_to_message_id=message.reply_to.reply_to_msg_id if message.reply_to else None,
        )

    @staticmethod
    def _media_placeholder(message) -> str:
        if isinstance(message, types.MessageService):
            return "[service message]"
        media = message.media
        if isinstance(media, types.MessageMediaPhoto):
            return "[photo]"
        if isinstance(media, types.MessageMediaDocument):
            attributes = getattr(media.document, "attributes", ()) or ()
            for attribute in attributes:
                if isinstance(attribute, types.DocumentAttributeSticker):
                    return "[sticker]"
                if isinstance(attribute, types.DocumentAttributeAudio):
                    return "[voice message]" if attribute.voice else "[audio]"
                if isinstance(attribute, types.DocumentAttributeVideo):
                    return "[video]"
            return "[document]"
        return "[media]" if media else "[empty message]"
