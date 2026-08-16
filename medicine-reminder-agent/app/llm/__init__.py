"""Reading spoken replies — provider-neutral.

The keypad ("press 1") works, but people answer a phone by talking, and older
people especially do. This layer turns whatever they said into a decision the
engine can act on, plus the sentence the call speaks back.
"""

from __future__ import annotations

import logging

from ..config import LLMSettings, Recipient, Schedule
from ..models import ReplyIntent
from .base import (
    ReminderReading,
    ReplyReader,
    build_user_turn,
    keyword_reading,
    parse_reading,
)

log = logging.getLogger(__name__)

__all__ = ["ReminderLLM", "ReminderReading", "build_llm", "keyword_reading"]

#: Vendors that speak the OpenAI chat-completions dialect.
OPENAI_COMPATIBLE = {"openai", "gemini", "groq", "xai", "deepseek", "mistral", "custom"}


class ReminderLLM:
    """Reads spoken replies, and degrades to keyword matching when it can't."""

    def __init__(self, settings: LLMSettings, reader: ReplyReader | None) -> None:
        self._settings = settings
        self._reader = reader

    @property
    def enabled(self) -> bool:
        return self._reader is not None

    @property
    def describe(self) -> str:
        return self._reader.name if self._reader else "keyword matching (no model)"

    async def read_reply(
        self, *, recipient: Recipient, schedule: Schedule, transcript: str
    ) -> ReminderReading:
        """Turn a speech transcript into an intent plus what to say back."""
        # Silence needs no model — skip the round trip and the latency.
        if not transcript.strip() or self._reader is None:
            return self._bound(keyword_reading(transcript, recipient))

        raw = await self._reader.read(build_user_turn(recipient, schedule, transcript))
        reading = parse_reading(raw or "")
        if reading is None:
            log.warning("no usable reading from %s — falling back", self._reader.name)
            return self._bound(keyword_reading(transcript, recipient))

        log.info("reply read as %s by %s", reading.intent, self._reader.name)
        return self._bound(reading)

    def _bound(self, reading: ReminderReading) -> ReminderReading:
        """Trust the classification; bound the number it produced."""
        reading.snooze_minutes = max(
            0, min(reading.snooze_minutes, self._settings.max_snooze_minutes)
        )
        if reading.as_intent() is ReplyIntent.SNOOZE and reading.snooze_minutes == 0:
            reading.snooze_minutes = self._settings.default_snooze_minutes
        return reading

    async def aclose(self) -> None:
        if self._reader is not None:
            await self._reader.aclose()


def build_llm(settings: LLMSettings) -> ReminderLLM:
    """Construct the configured backend, or a keyword-only reader."""
    if not (settings.enabled and settings.api_key):
        if settings.enabled:
            log.warning("no LLM API key set — falling back to keyword matching")
        return ReminderLLM(settings, None)

    from .providers import AnthropicReader, OpenAICompatReader

    reader: ReplyReader
    if settings.provider == "anthropic":
        reader = AnthropicReader(settings)
    elif settings.provider in OPENAI_COMPATIBLE:
        reader = OpenAICompatReader(settings)
    else:  # pragma: no cover - guarded at config load
        raise ValueError(f"unsupported LLM provider '{settings.provider}'")

    log.info("understanding replies with %s", reader.name)
    return ReminderLLM(settings, reader)
