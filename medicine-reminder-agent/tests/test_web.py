"""Webhook tests — the path that decides whether someone actually responded."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import CallSettings, ProviderSettings
from app.db import Store
from app.engine import ReminderEngine
from app.models import CallOutcome, RunStatus
from app.telephony.console_provider import ConsoleProvider
from app.web import Services, create_app

from .conftest import FakeNotifier, make_config

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
    engine = ReminderEngine(
        config,
        store,
        ConsoleProvider(outcome=CallOutcome.IN_PROGRESS, ring_seconds=0),
        FakeNotifier(),
    )
    services = Services(config=config, store=store, engine=engine,
                        notifier=engine.notifier)
    app = create_app(services=services, run_scheduler=False)
    with TestClient(app) as client:
        yield client, store, engine


async def _start_run(engine):
    run_id = await engine.trigger_schedule(engine.config.schedule("morning"))
    return run_id, engine.store.get_run(run_id).token


def _params(run_id: int, token: str, attempt: int = 1) -> dict[str, object]:
    return {"run_id": run_id, "attempt": attempt, "token": token}


@pytest.mark.asyncio
async def test_answer_webhook_returns_a_gather_with_the_reminder(client_and_store):
    client, _, engine = client_and_store
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
    client, store, engine = client_and_store
    run_id, token = await _start_run(engine)

    response = client.post(
        "/voice/gather", params=_params(run_id, token), data={"Digits": "1"}
    )

    assert response.status_code == 200
    assert "Thank you" in response.text
    assert store.get_run(run_id).status is RunStatus.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_wrong_key_does_not_acknowledge(client_and_store):
    client, store, engine = client_and_store
    run_id, token = await _start_run(engine)

    client.post("/voice/gather", params=_params(run_id, token), data={"Digits": "7"})

    assert store.get_run(run_id).status is RunStatus.CALLING


@pytest.mark.asyncio
async def test_no_answer_status_callback_queues_the_retry(client_and_store):
    client, store, engine = client_and_store
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
    client, store, engine = client_and_store
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
    client, _, engine = client_and_store
    run_id, _ = await _start_run(engine)

    for path in ("/voice/answer", "/voice/gather", "/voice/status"):
        response = client.post(path, params=_params(run_id, "not-the-token"), data={})
        assert response.status_code == 404, path


@pytest.mark.asyncio
async def test_status_endpoint_lists_runs(client_and_store):
    client, _, engine = client_and_store
    run_id, _ = await _start_run(engine)

    payload = client.get("/status").json()

    assert payload["confirmation_mode"] == "dtmf"
    assert [run["id"] for run in payload["runs"]] == [run_id]
