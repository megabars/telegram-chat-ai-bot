import asyncio
import logging
import random
import re
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from io import BytesIO
from time import monotonic
from zoneinfo import ZoneInfo

from aiogram import Bot, Router
from aiogram.enums import ChatAction, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import BufferedInputFile, LinkPreviewOptions, Message, PhotoSize, ReplyParameters

from app.config import Settings
from app.services.local_history import LocalHistoryService
from app.services.openai_service import ModelRateLimited, ModelUnavailable, OpenAIService
from app.services.telegram_history import TelegramHistoryService
from app.utils.context_command import (
    ContextCommandError,
    command_args,
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
PHOTO_DISABLED = "Анализ фотографий пока отключён."
PHOTO_UNAVAILABLE = "Не удалось загрузить фото. Попробуй отправить его ещё раз."
PHOTO_TOO_LARGE = "Фото слишком большое для анализа. Отправь снимок меньше 10 МБ."
PHOTO_WRONG_SCOPE = "Могу анализировать только фото из этого чата и этой темы."
PHOTO_USER_LIMIT = "Твой суточный лимит анализа фото исчерпан. Попробуй завтра."
PHOTO_CHAT_LIMIT = "Суточный лимит анализа фото для чата исчерпан. Попробуйте завтра."
IMAGE_DISABLED = "Генерация изображений пока отключена."
IMAGE_USAGE = (
    "Напиши /image и описание картинки; /edit с фото — что изменить; "
    "/mix с альбомом из 2–4 фото — как их объединить."
)
IMAGE_USER_LIMIT = "Твой суточный лимит генерации изображений исчерпан. Попробуй завтра."
IMAGE_CHAT_LIMIT = "Суточный лимит генерации изображений для чата исчерпан. Попробуйте завтра."
MIX_WRONG_SCOPE = "Для /mix ответь вторым фото на первое фото в этом чате и теме."
MIX_ALBUM_SIZE = "Для /mix нужно 2–4 фото в одном альбоме."
EDIT_WRONG_SCOPE = "Для /edit приложи фото или ответь командой на фото в этом чате и теме."
IMAGE_TOO_LARGE = "Получившееся изображение слишком большое для Telegram."
MAX_PHOTO_BYTES = 10 * 1024 * 1024
TELEGRAM_ERRORS = (TelegramAPIError, TelegramNetworkError)
PHOTO_DOWNLOAD_ERRORS = (*TELEGRAM_ERRORS, RuntimeError)
FUN_REPLIES = {
    "ептиль": ("Ептиль, бля 🙂", "Ну ептиль 😄", "Ёптиль-моптиль 🤷"),
    "ёптиль": ("Ептиль, бля 🙂", "Ну ептиль 😄", "Ёптиль-моптиль 🤷"),
    "сижу": (
        "Пержу, бля 💨",
        "Сижу, хуйню не несу 😎",
        "Пержу, не тужу 😄",
        "Сижу, бля, как мебель 🪑",
        "Сижу и медленно охуеваю 😵",
        "Сижу, пержу, жизнь идёт 💨",
        "Сижу, никого не трогаю 😌",
        "Сижу на жопе ровно 🍑",
        "Сижу, жду пиздеца ⏳",
        "Сижу, думаю о вечном 🧠",
        "Сижу, хуйнёй страдаю 🤹",
        "Сижу, делаю вид, что занят 💻",
        "Сижу, кайфую, не мешай 😎",
    ),
    "прикол": ("За щеку укол, бля 😄", "Прикол уровня «ну ё-моё» 🤦", "Нихуя себе шутка 🎭"),
    "почему": ("По кочану, бля 🥬", "Потому что гладиолус, ёпта 🌷", "Хуй знает, так вышло 🤷"),
    "кто": ("Конь в пальто, ёпта 🐴", "Дед Пихто, бля 🧓", "Хуй его знает 🤔"),
    "что": ("Через плечо, бля 🙃", "Нихуя, но интересно 🧐", "Секрет фирмы, ёпта 🤫"),
    "где": ("В пизде, где ж ещё? 🗺️", "В Караганде, бля 🏭", "Там, где нас нет 🌌"),
    "когда": (
        "Когда рак на горе свистнет, бля 🦞",
        "Когда-нибудь, хуй знает когда ⏳",
        "После дождичка в четверг 🌧️",
    ),
    "зачем": ("Затем, бля 😌", "Чтобы было дохуя красиво ✨", "Для науки, ёпта 🔬"),
    "можно": (
        "Можно, но пиздец осторожно ⚠️",
        "Можно, хули нет 🙂",
        "Можно всё, но не всё сразу 🤷",
    ),
    "спасибо": ("В карман не положишь, но заебись 🤝", "Пожалуйста, бля 😌", "Обращайся, ёпта 🫡"),
    "привет": ("Привет, ёпта 👋", "Салют, бля ✨", "Здорово, корова 🐮"),
    "алло": ("Алло, бля, приём 📞", "На связи, ёпта 📡", "Громче, нихуя не слышно 🎧"),
    "погнали": ("Погнали, ёпта 🚀", "Полный вперёд, бля 🚲", "Газ в пол 🔥"),
    "ладно": ("Ладно, хрен с ним 🤝", "Похуй, живём 😎", "Принято, бля 🫡"),
    "норм": (
        "Норм, не пиздец 😎",
        "Живём, бля 💪",
        "Уже заебись 👍",
        "Норм, бля, прорвёмся 💪",
        "Норм, пока не горим 🔥",
        "Норм, но можно и лучше 😏",
        "Норм, ебать, уже победа 🏆",
        "Норм, плюс-минус живой 🧟",
        "Норм, пойдёт на хлеб 🍞",
        "Норм, не жалуемся 😌",
        "Норм, но душа просит отпуск 🏖️",
        "Норм, лишь бы не хуже 🤞",
        "Норм, как после трёх энергетиков ⚡",
    ),
    "жесть": ("Жесть, аж пиздец 🫠", "Вот это разнос 🔥", "Моё почтение 😵"),
    "капец": ("Капец, но держимся 💪", "Ну всё, приплыли 🛟", "Пиздецометр зашкалил 📈"),
    "ахуеть": ("Ахуеть, но не встать 😵", "Вот это поворот, бля 🎢", "Слов нет, одни эмоции 🤯"),
    "чекаво": (
        "Да пиздец, но держимся 💪",
        "Живём, бля, не жалуемся 😎",
        "Всё по классике: работа-дом-охуевание 🏠",
        "Нормально, но хочется денег 💸",
        "Да так, космический бардак 🌌",
        "Потихоньку, без резких движений 🐢",
        "В режиме «не трогайте меня» 😴",
        "Всё заебись, пока не спрашиваешь 😄",
        "Да чё, жизнь происходит 🎢",
        "На минималках, но стабильно 🔋",
    ),
    "чё каво": (
        "Да пиздец, но держимся 💪",
        "Живём, бля, не жалуемся 😎",
        "Всё по классике: работа-дом-охуевание 🏠",
    ),
    "че каво": (
        "Да пиздец, но держимся 💪",
        "Живём, бля, не жалуемся 😎",
        "Всё по классике: работа-дом-охуевание 🏠",
    ),
}
FUN_TRIGGER_PATTERN = re.compile(
    r"(?<!\w)(?:"
    + "|".join(re.escape(key) for key in sorted(FUN_REPLIES, key=len, reverse=True))
    + r")(?!\w)"
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
        self._albums: dict[tuple[int, str], tuple[float, dict[int, Message]]] = {}

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
            self._record_album_photo(message)
            args = context_command_args(message, self.bot_username)
            image_args = command_args(message, self.bot_username, "/image")
            mix_args = command_args(message, self.bot_username, "/mix")
            if mix_args is None:
                mix_args = command_args(message, self.bot_username, "/mix", caption=True)
            edit_args = command_args(message, self.bot_username, "/edit")
            if edit_args is None:
                edit_args = command_args(message, self.bot_username, "/edit", caption=True)
            await self._store(message)
            if image_args is not None or mix_args is not None or edit_args is not None:
                await self._handle_image(message, bot, image_args, mix_args, edit_args)
                return
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
        if (
            not message.text
            or not message.from_user
            or message.from_user.is_bot
            or message.sender_chat
        ):
            return None
        # Commands and mentions keep their existing routing, validation and rate limits.
        if any(
            entity.type in {"bot_command", "mention", "text_mention"}
            for entity in message.entities or ()
        ):
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
        content = message.text if message.text is not None else message.caption
        entities = message.entities if message.text is not None else message.caption_entities
        if message.from_user.id == self.bot_id or not content:
            return
        command = None
        if command_args is not None:
            try:
                command = parse_context_command(command_args, self.settings)
            except ContextCommandError as exc:
                await self._send(bot, message, str(exc))
                return
        prompt = command.prompt if command else extract_prompt(content, entities, self.bot_username)
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
                    has_photo = bool(
                        message.photo
                        or (message.reply_to_message and message.reply_to_message.photo)
                    )
                    if command is None and has_photo:
                        if not self.settings.photo_analysis_enabled or self.local_history is None:
                            await self._send(bot, message, PHOTO_DISABLED)
                            return
                        photo = self._photo_for_question(message)
                        if photo is None:
                            await self._send(bot, message, PHOTO_WRONG_SCOPE)
                            return
                        try:
                            image = await self._download_photo(bot, photo)
                        except ValueError:
                            await self._send(bot, message, PHOTO_TOO_LARGE)
                            return
                        except PHOTO_DOWNLOAD_ERRORS as exc:
                            logger.warning("Photo download failed kind=%s", type(exc).__name__)
                            await self._send(bot, message, PHOTO_UNAVAILABLE)
                            return
                        day = (
                            datetime.now(UTC)
                            .astimezone(ZoneInfo(self.settings.photo_daily_timezone))
                            .date()
                            .isoformat()
                        )
                        claimed = await self.local_history.claim_photo_request(
                            message.chat.id,
                            message.message_id,
                            message.from_user.id,
                            day,
                            user_limit=self.settings.photo_daily_user_limit,
                            chat_limit=self.settings.photo_daily_chat_limit,
                        )
                        if claimed != "accepted":
                            if claimed == "user_limit":
                                notice = PHOTO_USER_LIMIT
                            elif claimed == "chat_limit":
                                notice = PHOTO_CHAT_LIMIT
                            elif claimed == "duplicate":
                                return
                            else:
                                notice = PHOTO_DISABLED
                            await self._send(bot, message, notice)
                            return
                        answer = await self.service.generate_photo_question(
                            prompt,
                            image,
                            chat_id=message.chat.id,
                            message_id=message.message_id,
                        )
                    elif command is not None:
                        context = await self._recent_context(message, bot, command.limit, prompt)
                        if context is None:
                            return
                        answer = await self.service.generate(
                            prompt,
                            recent_context=context,
                            current_author=message.from_user.full_name,
                            request_type="context_command",
                            chat_id=message.chat.id,
                            message_id=message.message_id,
                        )
                    else:
                        context = await self._reply_context(message)
                        if context is not None and context.messages:
                            answer = await self.service.generate(
                                prompt,
                                context=context,
                                current_author=message.from_user.full_name,
                                request_type="reply_chain",
                                chat_id=message.chat.id,
                                message_id=message.message_id,
                            )
                        else:
                            answer = await self.service.generate(
                                prompt,
                                chat_id=message.chat.id,
                                message_id=message.message_id,
                            )
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

    async def _handle_image(
        self,
        message: Message,
        bot: Bot,
        image_args: str | None,
        mix_args: str | None,
        edit_args: str | None,
    ) -> None:
        if not message.from_user or message.from_user.is_bot or message.sender_chat is not None:
            return
        if not self.settings.image_generation_enabled or self.local_history is None:
            await self._send(bot, message, IMAGE_DISABLED)
            return
        prompt = next(
            (value for value in (image_args, mix_args, edit_args) if value is not None), None
        )
        if not prompt:
            await self._send(bot, message, IMAGE_USAGE)
            return
        if len(prompt) > self.settings.max_input_chars:
            await self._send(
                bot,
                message,
                f"Описание слишком длинное: максимум {self.settings.max_input_chars} символов.",
            )
            return
        photos = None
        if mix_args is not None:
            if message.media_group_id:
                await asyncio.sleep(2)
                key = (message.chat.id, message.media_group_id)
                album = self._albums.pop(key, (0, {}))[1]
                members = sorted(album.values(), key=lambda item: item.message_id)
                if not 2 <= len(members) <= 4 or any(
                    not item.photo
                    or not item.from_user
                    or item.from_user.id != message.from_user.id
                    or item.message_thread_id != message.message_thread_id
                    for item in members
                ):
                    await self._send(bot, message, MIX_ALBUM_SIZE)
                    return
                photos = tuple(
                    max(item.photo, key=lambda photo: photo.width * photo.height)
                    for item in members
                )
            else:
                parent = message.reply_to_message
                if (
                    not message.photo
                    or parent is None
                    or not parent.photo
                    or message.external_reply is not None
                    or parent.chat.id != message.chat.id
                    or (
                        message.is_topic_message
                        and (
                            message.message_thread_id is None
                            or parent.message_thread_id not in (None, message.message_thread_id)
                        )
                    )
                ):
                    await self._send(bot, message, MIX_WRONG_SCOPE)
                    return
                photos = (
                    max(parent.photo, key=lambda item: item.width * item.height),
                    max(message.photo, key=lambda item: item.width * item.height),
                )
        elif edit_args is not None:
            photo = self._photo_for_question(message)
            if photo is None:
                await self._send(bot, message, EDIT_WRONG_SCOPE)
                return
            photos = (photo,)
        if not self.limiter.allow(message.from_user.id):
            await self._send(bot, message, LOCAL_RATE_LIMIT)
            return
        async with typing(bot, message):
            images = None
            if photos is not None:
                try:
                    images = [await self._download_photo(bot, photo) for photo in photos]
                except ValueError:
                    await self._send(bot, message, PHOTO_TOO_LARGE)
                    return
                except PHOTO_DOWNLOAD_ERRORS as exc:
                    logger.warning("Mix photo download failed kind=%s", type(exc).__name__)
                    await self._send(bot, message, PHOTO_UNAVAILABLE)
                    return
            day = (
                datetime.now(UTC)
                .astimezone(ZoneInfo(self.settings.image_daily_timezone))
                .date()
                .isoformat()
            )
            claimed = await self.local_history.claim_image_request(
                message.chat.id,
                message.message_id,
                message.from_user.id,
                day,
                user_limit=self.settings.image_daily_user_limit,
                chat_limit=self.settings.image_daily_chat_limit,
            )
            if claimed != "accepted":
                if claimed == "duplicate":
                    return
                notice = (
                    IMAGE_USER_LIMIT
                    if claimed == "user_limit"
                    else IMAGE_CHAT_LIMIT
                    if claimed == "chat_limit"
                    else IMAGE_DISABLED
                )
                await self._send(bot, message, notice)
                return
            try:
                generated = await self.service.generate_image(
                    prompt, images=images, chat_id=message.chat.id, message_id=message.message_id
                )
            except ModelRateLimited:
                await self._send(bot, message, MODEL_RATE_LIMIT)
                return
            except ModelUnavailable:
                await self._send(bot, message, TEMPORARY_ERROR)
                return
            if len(generated) > MAX_PHOTO_BYTES:
                logger.warning(
                    "Generated image exceeds Telegram photo limit bytes=%d", len(generated)
                )
                await self._send(bot, message, IMAGE_TOO_LARGE)
                return
            try:
                sent = await bot.send_photo(
                    chat_id=message.chat.id,
                    photo=BufferedInputFile(generated, filename="image.jpg"),
                    message_thread_id=message.message_thread_id,
                    reply_parameters=ReplyParameters(
                        message_id=message.message_id, allow_sending_without_reply=True
                    ),
                )
            except TELEGRAM_ERRORS as exc:
                logger.warning("Telegram image send failure kind=%s", type(exc).__name__)
                return
            if isinstance(sent, Message):
                await self._store(sent)

    def _record_album_photo(self, message: Message) -> None:
        if not message.media_group_id or not message.photo or not message.from_user:
            return
        now = monotonic()
        self._albums = {key: value for key, value in self._albums.items() if now - value[0] < 60}
        key = (message.chat.id, message.media_group_id)
        _, members = self._albums.get(key, (now, {}))
        members[message.message_id] = message
        self._albums[key] = (now, members)

    @staticmethod
    def _photo_for_question(message: Message) -> PhotoSize | None:
        if message.photo:
            return max(message.photo, key=lambda photo: photo.width * photo.height)
        parent = message.reply_to_message
        if parent is None or not parent.photo:
            return None
        if message.external_reply is not None or parent.chat.id != message.chat.id:
            return None
        if message.is_topic_message and (
            message.message_thread_id is None
            or parent.message_thread_id not in (None, message.message_thread_id)
        ):
            return None
        return max(parent.photo, key=lambda photo: photo.width * photo.height)

    @staticmethod
    async def _download_photo(bot: Bot, photo: PhotoSize) -> bytes:
        file = await bot.get_file(photo.file_id)
        size = file.file_size or photo.file_size
        if size is None:
            raise RuntimeError("Photo file size is unavailable")
        if size > MAX_PHOTO_BYTES:
            raise ValueError("Photo file size exceeds limit")
        if not file.file_path:
            raise RuntimeError("Photo file path is unavailable")
        buffer = BytesIO()
        await bot.download_file(file.file_path, destination=buffer, timeout=30)
        image = buffer.getvalue()
        if len(image) > MAX_PHOTO_BYTES:
            raise ValueError("Downloaded photo exceeds limit")
        if not image.startswith(b"\xff\xd8\xff"):
            raise RuntimeError("Downloaded photo is not JPEG")
        return image

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
