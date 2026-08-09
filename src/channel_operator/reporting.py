from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import ReportingConfig

LOGGER = logging.getLogger(__name__)
REPORT_TEXT_LIMIT = 4000


class ReporterError(RuntimeError):
    """Raised when the Telegram Bot API cannot deliver a report."""

    def __init__(self, message: str, *, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class BotReporter:
    def __init__(
        self,
        config: ReportingConfig,
        client: httpx.AsyncClient | None = None,
    ):
        self.config = config
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=10.0, read=10.0, write=10.0, pool=10.0)
        )
        self._base_url = f"https://api.telegram.org/bot{config.bot_token}"

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.client.post(f"{self._base_url}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise ReporterError(f"Bot API 网络错误：{type(exc).__name__}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise ReporterError(f"Bot API 返回了无效 JSON（HTTP {response.status_code}）") from exc
        if not isinstance(body, dict):
            raise ReporterError("Bot API 返回格式无效")
        if response.is_error or not body.get("ok"):
            description = str(body.get("description") or f"HTTP {response.status_code}")
            parameters = body.get("parameters")
            retry_after = None
            if isinstance(parameters, dict) and parameters.get("retry_after") is not None:
                try:
                    retry_after = int(parameters["retry_after"])
                except (TypeError, ValueError):
                    retry_after = None
            raise ReporterError(description, retry_after=retry_after)
        result = body.get("result")
        return result if isinstance(result, dict) else {"value": result}

    @staticmethod
    def _split_text(text: str) -> list[str]:
        if len(text) <= REPORT_TEXT_LIMIT:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            if len(remaining) <= REPORT_TEXT_LIMIT:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n\n", 0, REPORT_TEXT_LIMIT)
            if split_at < 1:
                split_at = remaining.rfind("\n", 0, REPORT_TEXT_LIMIT)
            if split_at < 1:
                split_at = REPORT_TEXT_LIMIT
            chunks.append(remaining[:split_at].rstrip())
            remaining = remaining[split_at:].lstrip()
        return chunks

    async def _send_chunk(self, text: str, *, strict: bool) -> bool:
        delays = (0, 2, 10)
        last_error: ReporterError | None = None
        for index, delay in enumerate(delays):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._call(
                    "sendMessage",
                    {"chat_id": self.config.chat_id, "text": text},
                )
                return True
            except ReporterError as exc:
                last_error = exc
                if exc.retry_after and index + 1 < len(delays):
                    await asyncio.sleep(max(1, exc.retry_after))
        if strict:
            raise last_error or ReporterError("Bot API 发送失败")
        LOGGER.error(
            "机器人报告发送失败：%s",
            last_error or "未知错误",
        )
        return False

    async def send(self, text: str, *, strict: bool = False) -> bool:
        delivered = True
        for chunk in self._split_text(text):
            if not await self._send_chunk(chunk, strict=strict):
                delivered = False
        return delivered

    async def doctor(self) -> str:
        bot = await self._call("getMe", {})
        username = str(bot.get("username") or bot.get("id") or "unknown")
        await self.send("✅ Telegram 频道运营机器人报告测试成功", strict=True)
        return username
