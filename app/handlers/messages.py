import asyncio
import logging
import random
import re
from contextlib import asynccontextmanager, suppress

from aiogram import Bot, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions, Message, ReplyParameters

from app.config import Settings
from app.services.local_history import LocalHistoryService
from app.services.openai_service import ModelRateLimited, ModelUnavailable, OpenAIService
from app.services.telegram_history import TelegramHistoryService
from app.utils.context_command import (
    ContextCommandError,
    context_command_args,
    parse_context_command,
)
from app.utils.local_message import UnknownTopic, forum_topic_id
from app.utils.mentions import extract_prompt
from app.utils.rate_limit import RateLimiter
from app.utils.reply_context import (
    RECENT_LIMIT_MARKER,
    ReplyContext,
    build_reply_inputs,
    limit_reply_context,
    render_current_question,
)
from app.utils.telegram_text import split_telegram_text

logger = logging.getLogger(__name__)
EMPTY_PROMPT = "Напиши вопрос после упоминания меня 🙂"
LOCAL_RATE_LIMIT = "Слишком много запросов. Попробуй немного позже."
MODEL_RATE_LIMIT = "Сейчас слишком много запросов. Попробуй чуть позже."
TEMPORARY_ERROR = "Не удалось получить ответ от модели. Попробуй ещё раз немного позже."
TELEGRAM_ERRORS = (TelegramAPIError, TelegramNetworkError)
FUN_REPLIES = {
    "ептиль": ("Ептиль, бля 🙂", "Ну ептиль 😄", "Ёптиль-моптиль 🤷"),
    "ёптиль": ("Ептиль, бля 🙂", "Ну ептиль 😄", "Ёптиль-моптиль 🤷"),
    "сижу": (
        "Пержу, бля 💨", "Сижу, хуйню не несу 😎", "Пержу, не тужу 😄",
        "Сижу, бля, как мебель 🪑", "Сижу и медленно охуеваю 😵",
        "Сижу, пержу, жизнь идёт 💨", "Сижу, никого не трогаю 😌",
        "Сижу на жопе ровно 🍑", "Сижу, жду пиздеца ⏳", "Сижу, думаю о вечном 🧠",
        "Сижу, хуйнёй страдаю 🤹", "Сижу, делаю вид, что занят 💻",
        "Сижу, кайфую, не мешай 😎",
    ),
    "прикол": ("За щеку укол, бля 😄", "Прикол уровня «ну ё-моё» 🤦", "Нихуя себе шутка 🎭"),
    "почему": ("По кочану, бля 🥬", "Потому что гладиолус, ёпта 🌷", "Хуй знает, так вышло 🤷"),
    "кто": ("Конь в пальто, ёпта 🐴", "Дед Пихто, бля 🧓", "Хуй его знает 🤔"),
    "что": ("Через плечо, бля 🙃", "Нихуя, но интересно 🧐", "Секрет фирмы, ёпта 🤫"),
    "где": ("В пизде, где ж ещё? 🗺️", "В Караганде, бля 🏭", "Там, где нас нет 🌌"),
    "когда": ("Когда рак на горе свистнет, бля 🦞", "Когда-нибудь, хуй знает когда ⏳", "После дождичка в четверг 🌧️"),
    "зачем": ("Затем, бля 😌", "Чтобы было дохуя красиво ✨", "Для науки, ёпта 🔬"),
    "можно": ("Можно, но пиздец осторожно ⚠️", "Можно, хули нет 🙂", "Можно всё, но не всё сразу 🤷"),
    "спасибо": ("В карман не положишь, но заебись 🤝", "Пожалуйста, бля 😌", "Обращайся, ёпта 🫡"),
    "привет": ("Привет, ёпта 👋", "Салют, бля ✨", "Здорово, корова 🐮"),
    "алло": ("Алло, бля, приём 📞", "На связи, ёпта 📡", "Громче, нихуя не слышно 🎧"),
    "погнали": ("Погнали, ёпта 🚀", "Полный вперёд, бля 🚲", "Газ в пол 🔥"),
    "ладно": ("Ладно, хрен с ним 🤝", "Похуй, живём 😎", "Принято, бля 🫡"),
    "норм": (
        "Норм, не пиздец 😎", "Живём, бля 💪", "Уже заебись 👍", "Норм, бля, прорвёмся 💪",
        "Норм, пока не горим 🔥", "Норм, но можно и лучше 😏", "Норм, ебать, уже победа 🏆",
        "Норм, плюс-минус живой 🧟", "Норм, пойдёт на хлеб 🍞", "Норм, не жалуемся 😌",
        "Норм, но душа просит отпуск 🏖️", "Норм, лишь бы не хуже 🤞",
        "Норм, как после трёх энергетиков ⚡",
    ),
    "жесть": ("Жесть, аж пиздец 🫠", "Вот это разнос 🔥", "Моё почтение 😵"),
    "капец": ("Капец, но держимся 💪", "Ну всё, приплыли 🛟", "Пиздецометр зашкалил 📈"),
    "ахуеть": ("Ахуеть, но не встать 😵", "Вот это поворот, бля 🎢", "Слов нет, одни эмоции 🤯"),
    "чекаво": ("Да пиздец, но держимся 💪", "Живём, бля, не жалуемся 😎", "Всё по классике: работа-дом-охуевание 🏠", "Нормально, но хочется денег 💸", "Да так, космический бардак 🌌", "Потихоньку, без резких движений 🐢", "В режиме «не трогайте меня» 😴", "Всё заебись, пока не спрашиваешь 😄", "Да чё, жизнь происходит 🎢", "На минималках, но стабильно 🔋"),
    "чё каво": ("Да пиздец, но держимся 💪", "Живём, бля, не жалуемся 😎", "Всё по классике: работа-дом-охуевание 🏠"),
    "че каво": ("Да пиздец, но держимся 💪", "Живём, бля, не жалуемся 😎", "Всё по классике: работа-дом-охуевание 🏠"),
}
FUN_TRIGGER_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(key) for key in sorted(FUN_REPLIES, key=len, reverse=True)) + r")(?!\w)"
)


