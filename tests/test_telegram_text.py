import random

import pytest

from app.utils.telegram_text import split_telegram_text


@pytest.mark.parametrize(
    "text", ["", "hello", "x" * 10000, "🙂" * 6000, "a\n\n" * 5000, "A sentence. " * 1000]
)
def test_preserves_text_and_limit(text):
    parts = split_telegram_text(text)
    assert "".join(parts) == text
    assert all(0 < len(p.encode("utf-16-le")) // 2 <= 4096 for p in parts)


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
