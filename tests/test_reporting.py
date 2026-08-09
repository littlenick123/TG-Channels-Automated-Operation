from __future__ import annotations

import json

import httpx
import pytest

from channel_operator.config import ReportingConfig
from channel_operator.reporting import BotReporter, ReporterError


@pytest.mark.asyncio
async def test_reporter_doctor_checks_bot_and_sends_private_message():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "ops_bot"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reporter = BotReporter(
            ReportingConfig("123:test", (987654321, 123456789)), client=client
        )
        username = await reporter.doctor()

    assert username == "ops_bot"
    assert calls[0][0].endswith("/getMe")
    assert calls[1][0].endswith("/sendMessage")
    assert calls[1][1]["chat_id"] == 987654321
    assert calls[2][0].endswith("/sendMessage")
    assert calls[2][1]["chat_id"] == 123456789


@pytest.mark.asyncio
async def test_reporter_failure_is_non_blocking_by_default(monkeypatch):
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            401,
            json={"ok": False, "description": "Unauthorized"},
        )

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("channel_operator.reporting.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reporter = BotReporter(
            ReportingConfig("bad-token", (987654321,)), client=client
        )
        assert await reporter.send("test") is False
        with pytest.raises(ReporterError, match="Unauthorized"):
            await reporter.send("test", strict=True)

    assert attempts == 6


@pytest.mark.asyncio
async def test_one_recipient_failure_does_not_block_later_recipients(monkeypatch):
    attempted_chat_ids = []

    def handler(request: httpx.Request) -> httpx.Response:
        chat_id = json.loads(request.content)["chat_id"]
        attempted_chat_ids.append(chat_id)
        if chat_id == 111:
            return httpx.Response(
                400,
                json={"ok": False, "description": "Bad Request: chat not found"},
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("channel_operator.reporting.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reporter = BotReporter(ReportingConfig("123:test", (111, 222)), client=client)
        assert await reporter.send("test") is False

    assert attempted_chat_ids == [111, 111, 111, 222]


@pytest.mark.asyncio
async def test_reporter_splits_long_reports():
    messages = []

    def handler(request: httpx.Request) -> httpx.Response:
        messages.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    text = "A" * 3900 + "\n\n" + "B" * 3900
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reporter = BotReporter(ReportingConfig("123:test", (987654321,)), client=client)
        assert await reporter.send(text) is True

    assert len(messages) == 2
    assert all(len(message) <= 4000 for message in messages)


@pytest.mark.asyncio
async def test_reporter_timeout_does_not_expose_token_or_raise(monkeypatch, caplog):
    token = "123456:very-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async def no_sleep(delay):
        return None

    monkeypatch.setattr("channel_operator.reporting.asyncio.sleep", no_sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        reporter = BotReporter(ReportingConfig(token, (987654321,)), client=client)
        assert await reporter.send("test") is False

    assert token not in caplog.text
