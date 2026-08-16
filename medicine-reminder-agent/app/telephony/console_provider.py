"""A provider that prints instead of dialling — for dry runs and tests."""

from __future__ import annotations

import itertools
import logging
import os
from datetime import datetime, timedelta

from ..models import CallHandle, CallOutcome, CallRequest, utcnow

log = logging.getLogger(__name__)


class ConsoleProvider:
    """Simulates calls so the whole retry/escalation flow can be exercised offline.

    ``CONSOLE_OUTCOME`` (default ``no_answer``) picks the simulated result and
    ``CONSOLE_RING_SECONDS`` how long the fake call "rings" before resolving.
    """

    name = "console"

    def __init__(
        self,
        outcome: CallOutcome | None = None,
        ring_seconds: float | None = None,
    ) -> None:
        env_outcome = os.environ.get("CONSOLE_OUTCOME", CallOutcome.NO_ANSWER.value)
        self.outcome = outcome or CallOutcome(env_outcome)
        self.ring_seconds = (
            ring_seconds
            if ring_seconds is not None
            else float(os.environ.get("CONSOLE_RING_SECONDS", "3"))
        )
        self.placed: list[CallRequest] = []
        self._ids = itertools.count(1)
        self._started: dict[str, datetime] = {}

    async def place_call(self, request: CallRequest) -> CallHandle:
        call_id = f"CONSOLE-{next(self._ids):06d}"
        self.placed.append(request)
        self._started[call_id] = utcnow()
        log.info(
            "[console] call %s -> %s (attempt %s): %s",
            call_id,
            request.to,
            request.attempt,
            request.message,
        )
        return CallHandle(provider_call_id=call_id)

    async def fetch_outcome(self, provider_call_id: str) -> CallOutcome:
        started = self._started.get(provider_call_id)
        if started and utcnow() - started < timedelta(seconds=self.ring_seconds):
            return CallOutcome.IN_PROGRESS
        return self.outcome

    async def aclose(self) -> None:
        return None
