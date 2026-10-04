import re
from collections.abc import Sequence

from aiogram.enums import MessageEntityType
from aiogram.types import MessageEntity


def extract_prompt(
    text: str,
    entities: Sequence[MessageEntity] | None,
    bot_username: str,
) -> str | None:
    """None: no verified mention; empty string: mention without a question.

    Telegram entity offsets/lengths use UTF-16 units, not Python character indices.
    Only exact mention entities can open the privacy gate. Malformed mentions
    fail closed. No substring matching, text_mention, reply context or commands.
    """
    encoded = text.encode("utf-16-le")
    expected = f"@{bot_username}".casefold()
    spans: list[tuple[int, int]] = []
    for entity in entities or ():
        if entity.type != MessageEntityType.MENTION:
            continue
        start, end = entity.offset * 2, (entity.offset + entity.length) * 2
        if start < 0 or end > len(encoded) or end <= start:
            return None
        try:
            value = encoded[start:end].decode("utf-16-le")
        except UnicodeDecodeError:
            return None
        if value.casefold() == expected:
            spans.append((start, end))
    if not spans:
        return None

    chunks: list[bytes] = []
    previous_end = 0
    for start, end in sorted(set(spans)):
        if start < previous_end:
            return None
        chunks.append(encoded[previous_end:start])
        previous_end = end
    chunks.append(encoded[previous_end:])
    prompt = b"".join(chunks).decode("utf-16-le")
    prompt = re.sub(r"[ \t]+([,.;:!?])", r"\1", prompt)
    return prompt.strip()