@asynccontextmanager
async def typing(bot: Bot, message: Message):
    async def send_actions() -> None:
        while True:
            try:
                await bot.send_chat_action(
                    chat_id=message.chat.id,
                    action=ChatAction.TYPING,
                    message_thread_id=message.message_thread_id,
                )
            except TELEGRAM_ERRORS as exc:
                logger.warning("Telegram typing failure kind=%s", type(exc).__name__)
                return
            await asyncio.sleep(4)

    task = asyncio.create_task(send_actions())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


class MessageHandler:
    def __init__(
        self,
        settings: Settings,
        bot_id: int,
        bot_username: str,
        service: OpenAIService,
        limiter: RateLimiter,
        history: TelegramHistoryService | None = None,
        local_history: LocalHistoryService | None = None,
    ) -> None:
        self.settings = settings
        self.bot_id = bot_id
        self.bot_username = bot_username
        self.service = service
        self.limiter = limiter
        self.history = history
        self.local_history = local_history
        self._active: set[asyncio.Task] = set()
        self._stopping = False
        self._fun_reply_order: dict[str, list[str]] = {}
        self._fun_reply_last: dict[str, str] = {}

    def router(self) -> Router:
        router = Router(name="mention_only")
        router.message.register(self.handle)
        router.edited_message.register(self.handle_edited)
        return router

    async def handle(self, message: Message, bot: Bot) -> None:
        if not self._allowed_message(message):
            return
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            args = context_command_args(message, self.bot_username)
            await self._store(message)
            fun_reply = self._fun_reply(message)
            if fun_reply is not None:
                await self._send(bot, message, fun_reply)
                return
            await self._handle_ai(message, bot, args)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Message processing failed kind=%s", type(exc).__name__)
            await self._send(bot, message, TEMPORARY_ERROR)
        finally:
            if task is not None:
                self._active.discard(task)

    def _allowed_message(self, message: Message) -> bool:
        if self._stopping or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return False
        if self.settings.allowed_chat_ids and message.chat.id not in self.settings.allowed_chat_ids:
            logger.debug("Ignored message chat_id=%d reason=chat_not_allowed", message.chat.id)
            return False
        return True

    def _fun_reply(self, message: Message) -> str | None:
        if not message.text or not message.from_user or message.from_user.is_bot or message.sender_chat:
            return None
        match = FUN_TRIGGER_PATTERN.search(message.text.casefold())
        if match is None:
            return None
        trigger = match.group()
        replies = FUN_REPLIES[trigger]
        queue = self._fun_reply_order.setdefault(trigger, [])
        if not queue:
            queue.extend(replies)
            random.shuffle(queue)
            if len(queue) > 1 and queue[-1] == self._fun_reply_last.get(trigger):
                queue[-1], queue[-2] = queue[-2], queue[-1]
        reply = queue.pop()
        self._fun_reply_last[trigger] = reply
        return reply

    async def _store(self, message: Message, *, edited: bool = False) -> None:
        if self.local_history is None or not self.settings.local_history_enabled:
            return
        try:
            if edited:
                await self.local_history.update_message(message)
            else:
                await self.local_history.save_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Local history write failed kind=%s", type(exc).__name__)

    async def handle_edited(self, message: Message) -> None:
        if not self._allowed_message(message):
            return
        task = asyncio.current_task()
        if task is not None:
            self._active.add(task)
        try:
            await self._store(message, edited=True)
        finally:
            if task is not None:
                self._active.discard(task)

    async def _handle_ai(self, message: Message, bot: Bot, command_args: str | None) -> None:
        # PRIVACY GATE: no OpenAI request OR reply-history fetch before these checks.
        if self._stopping or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return
        if self.settings.allowed_chat_ids and message.chat.id not in self.settings.allowed_chat_ids:
            logger.debug("Ignored message chat_id=%d reason=chat_not_allowed", message.chat.id)
            return
        if not message.from_user or message.from_user.is_bot or message.sender_chat is not None:
            return  # No identifiable human user for the per-user limiter.
        if message.from_user.id == self.bot_id or not message.text:
            return
        command = None
        if command_args is not None:
            try:
                command = parse_context_command(command_args, self.settings)
            except ContextCommandError as exc:
                await self._send(bot, message, str(exc))
                return
        prompt = (
            command.prompt
            if command
            else extract_prompt(message.text, message.entities, self.bot_username)
        )
        if prompt is None:
            logger.debug(
                "Ignored message chat_id=%d message_id=%d reason=bot_not_mentioned",
                message.chat.id,
                message.message_id,
            )
            return
        # Only context selected by an explicit mention/reply or /context reaches OpenAI.
        try:
            if not prompt:
                await self._send(bot, message, EMPTY_PROMPT)
                return
            if len(prompt) > self.settings.max_input_chars:
                await self._send(
                    bot,
                    message,
                    (
                        "Сообщение слишком длинное. Максимальная длина — "
                        f"{self.settings.max_input_chars} символов."
                    ),
                )
                return
            if not self.limiter.allow(message.from_user.id):
                await self._send(bot, message, LOCAL_RATE_LIMIT)
                return
            try:
                async with typing(bot, message):
                    if command is not None:
                        context = await self._recent_context(message, bot, command.limit, prompt)
                        if context is None:
                            return
                        self._log_ai_request(message)
                        answer = await self.service.generate(
                            prompt,
                            recent_context=context,
                            current_author=message.from_user.full_name,
                        )
                    else:
                        self._log_ai_request(message)
                        context = await self._reply_context(message)
                        answer = None
                    if command is None and context is not None and context.messages:
                        answer = await self.service.generate(
                            prompt,
                            context=context,
                            current_author=message.from_user.full_name,
                        )
                    elif command is None:
                        answer = await self.service.generate(prompt)
            except ModelRateLimited:
                await self._send(bot, message, MODEL_RATE_LIMIT)
                return
            except ModelUnavailable:
                await self._send(bot, message, TEMPORARY_ERROR)
                return
            await self._send(bot, message, answer)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log exception text/traceback: API errors may echo prompts.
            logger.error("Message processing failed kind=%s", type(exc).__name__)
            await self._send(bot, message, TEMPORARY_ERROR)
        # The outer handler tracks all cache writes and AI requests for shutdown.

    @staticmethod
    def _log_ai_request(message: Message) -> None:
        logger.info(
            "OpenAI request chat_id=%d user_id=%d message_id=%d",
            message.chat.id,
            message.from_user.id,
            message.message_id,
        )

    async def _recent_context(
        self, message: Message, bot: Bot, limit: int, prompt: str
    ) -> ReplyContext | None:
        if not self.settings.local_history_enabled or self.local_history is None:
            await self._send(bot, message, "Локальная история отключена.")
            return None
        try:
            topic_id = forum_topic_id(message) if self.settings.context_respect_topics else None
            messages = await self.local_history.get_recent_messages(
                message.chat.id, message.message_id, limit, message_thread_id=topic_id
            )
        except asyncio.CancelledError:
            raise
        except UnknownTopic:
            await self._send(bot, message, "Не удалось определить тему разговора.")
            return None
        except Exception as exc:
            logger.warning("Local history read failed kind=%s", type(exc).__name__)
            await self._send(bot, message, "Локальная история временно недоступна.")
            return None
        if not messages:
            await self._send(
                bot, message, "У меня пока нет сохранённой истории этого чата для анализа."
            )
            return None
        context = limit_reply_context(
            ReplyContext(tuple(messages)),
            max(
                0,
                self.settings.context_max_chars
                - len(render_current_question(prompt, message.from_user.full_name)),
            ),
            self.settings.context_command_max_messages,
            limit_marker=RECENT_LIMIT_MARKER,
        )
        logger.info(
            "Context request chat_id=%d user_id=%d requested_messages=%d "
            "selected_messages=%d context_chars=%d",
            message.chat.id,
            message.from_user.id,
            limit,
            len(context.messages),
            sum(
                len(item["content"])
                for item in build_reply_inputs(
                    prompt, context, message.from_user.full_name, limit_marker=RECENT_LIMIT_MARKER
                )
            ),
        )
        return context

    async def _reply_context(self, message: Message):
        if not self.settings.reply_context_enabled or self.history is None:
            return None
        parent = message.reply_to_message  # Direct parent only; no nested Bot API recursion.
        logger.info(
            "Reply scope chat_id=%d message_id=%d direct_reply=%s is_topic=%s "
            "thread_id=%s parent_thread_id=%s external_reply=%s",
            message.chat.id,
            message.message_id,
            parent is not None,
            bool(message.is_topic_message),
            message.message_thread_id,
            parent.message_thread_id if parent is not None else None,
            message.external_reply is not None,
        )
        if parent is None or message.external_reply is not None:
            return None
        if parent.chat.id != message.chat.id:
            return None
        topic_id = message.message_thread_id if message.is_topic_message else None
        if message.is_topic_message and topic_id is None:
            return None  # Unknown scope: fail closed.
        # Non-forum reply threads also carry message_thread_id; only forum topic IDs
        # constrain MTProto forum_topic/reply_to_top_id. They are different concepts.
        if (
            message.is_topic_message
            and parent.message_thread_id is not None
            and parent.message_thread_id != topic_id
        ):
            return None
        try:
            # The service owns the deadline so it can return ancestors fetched before timeout.
            return await self.history.get_reply_chain(
                message.chat.id,
                message.message_id,
                parent.message_id,
                message_thread_id=topic_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Reply chain truncated chat_id=%d message_id=%d "
                "reason=history_service_error kind=%s",
                message.chat.id,
                message.message_id,
                type(exc).__name__,
            )
            return None

    async def _send(self, bot: Bot, message: Message, text: str) -> None:
        for index, part in enumerate(split_telegram_text(text)):
            if not part.strip():
                continue  # Telegram rejects whitespace-only messages.
            kwargs = dict(
                chat_id=message.chat.id,
                text=part,
                parse_mode=None,
                message_thread_id=message.message_thread_id,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                reply_parameters=ReplyParameters(
                    message_id=message.message_id,
                    allow_sending_without_reply=True,
                )
                if index == 0
                else None,
            )
            try:
                sent = await bot.send_message(**kwargs)
            except TelegramRetryAfter as exc:
                # One bounded retry for Telegram only. Never regenerate via OpenAI.
                logger.warning("Telegram send rate limited retry_after=%d", exc.retry_after)
                if exc.retry_after > 30:
                    return
                await asyncio.sleep(exc.retry_after)
                try:
                    sent = await bot.send_message(**kwargs)
                except TELEGRAM_ERRORS as retry_exc:
                    logger.warning("Telegram send failure kind=%s", type(retry_exc).__name__)
                    return
            except TELEGRAM_ERRORS as exc:
                logger.warning("Telegram send failure kind=%s", type(exc).__name__)
                return
            if isinstance(sent, Message):
                # Preserve the known forum scope even if sendMessage omits optional flags.
                if message.is_topic_message:
                    sent = sent.model_copy(
                        update={
                            "is_topic_message": True,
                            "message_thread_id": message.message_thread_id,
                        }
                    )
                elif message.chat.is_forum:
                    sent = sent.model_copy(
                        update={"chat": sent.chat.model_copy(update={"is_forum": True})}
                    )
                await self._store(sent)

    async def shutdown(self, **_: object) -> None:
        self._stopping = True
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
