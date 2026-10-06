import asyncio
import logging
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from aiogram.enums import ChatType
from aiogram.types import Message

from app.config import Settings
from app.utils.local_message import forum_topic_id, local_message_content
from app.utils.reply_context import ContextMessage

logger = logging.getLogger(__name__)
SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_messages (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    message_thread_id INTEGER,
    sender_id INTEGER,
    sender_name TEXT,
    sender_username TEXT,
    text TEXT,
    timestamp INTEGER NOT NULL,
    reply_to_message_id INTEGER,
    is_bot INTEGER NOT NULL DEFAULT 0,
    is_our_bot INTEGER NOT NULL DEFAULT 0,
    edit_timestamp INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_chat_message
ON telegram_messages(chat_id, message_id DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_chat_topic_message
ON telegram_messages(chat_id, message_thread_id, message_id DESC);
CREATE INDEX IF NOT EXISTS idx_telegram_messages_timestamp
ON telegram_messages(timestamp, chat_id);
CREATE TABLE IF NOT EXISTS daily_digests (
    chat_id INTEGER NOT NULL,
    digest_date TEXT NOT NULL,
    status TEXT NOT NULL,
    message_id INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    retry_at INTEGER NOT NULL DEFAULT 0,
    payload TEXT,
    part_index INTEGER NOT NULL DEFAULT 0,
    sent_messages TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (chat_id, digest_date)
);
"""
INSERT = """INSERT OR IGNORE INTO telegram_messages (
    chat_id, message_id, message_thread_id, sender_id, sender_name, sender_username,
    text, timestamp, reply_to_message_id, is_bot, is_our_bot, edit_timestamp
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
UPDATE = """UPDATE telegram_messages SET message_thread_id=?, sender_id=?, sender_name=?,
    sender_username=?, text=?, timestamp=?, reply_to_message_id=?, is_bot=?, is_our_bot=?,
    edit_timestamp=? WHERE chat_id=? AND message_id=? AND edit_timestamp<=?"""


class LocalHistoryService:
    """Persistent rolling cache. Only local SQL; no network or AI operations."""

    def __init__(self, settings: Settings, bot_id: int):
        self.settings = settings
        self.bot_id = bot_id
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._counts: dict[int, int] = {}

    async def start(self) -> None:
        if not self.settings.local_history_enabled or self._connection is not None:
            return
        path = self._prepare_path()
        try:
            self._connection = await aiosqlite.connect(path, isolation_level=None)
            self._connection.row_factory = aiosqlite.Row
            async with self._connection.execute("PRAGMA journal_mode=WAL") as cursor:
                if (await cursor.fetchone())[0].lower() != "wal":
                    raise RuntimeError("SQLite WAL is unavailable")
            await self._connection.execute("PRAGMA busy_timeout=5000")
            await self._connection.execute("PRAGMA synchronous=NORMAL")
            await self._connection.execute("PRAGMA foreign_keys=ON")
            await self._connection.executescript(SCHEMA)
            await self._migrate_digest_schema()
            rows = await self._connection.execute_fetchall(
                "SELECT chat_id, COUNT(*) AS count FROM telegram_messages GROUP BY chat_id"
            )
            self._counts = {row["chat_id"]: row["count"] for row in rows}
            for chat_id in tuple(self._counts):
                await self.cleanup_chat(chat_id)
            logger.info("Local history started mode=sqlite_wal")
        except BaseException:
            await self.close()
            raise

    def _prepare_path(self) -> Path:
        path = Path(self.settings.local_history_db_path).expanduser().absolute()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            raise PermissionError("SQLite state paths must not be symlinks")
        if path.parent.stat().st_uid != os.geteuid():
            raise PermissionError("SQLite directory must belong to service user")
        path.parent.chmod(0o700)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.geteuid():
                raise PermissionError("SQLite database must belong to service user")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        return path

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                try:
                    await self._connection.close()  # Writes commit individually before returning.
                finally:
                    self._connection = None
                    self._counts.clear()

    def _allowed(self, chat_id: int) -> bool:
        return self.settings.local_history_enabled and (
            not self.settings.allowed_chat_ids or chat_id in self.settings.allowed_chat_ids
        )

    async def save_message(self, message: Message, *, edited: bool = False) -> bool:
        if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
        if not self._allowed(message.chat.id):
            return False
        content = local_message_content(message, self.bot_id)
        if content is None:
            return False
        topic_id = forum_topic_id(message)

        content = replace(
            content,
            text=self.redact(content.text),
            sender_name=self.redact(content.sender_name),
            sender_username=self.redact(content.sender_username),
        )
        edit_timestamp = 0
        if edited:
            # aiogram 3.31 exposes edit_date as a Unix integer; date is a datetime.
            edit_date = message.edit_date or datetime.now(UTC)
            edit_timestamp = (
                int(edit_date.timestamp()) if isinstance(edit_date, datetime) else int(edit_date)
            )
        async with self._lock:
            await self._finish_write(
                self._save_transaction(message.chat.id, topic_id, content, edit_timestamp)
            )
        logger.debug(
            "History message stored chat_id=%d message_id=%d thread_id=%s chars=%d",
            message.chat.id,
            message.message_id,
            topic_id,
            len(content.text),
        )
        return True

    async def update_message(self, message: Message) -> bool:
        return await self.save_message(message, edited=True)

    @staticmethod
    async def _finish_write(operation):
        # Cancellation must not abandon an aiosqlite worker commit halfway through.
        task = asyncio.create_task(operation)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _db(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Local history is not started")
        return self._connection

    async def _save_transaction(self, chat_id, topic_id, content, edit_timestamp):
        db = self._db()
        before_count = self._counts.get(chat_id, 0)
        values = (
            chat_id,
            content.message_id,
            topic_id,
            content.sender_id,
            content.sender_name,
            content.sender_username,
            content.text,
            int(content.timestamp.timestamp()),
            content.reply_to_message_id,
            int(content.is_bot),
            int(content.is_current_bot),
            edit_timestamp,
        )
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(INSERT, values) as cursor:
                inserted = cursor.rowcount
            if not inserted:
                await db.execute(UPDATE, (*values[2:], chat_id, content.message_id, edit_timestamp))
            count = before_count + inserted
            deleted = await self._cleanup_sql(chat_id, count)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        self._counts[chat_id] = count - deleted
        self._log_cleanup(chat_id, count, deleted)

    async def _cleanup_sql(self, chat_id: int, count: int) -> int:
        if count < self.settings.local_history_cleanup_threshold:
            return 0
        db = self._db()
        async with db.execute(
            "SELECT message_id FROM telegram_messages WHERE chat_id=? "
            "ORDER BY message_id DESC LIMIT 1 OFFSET ?",
            (chat_id, self.settings.local_history_max_messages - 1),
        ) as cursor:
            boundary = await cursor.fetchone()
        if boundary is None:
            return 0
        async with db.execute(
            "DELETE FROM telegram_messages WHERE chat_id=? AND message_id<?",
            (chat_id, boundary[0]),
        ) as cursor:
            return cursor.rowcount

    async def cleanup_chat(self, chat_id: int) -> int:
        async with self._lock:

            async def cleanup():
                db = self._db()
                before = self._counts.get(chat_id, 0)
                if before < self.settings.local_history_cleanup_threshold:
                    return 0
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    deleted = await self._cleanup_sql(chat_id, before)
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise
                self._counts[chat_id] = before - deleted
                self._log_cleanup(chat_id, before, deleted)
                return deleted

            return await self._finish_write(cleanup())

    @staticmethod
    def _log_cleanup(chat_id, before, deleted):
        if deleted:
            logger.info(
                "Local history cleanup chat_id=%d before_count=%d after_count=%d deleted_count=%d",
                chat_id,
                before,
                before - deleted,
                deleted,
            )

    async def get_recent_messages(
        self,
        chat_id: int,
        before_message_id: int,
        limit: int,
        message_thread_id: int | None = None,
    ) -> tuple[ContextMessage, ...]:
        if not self._allowed(chat_id):
            return ()
        if not 0 < limit <= self.settings.context_command_max_messages:
            raise ValueError("Recent-message limit is outside configured bounds")
        query = "SELECT * FROM telegram_messages WHERE chat_id=? AND message_id<?"
        parameters = [chat_id, before_message_id]
        if self.settings.context_respect_topics:
            query += " AND message_thread_id IS ?"
            parameters.append(message_thread_id)
        query += " ORDER BY message_id DESC LIMIT ?"
        parameters.append(limit)
        async with self._lock:
            rows = await self._db().execute_fetchall(query, parameters)
        # Materialize immutable values before any OpenAI/network work.
        return tuple(
            ContextMessage(
                message_id=row["message_id"],
                sender_id=row["sender_id"],
                sender_name=row["sender_name"],
                sender_username=row["sender_username"],
                timestamp=datetime.fromtimestamp(row["timestamp"], UTC),
                text=row["text"],
                reply_to_message_id=row["reply_to_message_id"],
                is_bot=bool(row["is_bot"]),
                is_current_bot=bool(row["is_our_bot"]),
            )
            for row in reversed(rows)
        )

    def redact(self, value: str | None) -> str | None:
        for secret in (
            self.settings.telegram_bot_token,
            self.settings.openai_api_key,
            self.settings.telegram_api_hash,
        ):
            if secret and value:
                value = value.replace(secret, "[REDACTED]")
        return value

    async def _migrate_digest_schema(self) -> None:
        # Upgrade the original PR schema without resetting existing delivery markers.
        db = self._db()
        columns = {
            row["name"] for row in await db.execute_fetchall("PRAGMA table_info(daily_digests)")
        }
        additions = {
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "updated_at": "INTEGER NOT NULL DEFAULT 0",
            "retry_at": "INTEGER NOT NULL DEFAULT 0",
            "payload": "TEXT",
            "part_index": "INTEGER NOT NULL DEFAULT 0",
            "sent_messages": "TEXT NOT NULL DEFAULT '[]'",
        }
        for name, definition in additions.items():
            if name not in columns:
                # Column names/types are application constants, never Telegram input.
                await db.execute(f"ALTER TABLE daily_digests ADD COLUMN {name} {definition}")

    async def get_daily_chat_ids(self, start_timestamp: int, end_timestamp: int) -> tuple[int, ...]:
        if not self.settings.daily_digest_enabled:
            return ()
        async with self._lock:
            rows = await self._db().execute_fetchall(
                "SELECT DISTINCT chat_id FROM telegram_messages WHERE timestamp>=? AND timestamp<?",
                (start_timestamp, end_timestamp),
            )
        return tuple(
            row["chat_id"] for row in rows if self.settings.digest_chat_allowed(row["chat_id"])
        )

    async def get_daily_messages(
        self, chat_id: int, start_timestamp: int, end_timestamp: int
    ) -> tuple[ContextMessage, ...]:
        if not self.settings.digest_chat_allowed(chat_id):
            return ()
        async with self._lock:
            rows = await self._db().execute_fetchall(
                "SELECT * FROM telegram_messages WHERE chat_id=? AND timestamp>=? AND timestamp<? "
                "ORDER BY message_id",
                (chat_id, start_timestamp, end_timestamp),
            )
        topics = {}
        messages = []
        for row in rows:
            topic = row["message_thread_id"]
            label = ""
            if topic is not None:
                if topic not in topics:
                    topics[topic] = f"Conversation topic {len(topics) + 1}"
                label = f"[{topics[topic]}] "
            messages.append(
                ContextMessage(
                    message_id=row["message_id"],
                    sender_id=row["sender_id"],
                    sender_name=row["sender_name"],
                    sender_username=row["sender_username"],
                    timestamp=datetime.fromtimestamp(row["timestamp"], UTC),
                    text=label + row["text"],
                    reply_to_message_id=row["reply_to_message_id"],
                    is_bot=bool(row["is_bot"]),
                    is_current_bot=bool(row["is_our_bot"]),
                )
            )
        return tuple(messages)

    async def claim_daily_digest(
        self, chat_id: int, digest_date: str, now: int, *, lease_seconds: int, max_attempts: int
    ) -> dict | None:
        if not self.settings.digest_chat_allowed(chat_id):
            return None
        async with self._lock:

            async def claim():
                db = self._db()
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await db.execute(
                        "INSERT OR IGNORE INTO daily_digests(chat_id,digest_date,status) "
                        "VALUES (?,?,'pending')",
                        (chat_id, digest_date),
                    )
                    rows = await db.execute_fetchall(
                        "SELECT * FROM daily_digests WHERE chat_id=? AND digest_date=?",
                        (chat_id, digest_date),
                    )
                    row = dict(rows[0])
                    stale = row["updated_at"] <= now - lease_seconds
                    if row["status"] == "sending" and stale:
                        # Lost send acknowledgement cannot safely be retried automatically.
                        await db.execute(
                            "UPDATE daily_digests SET status='uncertain',updated_at=? "
                            "WHERE chat_id=? AND digest_date=?",
                            (now, chat_id, digest_date),
                        )
                        logger.warning(
                            "Daily digest delivery uncertain chat_id=%d date=%s",
                            chat_id,
                            digest_date,
                        )
                        await db.commit()
                        return None
                    if (
                        row["status"] in {"sent", "empty", "uncertain"}
                        or row["attempts"] >= max_attempts
                        or row["retry_at"] > now
                        or (row["status"] in {"preparing", "sending", "ready"} and not stale)
                    ):
                        await db.commit()
                        return None
                    row.update(
                        status="ready" if row["payload"] else "preparing",
                        attempts=row["attempts"] + 1,
                        updated_at=now,
                    )
                    await db.execute(
                        "UPDATE daily_digests SET status=?,attempts=?,updated_at=? "
                        "WHERE chat_id=? AND digest_date=?",
                        (row["status"], row["attempts"], now, chat_id, digest_date),
                    )
                    await db.commit()
                    return row
                except BaseException:
                    await db.rollback()
                    raise

            return await self._finish_write(claim())

    async def update_daily_digest(
        self,
        chat_id: int,
        digest_date: str,
        *,
        status: str,
        now: int,
        retry_at: int = 0,
        payload: str | None = None,
        sent_messages: str | None = None,
        part_index: int | None = None,
        message_id: int | None = None,
    ) -> None:
        if status not in {"ready", "sending", "retry", "sent", "empty", "uncertain"}:
            raise ValueError("Invalid digest status")
        async with self._lock:

            async def update():
                db = self._db()
                try:
                    await db.execute("BEGIN IMMEDIATE")
                    await db.execute(
                        "UPDATE daily_digests SET status=?,updated_at=?,retry_at=?,"
                        "payload=COALESCE(?,payload),sent_messages=COALESCE(?,sent_messages),"
                        "part_index=COALESCE(?,part_index),message_id=COALESCE(?,message_id) "
                        "WHERE chat_id=? AND digest_date=?",
                        (
                            status,
                            now,
                            retry_at,
                            self.redact(payload),
                            self.redact(sent_messages),
                            part_index,
                            message_id,
                            chat_id,
                            digest_date,
                        ),
                    )
                    await db.commit()
                except BaseException:
                    await db.rollback()
                    raise

            await self._finish_write(update())

    async def prune_daily_digests(self, before_date: str) -> None:
        async with self._lock:

            async def prune():
                async with self._db().execute(
                    "DELETE FROM daily_digests WHERE digest_date<?", (before_date,)
                ):
                    pass

            await self._finish_write(prune())
