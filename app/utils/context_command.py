import re
from dataclasses import dataclass

from aiogram.types import Message

from app.config import Settings

DEFAULT_CONTEXT_PROMPT = (
    "Кратко подведи итог последних сообщений этой ветки разговора. "
    "Выдели основные темы, решения и открытые вопросы."
)


class ContextCommandError(ValueError):
    pass


@dataclass(frozen=True)
class ContextCommand:
    limit: int
    prompt: str


def context_command_args(message: Message, bot_username: str) -> str | None:
    return command_args(message, bot_username, "/context")


def command_args(
    message: Message, bot_username: str, command_name: str, *, caption: bool = False
) -> str | None:
    content = message.caption if caption else message.text
    entities = message.caption_entities if caption else message.entities
    if not content:
        return None
    for entity in entities or ():
        if entity.type != "bot_command" or entity.offset != 0:
            continue
        command = content.encode("utf-16-le")[: entity.length * 2].decode("utf-16-le")
        name, _, username = command.partition("@")
        if name.lower() != command_name:
            return None
        if username and username.casefold() != bot_username.casefold():
            return None
        return content[len(command) :].strip()
    return None


def parse_context_command(args: str, settings: Settings) -> ContextCommand:
    limit = settings.context_command_default_messages
    prompt = DEFAULT_CONTEXT_PROMPT
    if args:
        parts = args.split(maxsplit=1)
        first, remainder = parts[0], parts[1] if len(parts) > 1 else ""
        # A single nonnumeric token is an invalid N; multiword text is a question
        # without N. This resolves the required `/context abc` ambiguity explicitly.
        if re.fullmatch(r"[+-]?[0-9]+", first):
            if len(first) > 10:
                raise ContextCommandError(
                    f"Максимально можно использовать {settings.context_command_max_messages} "
                    "последних сообщений."
                )
            limit = int(first)
            prompt = remainder.strip() or DEFAULT_CONTEXT_PROMPT
        elif re.match(r"[+-]?[0-9]", first) or not remainder.strip():
            raise ContextCommandError(
                "N должно быть положительным целым числом. Пример: /context 50"
            )
        else:
            prompt = args
    if limit <= 0:
        raise ContextCommandError("N должно быть положительным целым числом. Пример: /context 50")
    if limit > settings.context_command_max_messages:
        raise ContextCommandError(
            f"Максимально можно использовать {settings.context_command_max_messages} "
            "последних сообщений."
        )
    return ContextCommand(limit, prompt)
