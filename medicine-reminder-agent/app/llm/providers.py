"""Concrete model backends.

Most vendors — OpenAI, Google, Groq, xAI, DeepSeek, Mistral, Together — expose
an OpenAI-compatible chat-completions endpoint, so one adapter plus a base URL
covers nearly the whole market. Anthropic uses its own wire format and gets a
second adapter. Switching vendor is then a config change, not a code change,
which matters because prices in this tier moved repeatedly through 2026.
"""

from __future__ import annotations

import logging

from ..config import LLMSettings
from .base import SYSTEM_PROMPT

log = logging.getLogger(__name__)


class OpenAICompatReader:
    """Talks to any OpenAI-compatible chat-completions endpoint."""

    def __init__(self, settings: LLMSettings) -> None:
        from openai import AsyncOpenAI

        self.name = f"{settings.provider}:{settings.model}"
        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url or None,
            timeout=settings.timeout_seconds,
            # One retry only: a second attempt still fits inside the call,
            # a third leaves the person listening to silence.
            max_retries=1,
        )
        # Providers disagree about which of these they accept; each is dropped
        # the first time it is rejected, and stays dropped for the process.
        self._use_json_mode = True
        self._token_field = "max_tokens"

    def _kwargs(self, prompt: str) -> dict[str, object]:
        kwargs: dict[str, object] = {
            "model": self._settings.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            self._token_field: self._settings.max_tokens,
        }
        if self._use_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def _relax(self, message: str) -> bool:
        """Drop whichever optional parameter the provider just rejected."""
        lowered = message.lower()
        if self._use_json_mode and "response_format" in lowered:
            log.info("%s rejects response_format — retrying without it", self.name)
            self._use_json_mode = False
            return True
        if self._token_field == "max_tokens" and "max_tokens" in lowered:
            log.info("%s wants max_completion_tokens — switching", self.name)
            self._token_field = "max_completion_tokens"
            return True
        return False

    async def read(self, prompt: str) -> str | None:
        from openai import BadRequestError

        for _ in range(3):  # at most two parameter relaxations, then give up
            try:
                response = await self._client.chat.completions.create(
                    **self._kwargs(prompt)  # type: ignore[arg-type]
                )
            except BadRequestError as exc:
                if self._relax(str(exc)):
                    continue
                log.warning("%s rejected the request: %s", self.name, exc)
                return None
            except Exception as exc:
                log.warning("%s call failed: %s", self.name, exc)
                return None
            return response.choices[0].message.content
        return None

    async def aclose(self) -> None:
        await self._client.close()


class AnthropicReader:
    """Talks to the Claude Messages API."""

    def __init__(self, settings: LLMSettings) -> None:
        from anthropic import AsyncAnthropic

        self.name = f"anthropic:{settings.model}"
        self._settings = settings
        self._client = AsyncAnthropic(
            api_key=settings.api_key,
            timeout=settings.timeout_seconds,
            max_retries=1,
        )

    async def read(self, prompt: str) -> str | None:
        try:
            # No thinking and no `effort`: the fast Claude tier predates
            # adaptive thinking and rejects `effort`, and on a live call the
            # first token should arrive as early as possible anyway.
            response = await self._client.messages.create(
                model=self._settings.model,
                max_tokens=self._settings.max_tokens,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            log.warning("%s call failed: %s", self.name, exc)
            return None

        return "".join(
            block.text for block in response.content if block.type == "text"
        )

    async def aclose(self) -> None:
        await self._client.close()
