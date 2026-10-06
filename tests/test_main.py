import os
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app import main
from app.services.local_history import LocalHistoryService
from tests.conftest import make_message


async def test_startup_identity_polling_and_cleanup(settings, monkeypatch):
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7654, username="from_get_me")),
        delete_webhook=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    service = SimpleNamespace(close=AsyncMock())
    dispatcher = SimpleNamespace(
        include_router=Mock(),
        shutdown=SimpleNamespace(register=Mock()),
        start_polling=AsyncMock(),
    )
    bot_constructor = Mock(return_value=bot)
    monkeypatch.setattr(main, "Bot", bot_constructor)
    monkeypatch.setattr(main, "OpenAIService", Mock(return_value=service))
    monkeypatch.setattr(main, "Dispatcher", Mock(return_value=dispatcher))
    await main.run(settings)
    bot.get_me.assert_awaited_once()
    bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=True)
    router = dispatcher.include_router.call_args.args[0]
    handler = router.message.handlers[0].callback.__self__
    assert handler.bot_username == "from_get_me"
    assert handler.bot_id == 7654
    dispatcher.start_polling.assert_awaited_once_with(
        bot,
        allowed_updates=["message"],
        close_bot_session=False,
        tasks_concurrency_limit=20,
    )
    service.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()


async def test_failed_get_me_closes_session(settings, monkeypatch):
    bot = SimpleNamespace(
        get_me=AsyncMock(side_effect=RuntimeError("secret")),
        session=SimpleNamespace(close=AsyncMock()),
    )
    constructor = Mock()
    monkeypatch.setattr(main, "Bot", Mock(return_value=bot))
    monkeypatch.setattr(main, "OpenAIService", constructor)
    with pytest.raises(RuntimeError):
        await main.run(settings)
    bot.session.close.assert_awaited_once()
    constructor.assert_not_called()


def test_module_entrypoint_validates_without_exposing_secrets():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENAI_", "TELEGRAM_"))
    }
    env.update(TELEGRAM_BOT_TOKEN="123456:test-cli-secret", OPENAI_API_KEY="test-api-secret")
    result = subprocess.run(
        [sys.executable, "-m", "app.main", "--check-config"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,  # Allow cold imports on small Ubuntu VPS instances.
        check=False,
    )
    assert result.returncode == 0
    assert "Configuration valid; no network requests performed" in result.stdout
    assert "test-cli-secret" not in result.stdout + result.stderr
    assert "test-api-secret" not in result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["enabled", "disabled", "missing", "failed_start"])
async def test_mtproto_singleton_startup_fallback_and_shutdown(settings, monkeypatch, caplog, mode):
    if mode != "missing":
        settings = replace(
            settings,
            telegram_api_id=123,
            telegram_api_hash="fake-hash",
            reply_context_enabled=mode != "disabled",
        )
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7654, username="from_get_me")),
        delete_webhook=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    model = SimpleNamespace(close=AsyncMock())
    history = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    dispatcher = SimpleNamespace(
        include_router=Mock(), shutdown=SimpleNamespace(register=Mock()), start_polling=AsyncMock()
    )
    if mode == "failed_start":
        history.start.side_effect = RuntimeError("SECRET auth error")
    factory = Mock(return_value=history)
    monkeypatch.setattr(main, "Bot", Mock(return_value=bot))
    monkeypatch.setattr(main, "OpenAIService", Mock(return_value=model))
    monkeypatch.setattr(main, "TelegramHistoryService", factory)
    monkeypatch.setattr(main, "Dispatcher", Mock(return_value=dispatcher))
    await main.run(settings)
    handler = dispatcher.include_router.call_args.args[0].message.handlers[0].callback.__self__
    if mode == "enabled":
        factory.assert_called_once_with(settings, 7654)
        history.start.assert_awaited_once()
        history.close.assert_awaited_once()
        assert handler.history is history
    else:
        assert handler.history is None
        if mode != "failed_start":
            factory.assert_not_called()
    dispatcher.start_polling.assert_awaited_once()
    model.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()
    assert "SECRET" not in caplog.text


async def test_local_sqlite_startup_save_shutdown_and_reopen(settings, tmp_path, monkeypatch):
    settings = replace(
        settings,
        local_history_enabled=True,
        local_history_db_path=str(tmp_path / "state" / "history.sqlite3"),
    )
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7654, username="from_get_me")),
        delete_webhook=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    model = SimpleNamespace(close=AsyncMock())
    dispatcher = SimpleNamespace(
        include_router=Mock(), shutdown=SimpleNamespace(register=Mock()), start_polling=AsyncMock()
    )

    async def polling(*args, **kwargs):
        assert kwargs["allowed_updates"] == ["message", "edited_message"]
        handler = dispatcher.include_router.call_args.args[0].message.handlers[0].callback.__self__
        await handler.handle(make_message("Local only", entities=False), bot)

    dispatcher.start_polling.side_effect = polling
    monkeypatch.setattr(main, "Bot", Mock(return_value=bot))
    monkeypatch.setattr(main, "OpenAIService", Mock(return_value=model))
    monkeypatch.setattr(main, "Dispatcher", Mock(return_value=dispatcher))
    await main.run(settings)
    handler = dispatcher.include_router.call_args.args[0].message.handlers[0].callback.__self__
    assert handler.local_history._connection is None
    reopened = LocalHistoryService(settings, 7654)
    await reopened.start()
    try:
        assert (await reopened.get_recent_messages(-100123, 8, 50))[0].text == "Local only"
    finally:
        await reopened.close()
    model.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()


