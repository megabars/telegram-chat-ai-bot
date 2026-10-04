import re


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
