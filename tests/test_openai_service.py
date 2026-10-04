import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    RateLimitError,
)

from app.handlers.messages import MODEL_RATE_LIMIT, TEMPORARY_ERROR
from app.services.openai_service import ModelRateLimited, ModelUnavailable, OpenAIService
from app.utils.reply_context import ReplyContext
from tests.conftest import make_message
from tests.test_reply_context import ancestor


def api_error(kind, status=None):
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    if status:
        return kind(
            "SECRET prompt/token",
            response=httpx2.Response(status, request=request),
            body={"secret": "SECRET"},
        )
    return kind(request=request)


@pytest.mark.parametrize(
    "error,expected,text",
    [
        (api_error(APITimeoutError), ModelUnavailable, TEMPORARY_ERROR),
        (api_error(APIConnectionError), ModelUnavailable, TEMPORARY_ERROR),
        (api_error(AuthenticationError, 401), ModelUnavailable, TEMPORARY_ERROR),
        (api_error(RateLimitError, 429), ModelRateLimited, MODEL_RATE_LIMIT),
        (api_error(APIStatusError, 503), ModelUnavailable, TEMPORARY_ERROR),
        (api_error(APIStatusError, 400), ModelUnavailable, TEMPORARY_ERROR),
    ],
)
async def test_api_errors_are_local_safe_and_not_retried(
    handler, sdk, telegram, caplog, error, expected, text
):
    sdk.responses.create.side_effect = error
    await handler.handle(make_message(), telegram)
    sdk.responses.create.assert_awaited_once()
    assert telegram.send_message.call_args.kwargs["text"] == text
    assert "SECRET" not in caplog.text


@pytest.mark.parametrize("prompt", ["", "   ", "x" * 12001])
async def test_service_rejects_invalid_input(settings, sdk, prompt):
    with pytest.raises(ValueError):
        await OpenAIService(settings, sdk).generate(prompt)
    sdk.responses.create.assert_not_awaited()


async def test_empty_model_output(handler, sdk, telegram):
    sdk.responses.create.return_value.output_text = " \n "
    await handler.handle(make_message(), telegram)
    assert telegram.send_message.call_args.kwargs["text"] == TEMPORARY_ERROR


async def test_wall_clock_timeout(settings, sdk):
    async def wait_forever(**kwargs):
        await asyncio.Event().wait()

    sdk.responses.create.side_effect = wait_forever
    service = OpenAIService(replace(settings, openai_timeout_seconds=0.02), sdk)
    with pytest.raises(ModelUnavailable):
        await service.generate("question")
    assert not service._semaphore.locked()


async def test_concurrency_and_cancellation(settings, sdk):
    active = peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def answer(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            entered.set()
        try:
            await release.wait()
            return SimpleNamespace(output_text="answer")
        finally:
            active -= 1

    sdk.responses.create.side_effect = answer
    service = OpenAIService(replace(settings, max_concurrent_requests=2), sdk)
    tasks = [asyncio.create_task(service.generate(str(i))) for i in range(6)]
    await asyncio.wait_for(entered.wait(), 1)
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    release.set()
    assert await asyncio.gather(*tasks[1:]) == ["answer"] * 5
    assert peak == 2
    assert active == 0


@pytest.mark.parametrize("with_context", [False, True, "recent"])
async def test_real_sdk_serializes_selected_input(settings, with_context):
    requests = []

    def transport(request):
        requests.append(request)
        return httpx2.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "gpt-6-luna",
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "Ответ", "annotations": []}],
                    }
                ],
            },
        )

    async with AsyncOpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(transport)),
    ) as client:
        context = ReplyContext((ancestor(1, "A"), ancestor(2, "B", own_bot=True)))
        options = (
            {"recent_context": context}
            if with_context == "recent"
            else {"context": context if with_context else None}
        )
        assert (
            await OpenAIService(settings, client).generate("объясни Docker", **options) == "Ответ"
        )
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    if with_context:
        assert [m["role"] for m in body["input"]] == ["user", "assistant", "user"]
        assert [json.loads(m["content"])["text"] for m in body["input"]] == [
            "A",
            "B",
            "объясни Docker",
        ]
        assert "untrusted" in body["instructions"]
    else:
        assert body["input"] == "объясни Docker"
    assert body["store"] is False
    assert body["model"] == "gpt-6-luna"
    assert "tools" not in body and "conversation" not in body and "previous_response_id" not in body


async def test_production_client_disables_retries_and_proxy_override(settings, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.com/proxy")
    service = OpenAIService(settings)
    try:
        assert service.client.max_retries == 0
        assert str(service.client.base_url) == "https://api.openai.com/v1/"
    finally:
        await service.close()
