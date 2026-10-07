import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    daily_digest_enabled: bool = False
    daily_digest_chat_ids: frozenset[int] = frozenset()
    daily_digest_hour: int = 9
    daily_digest_timezone: str = "Asia/Tomsk"
    daily_digest_context_chars: int = 120000
    daily_digest_max_output_tokens: int = 600
    photo_analysis_enabled: bool = False
    photo_daily_user_limit: int = 20
    photo_daily_chat_limit: int = 20
    photo_daily_timezone: str = "Asia/Tomsk"
    image_generation_enabled: bool = False
    image_generation_model: str = "gpt-image-2.5-flare"
    image_generation_timeout_seconds: float = 180
    image_daily_user_limit: int = 3
    image_daily_chat_limit: int = 10
    image_daily_timezone: str = "Asia/Tomsk"
    link_read_enabled: bool = False
    link_daily_user_limit: int = 3
    link_daily_chat_limit: int = 10
    link_daily_timezone: str = "Asia/Tomsk"

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
            "daily_digest_context_chars",
            "daily_digest_max_output_tokens",
            "photo_daily_user_limit",
            "photo_daily_chat_limit",
            "image_generation_timeout_seconds",
            "image_daily_user_limit",
            "image_daily_chat_limit",
            "link_daily_user_limit",
            "link_daily_chat_limit",
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
        if not 0 <= self.daily_digest_hour <= 23:
            raise ConfigurationError("DAILY_DIGEST_HOUR must be between 0 and 23")
        if self.daily_digest_enabled:
            if not self.local_history_enabled or not self.daily_digest_chat_ids:
                raise ConfigurationError(
                    "Daily digest requires LOCAL_HISTORY_ENABLED and DAILY_DIGEST_CHAT_IDS"
                )
            try:
                ZoneInfo(self.daily_digest_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigurationError("DAILY_DIGEST_TIMEZONE is invalid") from None
        if self.photo_analysis_enabled:
            if not self.local_history_enabled:
                raise ConfigurationError("Photo analysis requires LOCAL_HISTORY_ENABLED")
            try:
                ZoneInfo(self.photo_daily_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigurationError("PHOTO_DAILY_TIMEZONE is invalid") from None
        if self.image_generation_enabled:
            if not self.local_history_enabled:
                raise ConfigurationError("Image generation requires LOCAL_HISTORY_ENABLED")
            if not self.image_generation_model.strip():
                raise ConfigurationError("IMAGE_GENERATION_MODEL must be set")
            try:
                ZoneInfo(self.image_daily_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigurationError("IMAGE_DAILY_TIMEZONE is invalid") from None
        if self.link_read_enabled:
            if not self.local_history_enabled:
                raise ConfigurationError("Link reading requires LOCAL_HISTORY_ENABLED")
            try:
                ZoneInfo(self.link_daily_timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ConfigurationError("LINK_DAILY_TIMEZONE is invalid") from None

    def digest_chat_allowed(self, chat_id: int) -> bool:
        return (
            self.daily_digest_enabled
            and self.local_history_enabled
            and chat_id in self.daily_digest_chat_ids
            and (not self.allowed_chat_ids or chat_id in self.allowed_chat_ids)
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

        try:
            digest_chat_ids = frozenset(
                int(value.strip())
                for value in os.getenv("DAILY_DIGEST_CHAT_IDS", "").split(",")
                if value.strip()
            )
        except ValueError:
            raise ConfigurationError(
                "DAILY_DIGEST_CHAT_IDS must contain comma-separated integers"
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
            daily_digest_enabled=boolean("DAILY_DIGEST_ENABLED", False),
            daily_digest_chat_ids=digest_chat_ids,
            daily_digest_hour=number("DAILY_DIGEST_HOUR", 9),
            daily_digest_timezone=os.getenv("DAILY_DIGEST_TIMEZONE", "Asia/Tomsk").strip(),
            daily_digest_context_chars=number("DAILY_DIGEST_CONTEXT_CHARS", 120000),
            daily_digest_max_output_tokens=number("DAILY_DIGEST_MAX_OUTPUT_TOKENS", 600),
            photo_analysis_enabled=boolean("PHOTO_ANALYSIS_ENABLED", False),
            photo_daily_user_limit=number("PHOTO_DAILY_USER_LIMIT", 20),
            photo_daily_chat_limit=number("PHOTO_DAILY_CHAT_LIMIT", 20),
            photo_daily_timezone=os.getenv("PHOTO_DAILY_TIMEZONE", "Asia/Tomsk").strip(),
            image_generation_enabled=boolean("IMAGE_GENERATION_ENABLED", False),
            image_generation_model=os.getenv(
                "IMAGE_GENERATION_MODEL", "gpt-image-2.5-flare"
            ).strip(),
            image_generation_timeout_seconds=number("IMAGE_GENERATION_TIMEOUT_SECONDS", 180, float),
            image_daily_user_limit=number("IMAGE_DAILY_USER_LIMIT", 3),
            image_daily_chat_limit=number("IMAGE_DAILY_CHAT_LIMIT", 10),
            image_daily_timezone=os.getenv("IMAGE_DAILY_TIMEZONE", "Asia/Tomsk").strip(),
            link_read_enabled=boolean("LINK_READ_ENABLED", False),
            link_daily_user_limit=number("LINK_DAILY_USER_LIMIT", 3),
            link_daily_chat_limit=number("LINK_DAILY_CHAT_LIMIT", 10),
            link_daily_timezone=os.getenv("LINK_DAILY_TIMEZONE", "Asia/Tomsk").strip(),
        )
