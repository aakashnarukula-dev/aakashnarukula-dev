"""FastAPI app: Twilio webhooks, a small status API, and the app lifecycle."""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Query, Request, Response

from .config import Config, load_config
from .db import Store
from .engine import ReminderEngine
from .models import Run, RunStatus
from .notifier import TelegramNotifier
from .scheduler import ReminderScheduler
from .telephony import build_provider, map_call_status
from .telephony.base import MACHINE_ANSWERS
from .voice import NO_RESPONSE_LINE, THANK_YOU_LINE, closing_twiml, reminder_twiml

log = logging.getLogger(__name__)

XML = "application/xml"


@dataclass
class Services:
    config: Config
    store: Store
    engine: ReminderEngine
    notifier: TelegramNotifier
    scheduler: ReminderScheduler | None = None


def build_services(config: Config | None = None) -> Services:
    config = config or load_config()
    store = Store(config.database_path)
    provider = build_provider(config)
    notifier = TelegramNotifier(config.telegram)
    engine = ReminderEngine(config, store, provider, notifier)
    return Services(config=config, store=store, engine=engine, notifier=notifier)


def _services(request: Request) -> Services:
    return request.app.state.services


async def _verify_twilio_signature(request: Request, form: dict[str, Any]) -> None:
    """Reject webhook calls that Twilio did not sign."""
    config: Config = _services(request).config
    if not (config.provider.validate_signatures and config.provider.name == "twilio"):
        return

    from twilio.request_validator import RequestValidator

    signature = request.headers.get("X-Twilio-Signature", "")
    # Rebuild the URL Twilio signed: the public one, not what a proxy handed us.
    url = f"{config.provider.public_base_url}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"

    validator = RequestValidator(config.provider.auth_token)
    if not validator.validate(url, form, signature):
        log.warning("rejected webhook with a bad Twilio signature: %s", url)
        raise HTTPException(status_code=403, detail="invalid signature")


def _authorised_run(services: Services, run_id: int, token: str) -> Run:
    run = services.store.get_run(run_id)
    if run is None or not secrets.compare_digest(run.token, token):
        raise HTTPException(status_code=404, detail="unknown run")
    return run


def _require_admin(request: Request) -> None:
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    if not expected:
        return
    supplied = request.headers.get("X-Admin-Token", "")
    if not secrets.compare_digest(expected, supplied):
        raise HTTPException(status_code=401, detail="unauthorised")


@asynccontextmanager
async def lifespan(app: FastAPI):
    services: Services = getattr(app.state, "services", None) or build_services()
    app.state.services = services
    if getattr(app.state, "run_scheduler", True):
        services.scheduler = ReminderScheduler(services.config, services.engine)
        services.scheduler.start()
    try:
        yield
    finally:
        if services.scheduler is not None:
            services.scheduler.shutdown()
        await services.notifier.aclose()
        await services.engine.provider.aclose()
        services.store.close()


