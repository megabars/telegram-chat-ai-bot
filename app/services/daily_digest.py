import asyncio
import json
import logging
import math
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import LinkPreviewOptions, Message

from app.config import Settings
from app.services.local_history import LocalHistoryService
from app.services.openai_service import OpenAIService
from app.utils.reply_context import (
    RECENT_LIMIT_MARKER,
    ContextMessage,
    ReplyContext,
    limit_reply_context,
    render_current_question,
)
from app.utils.telegram_text import split_telegram_text

logger = logging.getLogger(__name__)
POLL_SECONDS = 60
RETRY_SECONDS = 300
MAX_ATTEMPTS = 3
MARKER_RETENTION_DAYS = 30
DIGEST_PROMPT = """Подготовь краткую сводку чата за {date}. Верни разделы:
Итоги, Важные темы, Открытые вопросы, Договорённости и задачи.
Не выдумывай факты. Это лишь сохранённая часть переписки, не полный архив дня.
Отсутствие ответа не доказывает, что вопрос не решён. При недостатке данных явно
укажи неопределённость. Не объединяй независимые Conversation topics в одну беседу.
Ответ на русском, без markdown-таблиц."""


def select_digest_messages(
    messages: tuple[ContextMessage, ...], limit: int, prompt: str = ""
) -> ReplyContext:
    # A contiguous recent suffix avoids cherry-picking a question while dropping its answer.
    remaining = max(0, limit - len(render_current_question(prompt, None)))
    return limit_reply_context(
        ReplyContext(messages), remaining, len(messages), limit_marker=RECENT_LIMIT_MARKER
    )


def day_bounds(digest_date: date, timezone: ZoneInfo) -> tuple[int, int]:
    # Each midnight is converted separately: DST days need not contain 24 hours.
    start = datetime.combine(digest_date, time.min, timezone).astimezone(UTC)
    end = datetime.combine(digest_date + timedelta(days=1), time.min, timezone).astimezone(UTC)
    return int(start.timestamp()), int(end.timestamp())


