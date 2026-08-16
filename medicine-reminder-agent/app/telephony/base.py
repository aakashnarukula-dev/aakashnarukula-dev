"""Telephony provider interface plus the shared provider-status mapping."""

from __future__ import annotations

from typing import Protocol

from ..models import CallHandle, CallOutcome, CallRequest

#: Twilio `AnsweredBy` values that mean a machine, not a person, took the call.
MACHINE_ANSWERS = {
    "machine_start",
    "machine_end_beep",
    "machine_end_silence",
    "machine_end_other",
    "fax",
}


class TelephonyProvider(Protocol):
    """Anything that can place an outbound reminder call."""

    name: str

    async def place_call(self, request: CallRequest) -> CallHandle:
        """Dial the recipient. Raises on failure to place the call at all."""

    async def fetch_outcome(self, provider_call_id: str) -> CallOutcome:
        """Poll a call's current outcome (used when webhooks are unavailable)."""

    async def aclose(self) -> None:
        """Release any provider resources."""


def map_call_status(status: str | None, answered_by: str | None = None) -> CallOutcome:
    """Normalise a provider call status into a :class:`CallOutcome`."""
    normalised = (status or "").strip().lower()
    answered = (answered_by or "").strip().lower()

    if normalised in {"queued", "initiated", "ringing", "in-progress"}:
        return CallOutcome.IN_PROGRESS
    if normalised == "completed":
        if answered in MACHINE_ANSWERS:
            return CallOutcome.ANSWERED_MACHINE
        return CallOutcome.ANSWERED_HUMAN
    if normalised == "busy":
        return CallOutcome.BUSY
    if normalised in {"no-answer", "no_answer"}:
        return CallOutcome.NO_ANSWER
    return CallOutcome.FAILED