def create_app(services: Services | None = None, run_scheduler: bool = True) -> FastAPI:
    app = FastAPI(title="Medicine Reminder Agent", lifespan=lifespan)
    if services is not None:
        app.state.services = services
    app.state.run_scheduler = run_scheduler

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/voice/answer")
    async def voice_answer(
        request: Request,
        run_id: int = Query(...),
        attempt: int = Query(...),
        token: str = Query(...),
    ) -> Response:
        """Twilio fetches the call script here the moment the call connects."""
        services = _services(request)
        form = dict(await request.form())
        await _verify_twilio_signature(request, form)
        run = _authorised_run(services, run_id, token)

        recipient = services.config.recipient(run.recipient_id)
        schedule = services.config.schedule(run.schedule_id)
        answered_by = str(form.get("AnsweredBy", "") or "").lower()

        if answered_by in MACHINE_ANSWERS:
            # Voicemail never counts as a response — hang up and let the retry run.
            log.info("run %s attempt %s: answering machine detected", run_id, attempt)
            await services.engine.record_outcome(
                run_id,
                attempt,
                map_call_status("completed", answered_by),
                detail="answering machine",
            )
            return Response(
                content=closing_twiml("", recipient.voice, recipient.language),
                media_type=XML,
            )

        action_url = None
        confirm_digit = None
        if services.config.call.confirmation_mode == "dtmf":
            query = urlencode({"run_id": run_id, "attempt": attempt, "token": token})
            action_url = (
                f"{services.config.provider.public_base_url}/voice/gather?{query}"
            )
            confirm_digit = services.config.call.confirm_digit

        return Response(
            content=reminder_twiml(
                message=schedule.message,
                voice=recipient.voice,
                language=recipient.language,
                confirm_digit=confirm_digit,
                gather_timeout=services.config.call.gather_timeout_seconds,
                action_url=action_url,
            ),
            media_type=XML,
        )

    @app.post("/voice/gather")
    async def voice_gather(
        request: Request,
        run_id: int = Query(...),
        attempt: int = Query(...),
        token: str = Query(...),
        Digits: str = Form(default=""),
    ) -> Response:
        """Where the keypress lands — this is what proves someone heard the reminder."""
        services = _services(request)
        form = dict(await request.form())
        await _verify_twilio_signature(request, form)
        run = _authorised_run(services, run_id, token)
        recipient = services.config.recipient(run.recipient_id)

        if Digits.strip() == services.config.call.confirm_digit:
            await services.engine.record_acknowledgement(run_id, attempt)
            spoken = THANK_YOU_LINE
        else:
            # No key (or the wrong one): the status callback will trigger the retry.
            spoken = NO_RESPONSE_LINE

        return Response(
            content=closing_twiml(spoken, recipient.voice, recipient.language),
            media_type=XML,
        )

    @app.post("/voice/status")
    async def voice_status(
        request: Request,
        run_id: int = Query(...),
        attempt: int = Query(...),
        token: str = Query(...),
    ) -> Response:
        """Final call status from Twilio — drives the retry / escalation decision."""
        services = _services(request)
        form = dict(await request.form())
        await _verify_twilio_signature(request, form)
        _authorised_run(services, run_id, token)

        outcome = map_call_status(
            str(form.get("CallStatus", "")), str(form.get("AnsweredBy", "") or "")
        )
        await services.engine.record_outcome(
            run_id, attempt, outcome, detail=str(form.get("CallStatus", ""))
        )
        return Response(status_code=204)

    @app.get("/status")
    async def status(request: Request, limit: int = 25) -> dict[str, Any]:
        _require_admin(request)
        services = _services(request)
        return {
            "timezone": str(services.config.timezone),
            "provider": services.config.provider.name,
            "confirmation_mode": services.config.call.confirmation_mode,
            "next_runs": (
                services.scheduler.next_run_times() if services.scheduler else {}
            ),
            "runs": [_run_json(run) for run in services.store.recent_runs(limit)],
        }

    @app.post("/admin/trigger/{schedule_id}")
    async def trigger(request: Request, schedule_id: str) -> dict[str, Any]:
        """Fire a reminder right now — handy for verifying the whole chain."""
        _require_admin(request)
        services = _services(request)
        try:
            schedule = services.config.schedule(schedule_id)
        except Exception:
            raise HTTPException(status_code=404, detail="unknown schedule") from None
        run_id = await services.engine.trigger_schedule(schedule)
        return {"run_id": run_id, "started": run_id is not None}

    return app


def _run_json(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "schedule": run.schedule_id,
        "recipient": run.recipient_id,
        "scheduled_for": run.scheduled_for.isoformat(),
        "status": RunStatus(run.status).value,
        "attempt": run.attempt,
        "last_outcome": run.last_outcome.value if run.last_outcome else None,
        "next_action_at": (
            run.next_action_at.isoformat() if run.next_action_at else None
        ),
    }


app_factory = create_app
