import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """A safe error containing field names, never their secret values."""


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str = field(repr=False)
    openai_api_key: str = field(repr=False)
    openai_model: str = "gpt-6-luna"
    allowed_chat_ids: frozenset[int] = frozenset()
    max_input_chars: int = 12000
    max_concurrent_requests: int = 5
    rate_limit_requests: int = 10
    rate_limit_period_seconds: float = 60
    openai_timeout_seconds: float = 60
    max_output_tokens: int = 2048
    log_level: str = "INFO"
    telegram_api_id: int | None = None
    telegram_api_hash: str = field(default="", repr=False)
    reply_context_enabled: bool = True
    reply_context_max_depth: int = 30
    reply_context_max_chars: int = 30000
    reply_context_fetch_timeout_seconds: float = 10
    telegram_mtproto_session_path: str = "/var/lib/telegram-openai-bot/telegram.session"
    local_history_enabled: bool = True
    local_history_db_path: str = "/var/lib/telegram-openai-bot/history.sqlite3"
    local_history_max_messages: int = 5000
    local_history_cleanup_threshold: int = 5100
    context_command_default_messages: int = 50
    context_command_max_messages: int = 200
    context_max_chars: int = 50000
    context_respect_topics: bool = True

    def __post_init__(self) -> None:
        for name in ("telegram_bot_token", "openai_api_key", "openai_model"):
            if not getattr(self, name).strip():
                raise ConfigurationError(f"{name.upper()} must be set")
        for name in (
            "max_input_chars",
            "max_concurrent_requests",
            "rate_limit_requests",
            "rate_limit_period_seconds",
            "openai_timeout_seconds",
            "max_output_tokens",
            "reply_context_max_depth",
            "reply_context_max_chars",
            "reply_context_fetch_timeout_seconds",
            "local_history_max_messages",
            "local_history_cleanup_threshold",
            "context_command_default_messages",
            "context_command_max_messages",
            "context_max_chars",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError(f"{name.upper()} must be positive and finite")
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("LOG_LEVEL is invalid")
        if self.telegram_api_id is not None and self.telegram_api_id <= 0:
            raise ConfigurationError("TELEGRAM_API_ID must be positive")
        if bool(self.telegram_api_id) != bool(self.telegram_api_hash.strip()):
            raise ConfigurationError("Set both TELEGRAM_API_ID and TELEGRAM_API_HASH, or neither")
        if not self.telegram_mtproto_session_path.strip():
            raise ConfigurationError("TELEGRAM_MTPROTO_SESSION_PATH must not be empty")
        if not self.local_history_db_path.strip():
            raise ConfigurationError("LOCAL_HISTORY_DB_PATH must not be empty")
        if self.local_history_db_path == self.telegram_mtproto_session_path:
            raise ConfigurationError("SQLite history and MTProto session must use different paths")
        if self.local_history_cleanup_threshold <= self.local_history_max_messages:
            raise ConfigurationError("LOCAL_HISTORY_CLEANUP_THRESHOLD must exceed MAX_MESSAGES")
        if self.context_command_default_messages > self.context_command_max_messages:
            raise ConfigurationError(
                "CONTEXT_COMMAND_DEFAULT_MESSAGES must not exceed MAX_MESSAGES"
            )

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        # Environment takes precedence; interpolation disabled to match systemd.
        load_dotenv(env_file, override=False, interpolate=False)

        def number(name: str, default: int | float, kind: type = int):
            try:
                return kind(os.getenv(name, str(default)))
            except ValueError:
                raise ConfigurationError(f"{name} must be numeric") from None

        def boolean(name: str, default: bool) -> bool:
            value = os.getenv(name, str(default)).strip().lower()
            if value not in {"true", "false"}:
                raise ConfigurationError(f"{name} must be true or false")
            return value == "true"

        try:
            chat_ids = frozenset(
                int(value.strip())
                for value in os.getenv("ALLOWED_CHAT_IDS", "").split(",")
                if value.strip()
            )
        except ValueError:
            raise ConfigurationError(
                "ALLOWED_CHAT_IDS must contain comma-separated integers"
            ) from None

        return cls(
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-6-luna").strip(),
            allowed_chat_ids=chat_ids,
            max_input_chars=number("MAX_INPUT_CHARS", 12000),
            max_concurrent_requests=number("MAX_CONCURRENT_REQUESTS", 5),
            rate_limit_requests=number("RATE_LIMIT_REQUESTS", 10),
            rate_limit_period_seconds=number("RATE_LIMIT_PERIOD_SECONDS", 60, float),
            openai_timeout_seconds=number("OPENAI_TIMEOUT_SECONDS", 60, float),
            max_output_tokens=number("MAX_OUTPUT_TOKENS", 2048),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            telegram_api_id=number("TELEGRAM_API_ID", 0)
            if os.getenv("TELEGRAM_API_ID", "").strip()
            else None,
            telegram_api_hash=os.getenv("TELEGRAM_API_HASH", "").strip(),
            reply_context_enabled=boolean("REPLY_CONTEXT_ENABLED", True),
            reply_context_max_depth=number("REPLY_CONTEXT_MAX_DEPTH", 30),
            reply_context_max_chars=number("REPLY_CONTEXT_MAX_CHARS", 30000),
            reply_context_fetch_timeout_seconds=number(
                "REPLY_CONTEXT_FETCH_TIMEOUT_SECONDS",
                10,
                float,
            ),
            telegram_mtproto_session_path=os.getenv(
                "TELEGRAM_MTPROTO_SESSION_PATH",
                "/var/lib/telegram-openai-bot/telegram.session",
            ).strip(),
            local_history_enabled=boolean("LOCAL_HISTORY_ENABLED", True),
            local_history_db_path=os.getenv(
                "LOCAL_HISTORY_DB_PATH", "/var/lib/telegram-openai-bot/history.sqlite3"
            ).strip(),
            local_history_max_messages=number("LOCAL_HISTORY_MAX_MESSAGES", 5000),
            local_history_cleanup_threshold=number("LOCAL_HISTORY_CLEANUP_THRESHOLD", 5100),
            context_command_default_messages=number("CONTEXT_COMMAND_DEFAULT_MESSAGES", 50),
            context_command_max_messages=number("CONTEXT_COMMAND_MAX_MESSAGES", 200),
            context_max_chars=number("CONTEXT_MAX_CHARS", 50000),
            context_respect_topics=boolean("CONTEXT_RESPECT_TOPICS", True),
        )