class DailyDigestService:
    def __init__(
        self, settings: Settings, bot: Bot, history: LocalHistoryService, model: OpenAIService
    ):
        self.settings, self.bot, self.history, self.model = settings, bot, history, model
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._timezone = (
            ZoneInfo(settings.daily_digest_timezone) if settings.daily_digest_enabled else None
        )
        self._run_lock = asyncio.Lock()

    def _now(self) -> datetime:
        return datetime.now(UTC)

    async def start(self) -> None:
        if self.settings.daily_digest_enabled and self._task is None:
            self._task = asyncio.create_task(self._run(), name="daily-digest")
            logger.info("Daily digest enabled chats=%d", len(self.settings.daily_digest_chat_ids))

    async def shutdown(self, **_: object) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _tick(self) -> None:
        if not self.settings.daily_digest_enabled or self._timezone is None:
            return
        now = self._now().astimezone(self._timezone)
        target = datetime.combine(now.date(), time(self.settings.daily_digest_hour), self._timezone)
        if now >= target:
            await self.run_for_date(now.date() - timedelta(days=1))
            await self.history.prune_daily_digests(
                (now.date() - timedelta(days=MARKER_RETENTION_DAYS)).isoformat()
            )

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A transient SQL error must not silently kill the scheduler.
                logger.warning("Daily digest scheduler failed kind=%s", type(exc).__name__)
            await asyncio.sleep(POLL_SECONDS)

    async def run_for_date(self, digest_date: date) -> None:
        if not self.settings.daily_digest_enabled or self._timezone is None:
            return
        async with self._run_lock:
            start, end = day_bounds(digest_date, self._timezone)
            # Explicit opt-in IDs also let ready outboxes survive eviction of original messages.
            for chat_id in sorted(self.settings.daily_digest_chat_ids):
                if self.settings.digest_chat_allowed(chat_id):
                    await self._run_chat(chat_id, digest_date.isoformat(), start, end)

    async def _update(self, chat_id: int, day: str, status: str, **kwargs) -> None:
        await self.history.update_daily_digest(
            chat_id, day, status=status, now=int(self._now().timestamp()), **kwargs
        )

    async def _run_chat(self, chat_id: int, day: str, start: int, end: int) -> None:
        claimed = False
        sending = False
        try:
            state = await self.history.claim_daily_digest(
                chat_id,
                day,
                int(self._now().timestamp()),
                lease_seconds=math.ceil(self.settings.openai_timeout_seconds) + 120,
                max_attempts=MAX_ATTEMPTS,
            )
            if state is None:
                return
            claimed = True
            payload = state["payload"]
            if payload is None:
                messages = await self.history.get_daily_messages(chat_id, start, end)
                prompt = DIGEST_PROMPT.format(date=day)
                context = select_digest_messages(
                    messages, self.settings.daily_digest_context_chars, prompt
                )
                if not context.messages:
                    await self._update(chat_id, day, "empty")
                    return
                if not self.settings.digest_chat_allowed(chat_id):
                    return
                logger.info(
                    "Daily digest request chat_id=%d date=%s "
                    "available_messages=%d selected_messages=%d",
                    chat_id,
                    day,
                    len(messages),
                    len(context.messages),
                )
                answer = await self.model.generate_daily_digest(
                    prompt,
                    context,
                    max_output_tokens=self.settings.daily_digest_max_output_tokens,
                    context_chars=self.settings.daily_digest_context_chars,
                    chat_id=chat_id,
                    digest_date=day,
                )
                header = f"Сводка за {day} — по сохранённой части переписки, не полному архиву.\n\n"
                payload = self.history.redact(header + answer)
                await self._update(chat_id, day, "ready", payload=payload)
            parts = split_telegram_text(payload)
            receipts = json.loads(state["sent_messages"])
            # Replay local cache writes only, never confirmed Telegram sends.
            for receipt in receipts:
                await self.history.save_message(Message.model_validate_json(receipt))
            index = state["part_index"]
            last_id = state["message_id"]
            try:
                await self.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
            except (TelegramAPIError, TelegramNetworkError):
                pass  # Optional typing must not prevent delivery.
            for part in parts[index:]:
                if not self.settings.digest_chat_allowed(chat_id):
                    return
                await self._update(chat_id, day, "sending")
                sending = True
                sent = await self.bot.send_message(
                    chat_id=chat_id,
                    text=part,
                    parse_mode=None,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                # Persist acknowledgement before local cache work; restart resumes at the next part.
                receipts.append(sent.model_dump_json(exclude_none=True))
                index += 1
                last_id = sent.message_id
                await self._update(
                    chat_id,
                    day,
                    "ready",
                    part_index=index,
                    message_id=last_id,
                    sent_messages=json.dumps(receipts, ensure_ascii=False),
                )
                sending = False
                await self.history.save_message(sent)
            await self._update(chat_id, day, "sent", message_id=last_id)
            logger.info("Daily digest sent chat_id=%d date=%s parts=%d", chat_id, day, index)
        except asyncio.CancelledError:
            # Preparing leases recover; in-flight sends become uncertain on lease expiration.
            raise
        except Exception as exc:
            status = "retry"
            delay = RETRY_SECONDS
            if sending:
                if isinstance(exc, TelegramRetryAfter):
                    delay = max(delay, math.ceil(exc.retry_after))
                elif not isinstance(exc, TelegramAPIError) or isinstance(
                    exc, (TelegramNetworkError, TelegramServerError)
                ):
                    status = "uncertain"  # Delivery may already have happened.
            if claimed:
                try:
                    await self._update(
                        chat_id, day, status, retry_at=int(self._now().timestamp()) + delay
                    )
                except Exception as storage_exc:
                    logger.warning("Daily digest state failed kind=%s", type(storage_exc).__name__)
            logger.warning(
                "Daily digest failed chat_id=%d date=%s status=%s kind=%s",
                chat_id,
                day,
                status,
                type(exc).__name__,
            )
