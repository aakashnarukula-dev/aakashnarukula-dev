"""The reminder state machine: call, retry once, then escalate to Telegram."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta
from html import escape

from .config import Config, Schedule
from .db import Store
from .models import (
    TERMINAL_STATUSES,
    CallOutcome,
    CallRequest,
    Run,
    RunStatus,
    utcnow,
)
from .notifier import TelegramNotifier
from .telephony import TelephonyProvider

log = logging.getLogger(__name__)

#: How often a failed Telegram alert is retried, and for how long we keep trying.
ESCALATION_RETRY_SECONDS = 60
ESCALATION_GIVE_UP_SECONDS = 1800

OUTCOME_LABELS = {
    CallOutcome.ANSWERED_HUMAN: "answered, no confirmation",
    CallOutcome.ANSWERED_MACHINE: "voicemail",
    CallOutcome.NO_ANSWER: "no answer",
    CallOutcome.BUSY: "busy",
    CallOutcome.FAILED: "call failed",
    CallOutcome.IN_PROGRESS: "in progress",
}


class ReminderEngine:
    """Owns every transition a reminder can make.

    All entry points are idempotent: duplicated webhooks, a poller racing a callback,
    or a restart mid-flight must never produce a second call or a second alert.
    """

    def __init__(
        self,
        config: Config,
        store: Store,
        provider: TelephonyProvider,
        notifier: TelegramNotifier,
    ) -> None:
        self.config = config
        self.store = store
        self.provider = provider
        self.notifier = notifier

    # ------------------------------------------------------------ triggering

    async def trigger_schedule(
        self, schedule: Schedule, scheduled_for: datetime | None = None, *,
        now: datetime | None = None,
    ) -> int | None:
        """Start a reminder. Returns the run id, or None if it was already started."""
        now = now or utcnow()
        scheduled_for = scheduled_for or now
        run_id = self.store.create_run(
            schedule_id=schedule.id,
            recipient_id=schedule.recipient_id,
            scheduled_for=scheduled_for,
            token=secrets.token_urlsafe(16),
            now=now,
        )
        if run_id is None:
            log.info(
                "schedule '%s' for %s already triggered — skipping duplicate",
                schedule.id,
                scheduled_for.isoformat(),
            )
            return None

        log.info("run %s: reminder '%s' starting", run_id, schedule.id)
        await self._place_attempt(run_id, 1, now=now)
        return run_id

    async def _place_attempt(
        self, run_id: int, attempt_no: int, *, now: datetime | None = None
    ) -> None:
        now = now or utcnow()
        run = self.store.get_run(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return
        if attempt_no > self.config.call.max_attempts:
            await self._escalate(run, now=now)
            return

        schedule = self.config.schedule(run.schedule_id)
        recipient = self.config.recipient(run.recipient_id)

        self.store.update_run(
            run_id,
            now=now,
            status=RunStatus.CALLING,
            attempt=attempt_no,
            clear_next_action=True,
        )
        self.store.start_attempt(run_id, attempt_no, now)

        request = CallRequest(
            to=recipient.phone,
            from_=self.config.provider.from_number,
            run_id=run_id,
            attempt=attempt_no,
            token=run.token,
            message=schedule.message,
            voice=recipient.voice,
            language=recipient.language,
            ring_timeout=self.config.call.ring_timeout_seconds,
        )

        try:
            handle = await self.provider.place_call(request)
        except Exception as exc:  # provider/network failure — treat as a missed call
            log.error("run %s attempt %s: could not place call: %s", run_id, attempt_no, exc)
            await self.record_outcome(
                run_id, attempt_no, CallOutcome.FAILED, detail=str(exc)[:300], now=now
            )
            return

        self.store.set_attempt_call_id(run_id, attempt_no, handle.provider_call_id)
        log.info(
            "run %s attempt %s: calling %s (%s)",
            run_id,
            attempt_no,
            recipient.name,
            handle.provider_call_id,
        )

    # -------------------------------------------------------------- outcomes

    async def record_acknowledgement(
        self, run_id: int, attempt_no: int, *, source: str = "keypress",
        now: datetime | None = None,
    ) -> bool:
        """They responded. Cancels any pending retry and closes the run."""
        now = now or utcnow()
        run = self.store.get_run(run_id)
        if run is None:
            log.warning("acknowledgement for unknown run %s", run_id)
            return False
        if run.status in TERMINAL_STATUSES:
            return run.status is RunStatus.ACKNOWLEDGED

        self.store.finish_attempt(
            run_id, attempt_no, CallOutcome.ANSWERED_HUMAN, now, detail=source
        )
        self.store.update_run(
            run_id,
            now=now,
            status=RunStatus.ACKNOWLEDGED,
            last_outcome=CallOutcome.ANSWERED_HUMAN,
            clear_next_action=True,
        )
        log.info("run %s: acknowledged via %s", run_id, source)

        if self.config.telegram.notify_on_acknowledged:
            await self.notifier.send(self._acknowledged_message(run, now))
        return True

    async def record_outcome(
        self,
        run_id: int,
        attempt_no: int,
        outcome: CallOutcome,
        *,
        detail: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Apply a finished call's result: retry it, or escalate."""
        now = now or utcnow()
        if outcome is CallOutcome.IN_PROGRESS:
            return

        run = self.store.get_run(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return
        if run.attempt != attempt_no:
            log.debug(
                "run %s: ignoring outcome for stale attempt %s (now on %s)",
                run_id, attempt_no, run.attempt,
            )
            return
        if not self.store.finish_attempt(run_id, attempt_no, outcome, now, detail=detail):
            return  # another webhook already resolved this attempt

        responded = (
            outcome is CallOutcome.ANSWERED_HUMAN
            and self.config.call.confirmation_mode == "answered"
        )
        if responded:
            await self.record_acknowledgement(
                run_id, attempt_no, source="answered", now=now
            )
            return

        log.info(
            "run %s attempt %s: %s", run_id, attempt_no, OUTCOME_LABELS.get(outcome, outcome)
        )
        if attempt_no < self.config.call.max_attempts:
            retry_at = now + timedelta(seconds=self.config.call.retry_delay_seconds)
            self.store.update_run(
                run_id,
                now=now,
                status=RunStatus.WAITING_RETRY,
                next_action_at=retry_at,
                last_outcome=outcome,
            )
            log.info("run %s: retrying at %s", run_id, retry_at.isoformat())
            return

        self.store.update_run(run_id, now=now, last_outcome=outcome)
        refreshed = self.store.get_run(run_id)
        if refreshed is not None:
            await self._escalate(refreshed, now=now)

    # ------------------------------------------------------------ escalation

    async def _escalate(self, run: Run, *, now: datetime | None = None) -> None:
        now = now or utcnow()
        if not self.notifier.enabled:
            # Alerts are switched off on purpose — close the run out rather than
            # retrying a delivery that can never succeed.
            self.store.update_run(
                run.id, now=now, status=RunStatus.ESCALATED, clear_next_action=True
            )
            log.warning(
                "run %s: missed every call, but Telegram alerts are disabled", run.id
            )
            return

        sent = await self.notifier.send(self._escalation_message(run, now))
        if sent:
            self.store.update_run(
                run.id, now=now, status=RunStatus.ESCALATED, clear_next_action=True
            )
            log.warning("run %s: escalated to Telegram", run.id)
            return

        # Keep retrying the alert for a while — a dropped alert is the worst failure.
        give_up_at = run.scheduled_for + timedelta(seconds=ESCALATION_GIVE_UP_SECONDS)
        if now >= give_up_at:
            self.store.update_run(
                run.id, now=now, status=RunStatus.FAILED, clear_next_action=True
            )
            log.error("run %s: giving up on the Telegram alert", run.id)
            return
        self.store.update_run(
            run.id,
            now=now,
            status=RunStatus.FAILED,
            next_action_at=now + timedelta(seconds=ESCALATION_RETRY_SECONDS),
        )
        log.error("run %s: Telegram alert failed, will retry", run.id)

    # ----------------------------------------------------------------- ticks

    async def tick(self, now: datetime | None = None) -> None:
        """Periodic housekeeping: due retries, call polling, alert retries."""
        now = now or utcnow()
        for run in self.store.due_by_status(RunStatus.WAITING_RETRY, now):
            await self._place_attempt(run.id, run.attempt + 1, now=now)

        await self._poll_in_flight(now)

        for run in self.store.due_by_status(RunStatus.FAILED, now):
            await self._escalate(run, now=now)

    async def _poll_in_flight(self, now: datetime) -> None:
        """Ask the provider how live calls ended.

        Without a public URL this is the only source of truth. With webhooks it is
        the safety net for callbacks that never arrive.
        """
        settle_after = (
            0
            if not self.config.provider.public_base_url
            else self.config.call.stale_call_seconds
        )
        abandon_after = self.config.call.stale_call_seconds + max(
            self.config.call.ring_timeout_seconds * 4, 120
        )

        for run, call_id in self.store.in_flight_calls():
            age = (now - run.updated_at).total_seconds()
            if age < settle_after:
                continue
            outcome = await self.provider.fetch_outcome(call_id)
            if outcome is CallOutcome.IN_PROGRESS:
                if age > abandon_after:
                    await self.record_outcome(
                        run.id,
                        run.attempt,
                        CallOutcome.FAILED,
                        detail="no outcome reported by provider",
                        now=now,
                    )
                continue
            await self.record_outcome(
                run.id, run.attempt, outcome, detail="polled", now=now
            )

    # -------------------------------------------------------------- messages

    def _local(self, moment: datetime) -> str:
        return moment.astimezone(self.config.timezone).strftime("%d %b %Y, %I:%M %p")

    def _escalation_message(self, run: Run, now: datetime) -> str:
        schedule = self.config.schedule(run.schedule_id)
        recipient = self.config.recipient(run.recipient_id)
        attempts = self.store.attempts_for(run.id)
        tried = ", ".join(
            OUTCOME_LABELS.get(CallOutcome(row["outcome"]), row["outcome"])
            for row in attempts
        ) or "none"

        return (
            "🔴 <b>Missed medicine reminder</b>\n\n"
            f"<b>{escape(recipient.name)}</b> ({escape(recipient.phone)}) did not "
            f"respond after {len(attempts)} call(s).\n\n"
            f"💊 <b>Reminder:</b> {escape(schedule.id)}\n"
            f"🗣 <b>Message:</b> {escape(schedule.message)}\n"
            f"🕒 <b>Due at:</b> {escape(self._local(run.scheduled_for))}\n"
            f"📞 <b>Attempts:</b> {escape(tried)}\n"
            f"⏱ <b>Last try:</b> {escape(self._local(now))}\n\n"
            "Please check on them."
        )

    def _acknowledged_message(self, run: Run, now: datetime) -> str:
        recipient = self.config.recipient(run.recipient_id)
        return (
            "✅ <b>Medicine reminder confirmed</b>\n\n"
            f"<b>{escape(recipient.name)}</b> responded to "
            f"<b>{escape(run.schedule_id)}</b> at {escape(self._local(now))}."
        )
