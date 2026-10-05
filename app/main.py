import argparse
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.utils.token import TokenValidationError

from app import __version__
from app.config import ConfigurationError, Settings
from app.handlers.messages import MessageHandler
from app.logging_config import configure_logging
from app.services.local_history import LocalHistoryService
from app.services.daily_digest import DailyDigestService
from app.services.openai_service import OpenAIService
from app.services.telegram_history import TelegramHistoryService
from app.utils.rate_limit import RateLimiter

# `python -m app.main` executes with __name__="__main__"; keep our safe logger namespace.
logger = logging.getLogger("app.main")


async def run(settings: Settings) -> None:
    bot = Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=None))
    service = None
    handler = None
    history = None
    local_history = None
    daily_digest = None
    try:
        me = await bot.get_me()
        if not me.username:
            raise ConfigurationError("Telegram getMe returned no bot username")
        if settings.local_history_enabled:
            local_history = LocalHistoryService(settings, me.id)
            await local_history.start()
        service = OpenAIService(settings)
        if local_history is not None:
            daily_digest = DailyDigestService(settings, bot, local_history, service)
            await daily_digest.start()
        if settings.reply_context_enabled:
            if settings.telegram_api_id and settings.telegram_api_hash:
                candidate = TelegramHistoryService(settings, me.id)
                try:
                    await candidate.start()
                except Exception as exc:
                    logger.warning(
                        "Reply context disabled reason=mtproto_startup_failure kind=%s",
                        type(exc).__name__,
                    )
                else:
                    history = candidate
            else:
                # Existing production .env files continue working during migration.
                logger.warning("Reply context disabled reason=mtproto_credentials_missing")
        handler = MessageHandler(
            settings,
            me.id,
            me.username,
            service,
            RateLimiter(settings.rate_limit_requests, settings.rate_limit_period_seconds),
            history=history,
            local_history=local_history,
        )
        dispatcher = Dispatcher(disable_fsm=True)
        dispatcher.include_router(handler.router())
        dispatcher.shutdown.register(handler.shutdown)
        if daily_digest is not None:
            dispatcher.shutdown.register(daily_digest.shutdown)
        # Explicitly switch to polling; discard stale updates to avoid old charges.
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info(
            "Bot started version=%s username=@%s model=%s mode=polling allowed_chat_ids=%s",
            __version__,
            me.username,
            settings.openai_model,
            sorted(settings.allowed_chat_ids) if settings.allowed_chat_ids else "any_group",
        )
        logger.info("Reply context enabled=%s", history is not None)
        await dispatcher.start_polling(
            bot,
            allowed_updates=["message", "edited_message"] if local_history else ["message"],
            close_bot_session=False,
            tasks_concurrency_limit=settings.max_concurrent_requests * 4,
        )
    finally:
        if handler is not None:
            await handler.shutdown()
        try:
            if history is not None:
                await history.close()
        finally:
            try:
                if local_history is not None:
                    await local_history.close()
            finally:
                try:
                    if daily_digest is not None:
                        await daily_digest.shutdown()
                finally:
                    try:
                        if service is not None:
                            await service.close()
                    finally:
                        await bot.session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Telegram OpenAI bot with reply chains and /context"
    )
    parser.add_argument(
        "--check-config", action="store_true", help="Validate config without network calls"
    )
    args = parser.parse_args()
    configure_logging("INFO")
    try:
        settings = Settings.from_env()
        configure_logging(
            settings.log_level,
            (settings.telegram_bot_token, settings.openai_api_key, settings.telegram_api_hash),
        )
        if args.check_config:
            from aiogram.utils.token import validate_token

            validate_token(settings.telegram_bot_token)
            logger.info("Configuration valid; no network requests performed")
            return 0
        asyncio.run(run(settings))
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return 2
    except TokenValidationError:
        logger.error("Configuration error: TELEGRAM_BOT_TOKEN has invalid format")
        return 2
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error("Application stopped kind=%s", type(exc).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
