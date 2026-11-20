import re

from aiogram.types import MessageEntity
from aiogram.utils.formatting import Bold, Code, Italic, Pre, Text

_MARKDOWN_BLOCK = re.compile(
    r"^ {0,3}(?P<fence>`{3,})(?!`)[ \t]*(?P<language>[A-Za-z0-9_+.-]{0,32})[ \t]*\n"
    r"(?P<pre>[\s\S]*?)(?:^ {0,3}(?P=fence)`*[ \t]*(?=\n|\Z)|\Z)"
    r"|^ {0,3}\#{1,6}[ \t]+(?P<heading>\S[^\n]*)",
    re.MULTILINE,
)
_MARKDOWN_INLINE = re.compile(
    r"(?P<url>https?://[^\s<>]+)"
    r"|\\(?P<literal>[\\`*_])"
    r"|(?<!`)(?P<ticks>`+)(?P<code>[^\n]+?)(?<!`)(?P=ticks)(?!`)"
    r"|(?<!\*)\*\*(?P<bold>(?:[^*`\n]|`[^`\n]+`)+)(?<!\\)\*\*(?!\*)"
    r"|(?<![\w*])\*(?P<italic>(?=\S)[^*`\n]+)(?<=\S)(?<!\\)\*(?![\w*])"
    r"|(?<!\w)_(?P<italic_alt>(?=\S)[^_`\n]+)(?<=\S)(?<!\\)_(?!\w)"
)


def _inline_markup(text: str, *, bold_text: bool = False) -> list[str | Text]:
    nodes: list[str | Text] = []

    def append_plain(value: str) -> None:
        if value:
            nodes.append(Bold(value) if bold_text and value.strip() else value)

    end = 0
    for match in _MARKDOWN_INLINE.finditer(text):
        append_plain(text[end : match.start()])
        end = match.end()
        if (url := match["url"]) is not None:
            append_plain(url)
        elif (literal := match["literal"]) is not None:
            append_plain(literal)
        elif (code := match["code"]) is not None:
            nodes.append(Code(code) if code.strip() else match[0])
        elif (bold := match["bold"]) is not None and bold.strip():
            # Telegram code entities cannot overlap with bold, even inside a heading.
            nodes.extend(_inline_markup(bold.strip(), bold_text=True))
        elif italic := match["italic"] or match["italic_alt"]:
            nodes.append(Bold(italic) if bold_text else Italic(italic))
        else:
            append_plain(match[0])
    append_plain(text[end:])
    return nodes


def format_telegram_text(
    text: str, *, literal_suffix: str = "", limit: int = 4096
) -> list[tuple[str, list[MessageEntity]]]:
    """Render common model markup as native Telegram entities with per-part offsets."""
    nodes: list[str | Text] = []
    end = 0
    for match in _MARKDOWN_BLOCK.finditer(text):
        nodes.extend(_inline_markup(text[end : match.start()]))
        end = match.end()
        if (heading := match["heading"]) is not None:
            nodes.extend(_inline_markup(heading, bold_text=True))
        elif match["pre"].strip():
            nodes.append(Pre(match["pre"], language=match["language"] or None))
        else:
            nodes.append(match[0])
    nodes.extend(_inline_markup(text[end:]))
    plain, entities = Text(*nodes, literal_suffix).render()

    chunks = []
    encoded = plain.encode("utf-16-le")
    start = 0
    for part in split_telegram_text(plain, limit):
        end = start + len(part.encode("utf-16-le")) // 2
        part_entities = []
        for entity in entities:
            left = max(start, entity.offset)
            right = min(end, entity.offset + entity.length)
            if left < right:
                span = encoded[2 * left : 2 * right].decode("utf-16-le").rstrip()
                if entity.type not in {"code", "pre"}:
                    trimmed = span.lstrip()
                    left += (len(span.encode("utf-16-le")) - len(trimmed.encode("utf-16-le"))) // 2
                    span = trimmed
                if span:
                    part_entities.append(
                        entity.model_copy(
                            update={
                                "offset": left - start,
                                "length": len(span.encode("utf-16-le")) // 2,
                            }
                        )
                    )
        chunks.append((part, part_entities))
        start = end
    return chunks


def split_telegram_text(text: str, limit: int = 4096) -> list[str]:
    """Preserve text, prioritizing paragraphs, lines, sentences, then hard cuts.

    Count UTF-16 conservatively, so emoji cannot exceed Telegram's size limit.
    Do not break an astral Unicode character into surrogate halves.
    """
    if limit < 2:
        raise ValueError("limit must be at least 2")
    parts: list[str] = []
    start = 0
    while start < len(text):
        end, units = start, 0
        while end < len(text):
            width = 2 if ord(text[end]) > 0xFFFF else 1
            if units + width > limit:
                break
            units += width
            end += 1
        if end < len(text):
            window = text[start:end]
            boundaries = [
                window.rfind("\n\n") + 2 if "\n\n" in window else 0,
                window.rfind("\n") + 1 if "\n" in window else 0,
                max((m.end() for m in re.finditer(r"[.!?…](?:[ \t]+|$)", window)), default=0),
            ]
            # Avoid producing very short parts for a boundary near the start.
            boundary = next((b for b in boundaries if b >= len(window) // 2 and b > 0), 0)
            if boundary:
                end = start + boundary
        parts.append(text[start:end])
        start = end
    return parts
