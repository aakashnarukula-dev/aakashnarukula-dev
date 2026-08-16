"""Behavioural tests for the call -> retry -> escalate state machine."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import CallSettings
from app.models import CallOutcome, RunStatus, utcnow

from .conftest import FakeNotifier, make_config


@pytest.mark.asyncio
async def test_unanswered_call_schedules_a_retry_five_minutes_later(harness):
    engine, store, provider, notifier = harness()
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_outcome(run_id, 1, CallOutcome.NO_ANSWER, now=now)

    run = store.get_run(run_id)
    assert run.status is RunStatus.WAITING_RETRY
    assert run.next_action_at == now + timedelta(seconds=300)
    assert len(provider.placed) == 1
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_second_miss_escalates_to_telegram(harness):
    engine, store, provider, notifier = harness()
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_outcome(run_id, 1, CallOutcome.NO_ANSWER, now=now)

    # Nothing happens until the retry timer elapses.
    await engine.tick(now + timedelta(seconds=299))
    assert len(provider.placed) == 1

    later = now + timedelta(seconds=300)
    await engine.tick(later)
    assert len(provider.placed) == 2
    assert store.get_run(run_id).attempt == 2

    await engine.record_outcome(run_id, 2, CallOutcome.NO_ANSWER, now=later)

    run = store.get_run(run_id)
    assert run.status is RunStatus.ESCALATED
    assert len(notifier.messages) == 1
    assert "Missed medicine reminder" in notifier.messages[0]
    assert "Amma" in notifier.messages[0]
    assert len(provider.placed) == 2, "must not call a third time"


@pytest.mark.asyncio
async def test_keypress_acknowledges_and_cancels_the_retry(harness):
    engine, store, provider, notifier = harness()
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    assert await engine.record_acknowledgement(run_id, 1, now=now)

    run = store.get_run(run_id)
    assert run.status is RunStatus.ACKNOWLEDGED
    assert run.next_action_at is None

    # A late status callback for the same call must not undo the acknowledgement.
    await engine.record_outcome(run_id, 1, CallOutcome.ANSWERED_HUMAN, now=now)
    await engine.tick(now + timedelta(seconds=600))

    assert store.get_run(run_id).status is RunStatus.ACKNOWLEDGED
    assert len(provider.placed) == 1
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_answering_the_call_is_not_a_response_in_dtmf_mode(harness):
    engine, store, _, _ = harness(
        config=make_config(call=CallSettings(confirmation_mode="dtmf"))
    )
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_outcome(run_id, 1, CallOutcome.ANSWERED_HUMAN, now=now)

    assert store.get_run(run_id).status is RunStatus.WAITING_RETRY


@pytest.mark.asyncio
async def test_answering_the_call_is_enough_in_answered_mode(harness):
    engine, store, _, _ = harness(
        config=make_config(call=CallSettings(confirmation_mode="answered"))
    )
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_outcome(run_id, 1, CallOutcome.ANSWERED_HUMAN, now=now)

    assert store.get_run(run_id).status is RunStatus.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_voicemail_never_counts_as_a_response(harness):
    engine, store, _, _ = harness(
        config=make_config(call=CallSettings(confirmation_mode="answered"))
    )
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_outcome(run_id, 1, CallOutcome.ANSWERED_MACHINE, now=now)

    assert store.get_run(run_id).status is RunStatus.WAITING_RETRY


@pytest.mark.asyncio
async def test_duplicate_trigger_for_the_same_dose_is_ignored(harness):
    engine, _, provider, _ = harness()
    now = utcnow()
    schedule = engine.config.schedule("morning")

    first = await engine.trigger_schedule(schedule, scheduled_for=now, now=now)
    second = await engine.trigger_schedule(schedule, scheduled_for=now, now=now)

    assert first is not None and second is None
    assert len(provider.placed) == 1


@pytest.mark.asyncio
async def test_repeated_webhooks_do_not_double_schedule(harness):
    engine, store, provider, notifier = harness()
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    for _ in range(3):
        await engine.record_outcome(run_id, 1, CallOutcome.NO_ANSWER, now=now)

    assert store.get_run(run_id).attempt == 1
    await engine.tick(now + timedelta(seconds=300))
    assert len(provider.placed) == 2
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_a_call_that_cannot_be_placed_counts_as_missed(harness):
    engine, store, provider, notifier = harness()
    now = utcnow()

    async def explode(request):
        raise RuntimeError("Twilio rejected the call")

    provider.place_call = explode

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    assert store.get_run(run_id).status is RunStatus.WAITING_RETRY

    await engine.tick(now + timedelta(seconds=300))
    run = store.get_run(run_id)
    assert run.status is RunStatus.ESCALATED
    assert len(notifier.messages) == 1


@pytest.mark.asyncio
async def test_failed_telegram_alert_is_retried(harness):
    notifier = FakeNotifier(succeed=False)
    engine, store, _, _ = harness(notifier=notifier)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_outcome(run_id, 1, CallOutcome.NO_ANSWER, now=now)
    later = now + timedelta(seconds=300)
    await engine.tick(later)
    await engine.record_outcome(run_id, 2, CallOutcome.NO_ANSWER, now=later)

    assert store.get_run(run_id).status is RunStatus.FAILED
    assert len(notifier.messages) == 1

    notifier.succeed = True
    await engine.tick(later + timedelta(seconds=60))

    assert store.get_run(run_id).status is RunStatus.ESCALATED
    assert len(notifier.messages) == 2


@pytest.mark.asyncio
async def test_polling_resolves_calls_when_no_webhook_arrives(harness):
    engine, store, _, notifier = harness(outcome=CallOutcome.BUSY)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    assert store.get_run(run_id).status is RunStatus.CALLING

    await engine.tick(now)  # polling mode: no public base url configured

    run = store.get_run(run_id)
    assert run.status is RunStatus.WAITING_RETRY
    assert run.last_outcome is CallOutcome.BUSY


@pytest.mark.asyncio
async def test_three_attempts_when_configured(harness):
    engine, store, provider, notifier = harness(
        config=make_config(call=CallSettings(max_attempts=3, retry_delay_seconds=60))
    )
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    for attempt in (1, 2, 3):
        await engine.record_outcome(run_id, attempt, CallOutcome.NO_ANSWER, now=now)
        now += timedelta(seconds=60)
        await engine.tick(now)

    assert len(provider.placed) == 3
    assert store.get_run(run_id).status is RunStatus.ESCALATED


@pytest.mark.asyncio
async def test_run_closes_out_when_telegram_is_switched_off(harness):
    notifier = FakeNotifier()
    notifier.enabled = False
    engine, store, _, _ = harness(notifier=notifier)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_outcome(run_id, 1, CallOutcome.NO_ANSWER, now=now)
    later = now + timedelta(seconds=300)
    await engine.tick(later)
    await engine.record_outcome(run_id, 2, CallOutcome.NO_ANSWER, now=later)

    run = store.get_run(run_id)
    assert run.status is RunStatus.ESCALATED
    assert run.next_action_at is None
    assert notifier.messages == []
