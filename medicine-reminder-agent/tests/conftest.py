from __future__ import annotations

import pytest

from app.config import (
    CallSettings,
    Config,
    ProviderSettings,
    Recipient,
    Schedule,
    TelegramSettings,
)
from app.db import Store
from app.engine import ReminderEngine
from app.models import CallOutcome
from app.telephony.console_provider import ConsoleProvider
from zoneinfo import ZoneInfo


class FakeNotifier:
    """Stands in for Telegram; records what would have been sent."""

    def __init__(self, succeed: bool = True, enabled: bool = True) -> None:
        self.succeed = succeed
        self.enabled = enabled
        self.messages: list[str] = []

    async def send(self, text: str, retries: int = 2) -> bool:
        self.messages.append(text)
        return self.succeed

    async def aclose(self) -> None:
        return None


def make_config(**overrides) -> Config:
    call = overrides.pop("call", CallSettings(retry_delay_seconds=300, max_attempts=2))
    provider = overrides.pop(
        "provider", ProviderSettings(name="console", public_base_url="")
    )
    recipient = Recipient(id="mom", name="Amma", phone="+919876543210",
                          language="en-IN", voice="Polly.Aditi")
    schedule = Schedule(
        id="morning",
        recipient_id="mom",
        message="Please take your morning tablet.",
        cron="0 8 * * mon,tue,wed,thu,fri,sat,sun",
    )
    return Config(
        timezone=ZoneInfo("Asia/Kolkata"),
        call=call,
        telegram=TelegramSettings(enabled=True, bot_token="t", chat_id="c",
                                  **overrides.pop("telegram", {})),
        provider=provider,
        recipients={"mom": recipient},
        schedules=[schedule],
        database_path=":memory:",
        **overrides,
    )


@pytest.fixture
def harness():
    """Engine wired to an in-memory store, a simulated phone line and a fake bot."""

    def _build(outcome=None, config=None, notifier=None):
        config = config or make_config()
        store = Store(":memory:")
        # Default: calls stay 'live' so each test drives outcomes explicitly.
        provider = ConsoleProvider(
            outcome=outcome or CallOutcome.IN_PROGRESS, ring_seconds=0
        )
        notifier = notifier or FakeNotifier()
        engine = ReminderEngine(config, store, provider, notifier)
        return engine, store, provider, notifier

    return _build