@pytest.mark.parametrize("failure", ["startup", "close"])
async def test_local_history_failure_still_closes_provider_clients(settings, monkeypatch, failure):
    settings = replace(settings, local_history_enabled=True)
    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=123456, username="my_bot")),
        delete_webhook=AsyncMock(),
        session=SimpleNamespace(close=AsyncMock()),
    )
    model = SimpleNamespace(close=AsyncMock())
    local = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
    getattr(local, "start" if failure == "startup" else "close").side_effect = RuntimeError("fail")
    dispatcher = SimpleNamespace(
        include_router=Mock(), shutdown=SimpleNamespace(register=Mock()), start_polling=AsyncMock()
    )
    monkeypatch.setattr(main, "Bot", Mock(return_value=bot))
    factory = Mock(return_value=model)
    monkeypatch.setattr(main, "OpenAIService", factory)
    monkeypatch.setattr(main, "LocalHistoryService", Mock(return_value=local))
    monkeypatch.setattr(main, "Dispatcher", Mock(return_value=dispatcher))
    with pytest.raises(RuntimeError):
        await main.run(settings)
    bot.session.close.assert_awaited_once()
    local.close.assert_awaited_once()
    if failure == "close":
        model.close.assert_awaited_once()
    else:
        factory.assert_not_called()


@pytest.mark.parametrize("failure", [None, "webhook", "polling", "shutdown"])
async def test_daily_scheduler_lifecycle_closes_before_dependencies(settings, monkeypatch, failure):
    settings = replace(
        settings,
        local_history_enabled=True,
        daily_digest_enabled=True,
        daily_digest_chat_ids=frozenset({-100123}),
    )
    events = []

    def step(name):
        async def action(*args, **kwargs):
            events.append(name)
            if name == failure:
                raise RuntimeError("Failure")

        return AsyncMock(side_effect=action)

    bot = SimpleNamespace(
        get_me=AsyncMock(return_value=SimpleNamespace(id=7654, username="my_bot")),
        delete_webhook=step("webhook"),
        session=SimpleNamespace(close=step("bot_close")),
    )
    model = SimpleNamespace(close=step("model_close"))
    local = SimpleNamespace(start=step("local_start"), close=step("local_close"))
    digest = SimpleNamespace(start=step("digest_start"), shutdown=step("shutdown"))
    dispatcher = SimpleNamespace(
        include_router=Mock(),
        shutdown=SimpleNamespace(register=Mock()),
        start_polling=step("polling"),
    )
    monkeypatch.setattr(main, "Bot", Mock(return_value=bot))
    monkeypatch.setattr(main, "OpenAIService", Mock(return_value=model))
    monkeypatch.setattr(main, "LocalHistoryService", Mock(return_value=local))
    monkeypatch.setattr(main, "DailyDigestService", Mock(return_value=digest))
    monkeypatch.setattr(main, "Dispatcher", Mock(return_value=dispatcher))
    if failure:
        with pytest.raises(RuntimeError):
            await main.run(settings)
    else:
        await main.run(settings)
    assert events.index("shutdown") < events.index("local_close")
    assert events[-3:] == ["local_close", "model_close", "bot_close"]
    if failure == "webhook":
        digest.start.assert_not_awaited()
    else:
        assert events.index("webhook") < events.index("digest_start") < events.index("polling")


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_validates_daily_timezone_without_network_or_secrets(enabled):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENAI_", "TELEGRAM_", "DAILY_DIGEST_", "LOCAL_HISTORY_"))
    }
    env.update(
        TELEGRAM_BOT_TOKEN="123456:test-cli-secret",
        OPENAI_API_KEY="test-api-secret",
        LOCAL_HISTORY_ENABLED="true",
        DAILY_DIGEST_ENABLED=str(enabled).lower(),
        DAILY_DIGEST_CHAT_IDS="-100123",
        DAILY_DIGEST_TIMEZONE="Invalid/SECRET_TIMEZONE",
    )
    result = subprocess.run(
        [sys.executable, "-m", "app.main", "--check-config"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == (2 if enabled else 0)
    for secret in ("test-cli-secret", "test-api-secret", "SECRET_TIMEZONE"):
        assert secret not in result.stdout + result.stderr
