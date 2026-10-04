import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

LIMIT_MARKER = "[Earlier messages omitted due to context limit]"
PARTIAL_MARKER = "[Earlier reply-chain messages are unavailable]"
RECENT_LIMIT_MARKER = "[Earlier messages omitted due to context size limit]"
TEXT_MARKER = " [Message text truncated due to context limit]"
MARKER_RESERVE = max(len(LIMIT_MARKER), len(PARTIAL_MARKER))


@dataclass(frozen=True)
class ContextMessage:
    message_id: int
    sender_id: int | None
    sender_name: str
    sender_username: str | None
    timestamp: datetime
    text: str = field(repr=False)
    is_bot: bool = False
    is_current_bot: bool = False
    reply_to_message_id: int | None = None


@dataclass(frozen=True)
class ReplyContext:
    # Always chronological, independent of timestamps. Reply edges define order.
    messages: tuple[ContextMessage, ...] = field(default=(), repr=False)
    truncation_reason: str | None = None


def render_context_message(message: ContextMessage) -> str:
    timestamp = message.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return json.dumps(
        {
            "author": message.sender_name,
            "timestamp": timestamp.astimezone(UTC).isoformat(),
            "text": message.text,
        },
        ensure_ascii=False,
    )


def fit_context_message(message: ContextMessage, budget: int) -> ContextMessage | None:
    """Preserve the closest ancestor, explicitly clipping its text if necessary."""
    if len(render_context_message(message)) <= budget:
        return message
    if len(render_context_message(replace(message, text=TEXT_MARKER))) > budget:
        return None
    low, high = 0, len(message.text)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = replace(message, text=message.text[:middle] + TEXT_MARKER)
        if len(render_context_message(candidate)) <= budget:
            low = middle
        else:
            high = middle - 1
    return replace(message, text=message.text[:low] + TEXT_MARKER)


def limit_reply_context(
    context: ReplyContext, max_chars: int, max_depth: int, *, limit_marker: str = LIMIT_MARKER
) -> ReplyContext:
    """Keep nearest ancestors within the budget remaining after the current question."""
    remaining = max(0, max_chars - max(len(limit_marker), len(PARTIAL_MARKER)))
    selected = []
    reason = context.truncation_reason
    for message in reversed(context.messages):
        if len(selected) == max_depth:
            reason = "depth_limit"
            break
        fitted = fit_context_message(message, remaining)
        if fitted is None:
            reason = "char_limit"
            break
        selected.append(fitted)
        remaining -= len(render_context_message(fitted))
        if fitted != message:
            reason = "char_limit"
            break
    return ReplyContext(tuple(reversed(selected)), reason)


def render_current_question(prompt: str, current_author: str | None) -> str:
    return json.dumps(
        {"author": current_author or "Current user", "text": prompt},
        ensure_ascii=False,
    )


def build_reply_inputs(
    prompt: str,
    context: ReplyContext,
    current_author: str | None,
    *,
    limit_marker: str = LIMIT_MARKER,
) -> list[dict]:
    inputs = []
    if context.truncation_reason:
        marker = (
            limit_marker
            if context.truncation_reason in {"char_limit", "depth_limit"}
            else PARTIAL_MARKER
        )
        inputs.append({"role": "user", "content": marker})
    inputs.extend(
        {
            "role": "assistant" if message.is_current_bot else "user",
            "content": render_context_message(message),
        }
        for message in context.messages
    )
    inputs.append(
        {
            "role": "user",
            "content": render_current_question(prompt, current_author),
        }
    )
    return inputs
