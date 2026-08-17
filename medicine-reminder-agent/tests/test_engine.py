"""Behavioural tests for the call -> retry -> escalate state machine."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.config import CallSettings
from app.llm import ReminderReading
from app.models import CallOutcome, ReplyIntent, RunStatus, utcnow

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


def reading(**fields) -> ReminderReading:
    """A model reading with sensible defaults, for driving the engine."""
    payload = {
        "intent": "confirmed",
        "snooze_minutes": 0,
        "spoken_reply": "Thank you.",
        "summary": "said yes",
    }
    payload.update(fields)
    return ReminderReading(**payload)


SPEECH = make_config(call=CallSettings(confirmation_mode="speech", max_snoozes=1))


@pytest.mark.asyncio
async def test_speaking_a_confirmation_closes_the_reminder(harness):
    engine, store, provider, notifier = harness(config=make_config(
        call=CallSettings(confirmation_mode="speech")
    ))
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    intent = await engine.record_reply(
        run_id, 1, reading(summary='Amma said "already took it"'), now=now
    )

    run = store.get_run(run_id)
    assert intent is ReplyIntent.CONFIRMED
    assert run.status is RunStatus.ACKNOWLEDGED
    assert run.next_action_at is None
    await engine.tick(now + timedelta(seconds=600))
    assert len(provider.placed) == 1
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_snooze_calls_back_later_and_restarts_the_ladder(harness):
    engine, store, provider, notifier = harness(config=SPEECH)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_reply(
        run_id, 1, reading(intent="snooze", snooze_minutes=20), now=now
    )

    run = store.get_run(run_id)
    assert run.status is RunStatus.WAITING_RETRY
    assert run.next_action_at == now + timedelta(minutes=20)
    assert run.snoozes == 1

    # Nothing happens until the snooze elapses.
    await engine.tick(now + timedelta(minutes=19))
    assert len(provider.placed) == 1

    later = now + timedelta(minutes=20)
    await engine.tick(later)
    assert len(provider.placed) == 2

    # The full two-call ladder runs again from here, rather than escalating
    # immediately because attempt 2 was already used before the snooze.
    await engine.record_outcome(run_id, 2, CallOutcome.NO_ANSWER, now=later)
    assert store.get_run(run_id).status is RunStatus.WAITING_RETRY
    assert notifier.messages == []

    final = later + timedelta(seconds=300)
    await engine.tick(final)
    await engine.record_outcome(run_id, 3, CallOutcome.NO_ANSWER, now=final)
    assert store.get_run(run_id).status is RunStatus.ESCALATED
    assert len(provider.placed) == 3


@pytest.mark.asyncio
async def test_a_second_snooze_is_not_granted(harness):
    engine, store, provider, notifier = harness(config=SPEECH)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_reply(run_id, 1, reading(intent="snooze", snooze_minutes=10),
                              now=now)
    later = now + timedelta(minutes=10)
    await engine.tick(later)

    # Asking again is treated as a missed call, not another postponement.
    await engine.record_reply(run_id, 2, reading(intent="snooze", snooze_minutes=10),
                              now=later)

    run = store.get_run(run_id)
    assert run.snoozes == 1
    assert run.status is RunStatus.WAITING_RETRY
    assert run.next_action_at == later + timedelta(seconds=300)


@pytest.mark.asyncio
async def test_a_refusal_escalates_immediately(harness):
    engine, store, provider, notifier = harness(config=SPEECH)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_reply(
        run_id, 1,
        reading(intent="refused", summary='Amma said "I do not want it today"'),
        now=now,
    )

    assert store.get_run(run_id).status is RunStatus.ESCALATED
    assert len(provider.placed) == 1, "calling back will not change their mind"
    assert len(notifier.messages) == 1
    assert "declined to take it" in notifier.messages[0]
    assert "I do not want it today" in notifier.messages[0]


@pytest.mark.asyncio
async def test_speaking_counts_even_when_the_transcript_is_unusable(harness):
    """The default: they answered and spoke, so the dose is not chased again.

    Someone who cannot work a keypad has speech as their only channel. Demanding
    a clean transcript from an elderly speaker would raise an alert on doses
    they took and confirmed out loud, which is how alerts get ignored.
    """
    engine, store, provider, notifier = harness(config=SPEECH)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_reply(run_id, 1, reading(intent="unclear"), now=now)

    assert store.get_run(run_id).status is RunStatus.ACKNOWLEDGED
    await engine.tick(now + timedelta(seconds=600))
    assert len(provider.placed) == 1
    assert notifier.messages == []


@pytest.mark.asyncio
async def test_unclear_can_be_made_strict(harness):
    engine, store, _, _ = harness(config=make_config(
        call=CallSettings(confirmation_mode="speech", unclear_speech_counts_as="missed")
    ))
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_reply(run_id, 1, reading(intent="unclear"), now=now)

    assert store.get_run(run_id).status is RunStatus.WAITING_RETRY


@pytest.mark.asyncio
async def test_silence_still_counts_as_a_miss(harness):
    """Nothing heard at all never reaches record_reply — it is a plain miss."""
    engine, store, _, _ = harness(config=SPEECH)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_outcome(run_id, 1, CallOutcome.ANSWERED_HUMAN, now=now)

    assert store.get_run(run_id).status is RunStatus.WAITING_RETRY


@pytest.mark.asyncio
async def test_someone_else_answering_is_treated_as_a_miss(harness):
    engine, store, _, _ = harness(config=SPEECH)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_reply(run_id, 1, reading(intent="wrong_person"), now=now)

    assert store.get_run(run_id).status is RunStatus.WAITING_RETRY


@pytest.mark.asyncio
async def test_alert_keeps_quotes_readable_but_escapes_markup(harness):
    engine, _, _, notifier = harness(config=SPEECH)
    now = utcnow()

    run_id = await engine.trigger_schedule(engine.config.schedule("morning"), now=now)
    await engine.record_reply(
        run_id, 1,
        reading(intent="refused", summary='Amma said "I don\'t want it" <script>'),
        now=now,
    )

    alert = notifier.messages[0]
    # Telegram decodes &lt; &gt; &amp; but not &#x27;, so quoting speech would
    # fill the alert with visible entity noise.
    assert '"I don\'t want it"' in alert
    assert "&#x27;" not in alert and "&quot;" not in alert
    # Markup in a transcript still must not reach Telegram as live HTML.
    assert "&lt;script&gt;" in alert
