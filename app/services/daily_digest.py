import asyncio
import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.types import LinkPreviewOptions

from app.config import Settings
from app.services.local_history import LocalHistoryService
from app.services.openai_service import OpenAIService
from app.utils.reply_context import ContextMessage, ReplyContext
from app.utils.telegram_text import split_telegram_text

logger = logging.getLogger(__name__)

DIGEST_PROMPT = """Подготовь краткую сводку чата за {date}. Верни разделы:
Итоги, Важные темы, Открытые вопросы, Договорённости и задачи.
Не выдумывай факты. Считай вопрос открытым только если в переданной истории нет
ясного ответа. Если раздел пуст, так и напиши. Ответ на русском, без markdown-таблиц."""
IMPORTANT_MARKERS = ("?", "надо", "нужно", "важно", "решили", "сделать", "дедлайн")


def select_digest_messages(messages: tuple[ContextMessage, ...], limit: int) -> tuple[ContextMessage, ...]:
    selected: list[ContextMessage] = []
    used = 0
    for message in reversed(messages):
        important = any(marker in message.text.casefold() for marker in IMPORTANT_MARKERS)
        if not important and used >= limit // 2:
            continue
        size = len(message.text) + len(message.sender_name or "") + 80
        if size > limit - used:
            continue
        selected.append(message)
        used += size
        if used >= limit:
            break
    return tuple(reversed(selected))


class DailyDigestService:
    def __init__(self, settings: Settings, bot: Bot, history: LocalHistoryService, model: OpenAIService):
        self.settings, self.bot, self.history, self.model = settings, bot, history, model
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._timezone = ZoneInfo(settings.daily_digest_timezone)

    async def start(self) -> None:
        if self.settings.daily_digest_enabled:
            self._task = asyncio.create_task(self._run(), name="daily-digest")

    async def shutdown(self, **_: object) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stopping:
            now = datetime.now(self._timezone)
            target = datetime.combine(now.date(), time(self.settings.daily_digest_hour), self._timezone)
            if now >= target:
                await self.run_for_date(now.date() - timedelta(days=1))
                target += timedelta(days=1)
            await asyncio.sleep(max(1, (target - datetime.now(self._timezone)).total_seconds()))

    async def run_for_date(self, digest_date) -> None:
        start = datetime.combine(digest_date, time.min, self._timezone).astimezone(UTC)
        end = (start + timedelta(days=1)).astimezone(UTC)
        for chat_id in await self.history.get_daily_chat_ids(int(start.timestamp()), int(end.timestamp())):
            if not await self.history.start_daily_digest(chat_id, digest_date.isoformat()):
                continue
            try:
                messages = await self.history.get_daily_messages(
                    chat_id, int(start.timestamp()), int(end.timestamp())
                )
                context = ReplyContext(select_digest_messages(messages, self.settings.daily_digest_context_chars))
                if not context.messages:
                    continue
                await self.bot.send_chat_action(chat_id, ChatAction.TYPING)
                answer = await self.model.generate_daily_digest(
                    DIGEST_PROMPT.format(date=digest_date.isoformat()), context,
                    max_output_tokens=self.settings.daily_digest_max_output_tokens,
                    context_chars=self.settings.daily_digest_context_chars,
                )
                sent = None
                for part in split_telegram_text(answer):
                    sent = await self.bot.send_message(
                        chat_id=chat_id, text=part, parse_mode=None,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
                if sent is not None:
                    await self.history.complete_daily_digest(chat_id, digest_date.isoformat(), sent.message_id)
            except Exception as exc:
                logger.warning("Daily digest failed chat_id=%d kind=%s", chat_id, type(exc).__name__)
