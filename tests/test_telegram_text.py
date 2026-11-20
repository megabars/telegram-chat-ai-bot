import random

import pytest

from app.utils.telegram_text import format_telegram_text, split_telegram_text


@pytest.mark.parametrize(
    "text", ["", "hello", "x" * 10000, "🙂" * 6000, "a\n\n" * 5000, "A sentence. " * 1000]
)
def test_preserves_text_and_limit(text):
    parts = split_telegram_text(text)
    assert "".join(parts) == text
    assert all(0 < len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts)


def test_exact_telegram_limit_stays_in_one_message():
    text = "x" * 4096
    parts = split_telegram_text(text)
    assert parts == [text]
    assert len(parts[0].encode("utf-16-le")) // 2 == 4096


@pytest.mark.parametrize("separator", ["\n\n", "\n", ". "])
def test_preferred_break(separator):
    text = "a" * 2500 + separator + "b" * 3000
    assert split_telegram_text(text)[0] == "a" * 2500 + separator


def test_random_unicode_boundaries():
    rng = random.Random(123)
    for _ in range(100):
        text = "".join(rng.choice("abc🙂\n.! 👨‍👩‍👧‍👦") for _ in range(rng.randrange(500)))
        limit = rng.randrange(2, 80)
        parts = split_telegram_text(text, limit)
        assert "".join(parts) == text
        assert all(len(p.encode("utf-16-le")) // 2 <= limit for p in parts)


def test_model_markup_renders_as_native_entities():
    [(text, entities)] = format_telegram_text(
        "### **Итоги**\n🙂 ** ооо ** и _курсив_\n**Команда `echo **x**`**"
    )
    assert text == "Итоги\n🙂 ооо и курсив\nКоманда echo **x**"
    raw = text.encode("utf-16-le")
    assert [
        (e.type, raw[2 * e.offset : 2 * (e.offset + e.length)].decode("utf-16-le"))
        for e in entities
    ] == [
        ("bold", "Итоги"),
        ("bold", "ооо"),
        ("italic", "курсив"),
        ("bold", "Команда"),
        ("code", "echo **x**"),
    ]


def test_markup_preserves_fenced_code_literal_urls_and_unbalanced_markers():
    markup = "```python\nprint('**literal** <b> &')\n```\n**без закрытия и 2 * 3 * 4"
    url = "\n\nИсточник: https://example.com/**a**"
    [(text, entities)] = format_telegram_text(markup, literal_suffix=url)
    assert text == "print('**literal** <b> &')\n\n**без закрытия и 2 * 3 * 4" + url
    [code] = entities
    assert code.type == "pre" and code.language == "python" and code.offset == 0
    assert code.length == len("print('**literal** <b> &')")


def test_long_bold_text_keeps_its_entity_across_telegram_parts():
    body = "🙂" * 2100
    parts = format_telegram_text(f"🙂**{body}**.")
    assert len(parts) == 2
    assert "".join(part for part, _ in parts) == f"🙂{body}."
    for index, (part, [bold]) in enumerate(parts):
        units = len(part.encode("utf-16-le")) // 2
        assert units <= 4096
        assert bold.offset == (2 if index == 0 else 0)
        assert bold.length == units - (2 if index == 0 else 1)
