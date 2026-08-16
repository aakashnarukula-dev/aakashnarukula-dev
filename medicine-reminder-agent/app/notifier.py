"""Telegram escalation — the alert you get when a reminder goes unanswered."""

from __future__ import annotations

import asyncio
import logging

import httpx

from .config import TelegramSettings

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Sends messages through the Telegram Bot API."""

    def __init__(self, settings: TelegramSettings, timeout: float = 15.0) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=timeout)

    @property
    def enabled(self) -> bool:
        return bool(
            self._settings.enabled
            and self._settings.bot_token
            and self._settings.chat_id
        )

    async def send(self, text: str, retries: int = 2) -> bool:
        """Post a message. Returns True once Telegram accepts it."""
        if not self.enabled:
            log.warning("telegram disabled — dropping alert: %s", text)
            return False

        url = f"{TELEGRAM_API}/bot{self._settings.bot_token}/sendMessage"
        payload = {
            "chat_id": self._settings.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }

        for attempt in range(retries + 1):
            try:
                response = await self._client.post(url, json=payload)
                if response.status_code == 200:
                    return True
                log.error(
                    "telegram sendMessage failed (%s): %s",
                    response.status_code,
                    response.text[:300],
                )
                # 4xx means a bad token/chat id — retrying will not help.
                if 400 <= response.status_code < 500:
                    return False
            except httpx.HTTPError as exc:
                log.error("telegram request error: %s", exc)
            if attempt < retries:
                await asyncio.sleep(2 ** attempt)
        return False

    async def aclose(self) -> None:
        await self._client.aclose()
