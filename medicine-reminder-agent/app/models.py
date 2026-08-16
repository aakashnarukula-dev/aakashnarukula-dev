"""Core value types shared by the scheduler, engine, telephony and storage layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, Enum):
    """Lifecycle of a single scheduled reminder (one dose, one person)."""

    CALLING = "calling"            # a call is live / awaiting its outcome
    WAITING_RETRY = "waiting_retry"  # missed, next attempt is queued
    ACKNOWLEDGED = "acknowledged"  # they answered and confirmed
    ESCALATED = "escalated"        # every attempt missed, Telegram alert sent
    FAILED = "failed"              # every attempt missed, alert could not be sent


TERMINAL_STATUSES = {RunStatus.ACKNOWLEDGED, RunStatus.ESCALATED, RunStatus.FAILED}


class CallOutcome(str, Enum):
    """Normalised result of one call attempt, across providers."""

    IN_PROGRESS = "in_progress"
    ANSWERED_HUMAN = "answered_human"
    ANSWERED_MACHINE = "answered_machine"  # voicemail / IVR — never counts as a response
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"


#: Outcomes that mean the phone was picked up by a person.
ANSWERED_OUTCOMES = {CallOutcome.ANSWERED_HUMAN}


@dataclass(frozen=True)
class CallRequest:
    """Everything a telephony provider needs to dial one reminder."""

    to: str
    from_: str
    run_id: int
    attempt: int
    token: str
    message: str
    voice: str
    language: str
    ring_timeout: int


@dataclass(frozen=True)
class CallHandle:
    provider_call_id: str


@dataclass
class Run:
    id: int
    schedule_id: str
    recipient_id: str
    scheduled_for: datetime
    status: RunStatus
    attempt: int
    token: str
    next_action_at: datetime | None
    last_outcome: CallOutcome | None
    created_at: datetime
    updated_at: datetime
