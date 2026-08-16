"""Webhook tests — the path that decides whether someone actually responded."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import CallSettings, ProviderSettings
from app.db import Store
from app.engine import ReminderEngine
from app.models import CallOutcome, RunStatus
from app.telephony.console_provider import ConsoleProvider
from app.web import Services, create_app

from .conftest import FakeNotifier, ScriptedReader, make_config, make_llm

BASE_URL = "https://example.test"


@pytest.fixture
def client_and_store():
    config = make_config(
        call=CallSettings(confirmation_mode="dtmf", confirm_digit="1"),
        provider=ProviderSettings(
            name="console", public_base_url=BASE_URL, validate_signatures=False
        ),
    )
    store = Store(":memory:")
    reader = ScriptedReader()
    llm = make_llm(reader)
    engine = ReminderEngine(
        config,
        store,
        ConsoleProvider(outcome=CallOutcome.IN_PROGRESS, ring_seconds=0),
        FakeNotifier(),
        llm,
    )
    services = Services(config=config, store=store, engine=engine,
                        notifier=engine.notifier, llm=llm)
    app = create_app(services=services, run_scheduler=False)
    with TestClient(app) as client:
        yield client, store, engine, reader


async def _start_run(engine):
    run_id = await engine.trigger_schedule(engine.config.schedule("morning"))
    return run_id, engine.store.get_run(run_id).token


def _params(run_id: int, token: str, attempt: int = 1) -> dict[str, object]:
    return {"run_id": run_id, "attempt": attempt, "token": token}


@pytest.mark.asyncio
async def test_answer_webhook_returns_a_gather_with_the_reminder(client_and_store):
    client, _, engine, reader = client_and_store
    run_id, token = await _start_run(engine)

    response = client.post("/voice/answer", params=_params(run_id, token), data={})

    assert response.status_code == 200
    body = response.text
    assert "<Gather" in body
    assert "Please take your morning tablet." in body
    assert "Press 1 to confirm" in body
    assert f"{BASE_URL}/voice/gather" in body


@pytest.mark.asyncio
async def test_pressing_the_confirm_key_acknowledges_the_run(client_and_store):
    client, store, engine, reader = client_and_store
    run_id, token = await _start_run(engine)

    response = client.post(
        "/voice/gather", params=_params(run_id, token), data={"Digits": "1"}
    )

    assert response.status_code == 200
    assert "Thank you" in response.text
    assert store.get_run(run_id).status is RunStatus.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_wrong_key_does_not_acknowledge(client_and_store):
    client, store, engine, reader = client_and_store
    run_id, token = await _start_run(engine)

    client.post("/voice/gather", params=_params(run_id, token), data={"Digits": "7"})

    assert store.get_run(run_id).status is RunStatus.CALLING


@pytest.mark.asyncio
async def test_no_answer_status_callback_queues_the_retry(client_and_store):
    client, store, engine, reader = client_and_store
    run_id, token = await _start_run(engine)

    response = client.post(
        "/voice/status",
        params=_params(run_id, token),
        data={"CallStatus": "no-answer"},
    )

    assert response.status_code == 204
    run = store.get_run(run_id)
    assert run.status is RunStatus.WAITING_RETRY
    assert run.last_outcome is CallOutcome.NO_ANSWER


@pytest.mark.asyncio
async def test_voicemail_hangs_up_instead_of_reading_the_reminder(client_and_store):
    client, store, engine, reader = client_and_store
    run_id, token = await _start_run(engine)

    response = client.post(
        "/voice/answer",
        params=_params(run_id, token),
        data={"AnsweredBy": "machine_start"},
    )

    assert "<Gather" not in response.text
    assert "<Hangup/>" in response.text
    assert store.get_run(run_id).status is RunStatus.WAITING_RETRY


@pytest.mark.asyncio
async def test_webhooks_require_the_run_token(client_and_store):
    client, _, engine, reader = client_and_store
    run_id, _ = await _start_run(engine)

    for path in ("/voice/answer", "/voice/gather", "/voice/status"):
        response = client.post(path, params=_params(run_id, "not-the-token"), data={})
        assert response.status_code == 404, path


@pytest.mark.asyncio
async def test_status_endpoint_lists_runs(client_and_store):
    client, _, engine, reader = client_and_store
    run_id, _ = await _start_run(engine)

    payload = client.get("/status").json()

    assert payload["confirmation_mode"] == "dtmf"
    assert [run["id"] for run in payload["runs"]] == [run_id]


@pytest.fixture
def speech_client():
    """A client whose calls listen for speech, with a scripted model behind it."""
    config = make_config(
        call=CallSettings(confirmation_mode="speech", confirm_digit="1"),
        provider=ProviderSettings(
            name="console", public_base_url=BASE_URL, validate_signatures=False
        ),
    )
    store = Store(":memory:")
    reader = ScriptedReader()
    llm = make_llm(reader)
    engine = ReminderEngine(
        config, store,
        ConsoleProvider(outcome=CallOutcome.IN_PROGRESS, ring_seconds=0),
        FakeNotifier(), llm,
    )
    services = Services(config=config, store=store, engine=engine,
                        notifier=engine.notifier, llm=llm)
    with TestClient(create_app(services=services, run_scheduler=False)) as client:
        yield client, store, engine, reader


@pytest.mark.asyncio
async def test_speech_mode_asks_an_open_question_and_listens(speech_client):
    client, _, engine, _ = speech_client
    run_id, token = await _start_run(engine)

    body = client.post("/voice/answer", params=_params(run_id, token), data={}).text

    assert 'input="speech dtmf"' in body
    assert 'speechTimeout="auto"' in body
    assert "hints=" in body, "hints materially improve accented speech recognition"
    assert "Have you taken it?" in body
    assert "or press 1" in body, "the keypad stays available as a fallback"


@pytest.mark.asyncio
async def test_spoken_yes_acknowledges_and_speaks_the_model_reply(speech_client):
    client, store, engine, reader = speech_client
    run_id, token = await _start_run(engine)
    reader.script(intent="confirmed", spoken_reply="Wonderful, thank you Amma.")

    response = client.post(
        "/voice/gather", params=_params(run_id, token),
        data={"SpeechResult": "haan main le liya"},
    )

    assert "Wonderful, thank you Amma." in response.text
    assert store.get_run(run_id).status is RunStatus.ACKNOWLEDGED
    assert "haan main le liya" in reader.prompts[0]


@pytest.mark.asyncio
async def test_spoken_later_schedules_the_callback(speech_client):
    client, store, engine, reader = speech_client
    run_id, token = await _start_run(engine)
    reader.script(intent="snooze", snooze_minutes=25, spoken_reply="Okay, later then.")

    client.post(
        "/voice/gather", params=_params(run_id, token),
        data={"SpeechResult": "after lunch please"},
    )

    run = store.get_run(run_id)
    assert run.status is RunStatus.WAITING_RETRY
    assert run.snoozes == 1


@pytest.mark.asyncio
async def test_keypress_still_works_without_consulting_the_model(speech_client):
    client, store, engine, reader = speech_client
    run_id, token = await _start_run(engine)

    client.post("/voice/gather", params=_params(run_id, token), data={"Digits": "1"})

    assert store.get_run(run_id).status is RunStatus.ACKNOWLEDGED
    assert reader.prompts == [], "an unambiguous keypress needs no model"


@pytest.mark.asyncio
async def test_the_follow_up_question_can_be_asked_in_their_language(speech_client):
    client, _, engine, _ = speech_client
    # An English question after a Hindi reminder is where people hang up.
    engine.config.recipients["mom"] = replace(
        engine.config.recipient("mom"),
        language="hi-IN",
        confirm_prompt="Aapne dawai le li? Bata dijiye, ya {digit} dabaiye.",
    )
    run_id, token = await _start_run(engine)

    body = client.post("/voice/answer", params=_params(run_id, token), data={}).text

    assert "Aapne dawai le li?" in body
    assert "1 dabaiye" in body
    assert "Have you taken it?" not in body
