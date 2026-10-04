import logging
from dataclasses import replace

import pytest

from app.config import ConfigurationError, Settings
from app.logging_config import SafeLogFilter


@pytest.mark.parametrize(
    "name,value",
    [
        ("max_input_chars", 0),
        ("max_concurrent_requests", -1),
        ("openai_timeout_seconds", float("nan")),
        ("rate_limit_period_seconds", float("inf")),
        ("telegram_bot_token", ""),
        ("openai_api_key", ""),
        ("log_level", "UNKNOWN"),
        ("reply_context_max_depth", 0),
        ("reply_context_max_chars", -1),
        ("reply_context_fetch_timeout_seconds", float("inf")),
        ("reply_context_fetch_timeout_seconds", 0),
        ("telegram_api_id", -1),
        ("telegram_api_hash", "hash-without-id"),
        ("telegram_api_id", 123),
        ("telegram_mtproto_session_path", ""),
        ("local_history_db_path", ""),
        ("local_history_max_messages", 0),
        ("local_history_cleanup_threshold", 5000),
        ("context_command_default_messages", 201),
        ("context_command_max_messages", 49),
        ("context_max_chars", 0),
    ],
)
def test_invalid_config(settings, name, value):
    with pytest.raises(ConfigurationError):
        replace(settings, **{name: value})


def test_env_loading_precedence(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "TELEGRAM_BOT_TOKEN=123456:test\nOPENAI_API_KEY=fake\n"
        "ALLOWED_CHAT_IDS=-1001, -1002\nOPENAI_MODEL=gpt-6-luna\n"
    )
    for name in ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "ALLOWED_CHAT_IDS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "explicit-model")
    settings = Settings.from_env(path)
    assert settings.openai_model == "explicit-model"
    assert settings.allowed_chat_ids == frozenset({-1001, -1002})
    assert "fake" not in repr(settings) and "123456:test" not in repr(settings)
    # load_dotenv writes os.environ; register cleanup for those added keys.
    for name in ("TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "ALLOWED_CHAT_IDS"):
        monkeypatch.setenv(name, "")


def test_secrets_and_exception_body_never_in_logs():
    filter_ = SafeLogFilter(("secret-key", "bot-token"))
    record = logging.LogRecord(
        "app.test",
        logging.ERROR,
        "",
        1,
        "key=secret-key token=%s Authorization: Bearer other",
        ("bot-token",),
        None,
    )
    filter_.filter(record)
    assert "secret-key" not in record.getMessage()
    assert "bot-token" not in record.getMessage()
    assert "Bearer other" not in record.getMessage()
    library = logging.LogRecord(
        "aiogram.event",
        logging.ERROR,
        "",
        1,
        "user text SECRET %s",
        ("Authorization",),
        (ValueError, ValueError("SECRET"), None),
    )
    filter_.filter(library)
    assert "SECRET" not in library.getMessage()
    assert "kind=ValueError" in library.getMessage()
    assert library.exc_info is None


def test_reply_config_env_and_hash_repr(tmp_path, monkeypatch):
    env = {
        "TELEGRAM_BOT_TOKEN": "123456:test",
        "OPENAI_API_KEY": "fake",
        "TELEGRAM_API_ID": "123",
        "TELEGRAM_API_HASH": "SECRET-HASH",
        "REPLY_CONTEXT_ENABLED": "false",
        "REPLY_CONTEXT_MAX_DEPTH": "9",
        "REPLY_CONTEXT_MAX_CHARS": "2000",
        "REPLY_CONTEXT_FETCH_TIMEOUT_SECONDS": "1.5",
        "TELEGRAM_MTPROTO_SESSION_PATH": "/var/lib/telegram-openai-bot/telegram.session",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    settings = Settings.from_env(tmp_path / "absent.env")
    assert settings.telegram_api_id == 123
    assert settings.telegram_api_hash == "SECRET-HASH"
    assert settings.reply_context_enabled is False
    assert settings.reply_context_max_depth == 9
    assert settings.reply_context_max_chars == 2000
    assert settings.reply_context_fetch_timeout_seconds == 1.5
    assert "SECRET-HASH" not in repr(settings)


@pytest.mark.parametrize(
    "key,value",
    [
        ("REPLY_CONTEXT_ENABLED", "yes"),
        ("TELEGRAM_API_ID", "secret-invalid-id"),
        ("REPLY_CONTEXT_MAX_DEPTH", "secret-invalid-depth"),
    ],
)
def test_reply_env_errors_hide_values(tmp_path, monkeypatch, key, value):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test")
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setenv(key, value)
    with pytest.raises(ConfigurationError) as exc:
        Settings.from_env(tmp_path / "absent.env")
    assert value not in str(exc.value)


def test_telethon_logs_do_not_leak_message_text():
    filter_ = SafeLogFilter(("SECRET-HASH",))
    record = logging.LogRecord(
        "telethon.network.mtprotosender",
        logging.ERROR,
        "",
        1,
        "message=PRIVATE-TEXT api_hash=SECRET-HASH",
        (),
        (ValueError, ValueError("PRIVATE-TEXT"), None),
    )
    filter_.filter(record)
    assert "SECRET-HASH" not in record.getMessage()
    assert "PRIVATE-TEXT" not in record.getMessage()
    assert record.exc_info is None


def test_local_history_env_parameters(tmp_path, monkeypatch):
    values = {
        "TELEGRAM_BOT_TOKEN": "123456:test",
        "OPENAI_API_KEY": "fake",
        "LOCAL_HISTORY_ENABLED": "false",
        "LOCAL_HISTORY_DB_PATH": "./data/history.sqlite3",
        "LOCAL_HISTORY_MAX_MESSAGES": "100",
        "LOCAL_HISTORY_CLEANUP_THRESHOLD": "110",
        "CONTEXT_COMMAND_DEFAULT_MESSAGES": "20",
        "CONTEXT_COMMAND_MAX_MESSAGES": "80",
        "CONTEXT_MAX_CHARS": "20000",
        "CONTEXT_RESPECT_TOPICS": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    settings = Settings.from_env(tmp_path / "absent")
    assert not settings.local_history_enabled and not settings.context_respect_topics
    assert settings.local_history_max_messages == 100
    assert settings.local_history_cleanup_threshold == 110
    assert settings.context_command_default_messages == 20
    assert settings.context_command_max_messages == 80
    assert settings.context_max_chars == 20000
    assert settings.local_history_db_path == "./data/history.sqlite3"
