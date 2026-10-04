from aiogram.types import Message

from app.utils.reply_context import ContextMessage


class UnknownTopic(ValueError):
    pass


def forum_topic_id(message: Message) -> int | None:
    # Ordinary supergroup reply thread IDs must not be treated as forum topics.
    if message.is_topic_message:
        if message.message_thread_id is None:
            raise UnknownTopic("Forum topic is unknown")
        return message.message_thread_id
    return 1 if message.chat.is_forum else None


def local_message_content(message: Message, bot_id: int) -> ContextMessage | None:
    text = message.text or message.caption
    if not text:
        for field, placeholder in (
            ("photo", "[photo]"),
            ("animation", "[animation]"),
            ("video", "[video]"),
            ("video_note", "[video]"),
            ("voice", "[voice message]"),
            ("audio", "[audio]"),
            ("sticker", "[sticker]"),
            ("document", "[document]"),
            ("poll", "[media]"),
            ("location", "[media]"),
            ("venue", "[media]"),
            ("contact", "[media]"),
            ("dice", "[media]"),
            ("paid_media", "[media]"),
        ):
            if getattr(message, field, None):
                text = placeholder
                break
    if not text:
        return None  # Service events never become conversational content.
    sender = message.sender_chat or message.from_user
    sender_id = sender.id if sender is not None else None
    username = getattr(sender, "username", None)
    name = (
        getattr(sender, "full_name", None)
        or getattr(sender, "title", None)
        or (f"@{username}" if username else "Unknown author")
    )
    return ContextMessage(
        message_id=message.message_id,
        sender_id=sender_id,
        sender_name=name,
        sender_username=username,
        timestamp=message.date,
        text=text,
        is_bot=bool(getattr(sender, "is_bot", False)) or sender_id == bot_id,
        is_current_bot=sender_id == bot_id,
        reply_to_message_id=message.reply_to_message.message_id
        if message.reply_to_message
        else None,
    )
