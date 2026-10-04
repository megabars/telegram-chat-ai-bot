import pytest
from aiogram.types import MessageEntity

from app.utils.mentions import extract_prompt


@pytest.mark.parametrize(
    "text", ["Всем привет", "Кто сегодня идёт на встречу?", "текст @my_bot текст"]
)
def test_no_entities_means_no_mention(text):
    assert extract_prompt(text, None, "my_bot") is None


@pytest.mark.parametrize("prefix", ["", "Привет ", "🙂 ", "👨‍👩‍👧‍👦 "])
def test_utf16_and_case_insensitive(prefix):
    text = prefix + "@MY_BOT объясни Docker"
    entity = MessageEntity(type="mention", offset=len(prefix.encode("utf-16-le")) // 2, length=7)
    assert extract_prompt(text, [entity], "my_bot") == (prefix + " объясни Docker").strip()


def test_remove_only_own_mentions():
    text = "@my_bot спроси @other_bot и @my_bot"
    entities = [
        MessageEntity(type="mention", offset=offset, length=length)
        for offset, length in [(0, 7), (15, 10), (28, 7)]
    ]
    assert extract_prompt(text, entities, "my_bot") == "спроси @other_bot и"


def test_punctuation():
    assert (
        extract_prompt(
            "Привет @my_bot, почему?", [MessageEntity(type="mention", offset=7, length=7)], "my_bot"
        )
        == "Привет, почему?"
    )


@pytest.mark.parametrize(
    "entity",
    [
        MessageEntity(type="mention", offset=0, length=12),
        MessageEntity(type="mention", offset=100, length=7),
        MessageEntity(type="mention", offset=-1, length=7),
        MessageEntity(type="code", offset=0, length=7),
        MessageEntity(type="bot_command", offset=0, length=7),
    ],
)
def test_unverified_entities_fail_closed(entity):
    assert extract_prompt("@my_bot_test привет", [entity], "my_bot") is None


def test_surrogate_boundary_fails_closed():
    assert (
        extract_prompt("🙂@my_bot", [MessageEntity(type="mention", offset=1, length=7)], "my_bot")
        is None
    )


def test_mention_only():
    assert (
        extract_prompt("@my_bot", [MessageEntity(type="mention", offset=0, length=7)], "my_bot")
        == ""
    )
