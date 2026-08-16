"""Twilio-backed voice calling."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlencode

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from ..config import CallSettings, ProviderSettings
from ..models import CallHandle, CallOutcome, CallRequest
from ..voice import reminder_twiml
from .base import map_call_status

log = logging.getLogger(__name__)


class TwilioProvider:
    """Places reminder calls through Twilio Programmable Voice."""

    name = "twilio"

    def __init__(self, settings: ProviderSettings, call_settings: CallSettings) -> None:
        self._settings = settings
        self._call = call_settings
        self._client = Client(settings.account_sid, settings.auth_token)

    def _callback_url(self, path: str, request: CallRequest) -> str:
        query = urlencode(
            {
                "run_id": request.run_id,
                "attempt": request.attempt,
                "token": request.token,
            }
        )
        return f"{self._settings.public_base_url}{path}?{query}"

    def _build_params(self, request: CallRequest) -> dict[str, object]:
        params: dict[str, object] = {
            "to": request.to,
            "from_": request.from_ or self._settings.from_number,
            "timeout": request.ring_timeout,
        }

        if self._settings.public_base_url:
            # Webhook mode: Twilio fetches the script from us, so a keypress can be
            # reported back and the call outcome arrives as a status callback.
            params["url"] = self._callback_url("/voice/answer", request)
            params["method"] = "POST"
            params["status_callback"] = self._callback_url("/voice/status", request)
            params["status_callback_method"] = "POST"
            params["status_callback_event"] = ["completed"]
        else:
            # No public URL: hand Twilio the whole script up front and poll for the
            # result. Only "answered" confirmation is possible in this mode.
            params["twiml"] = reminder_twiml(
                message=request.message,
                voice=request.voice,
                language=request.language,
            )

        if self._settings.machine_detection:
            params["machine_detection"] = "Enable"

        return params

    async def place_call(self, request: CallRequest) -> CallHandle:
        params = self._build_params(request)
        try:
            call = await asyncio.to_thread(self._client.calls.create, **params)
        except TwilioRestException as exc:
            raise RuntimeError(
                f"Twilio rejected the call to {request.to}: {exc.msg} (code {exc.code})"
            ) from exc
        log.info("placed twilio call %s to %s (attempt %s)", call.sid, request.to,
                 request.attempt)
        return CallHandle(provider_call_id=call.sid)

    async def fetch_outcome(self, provider_call_id: str) -> CallOutcome:
        try:
            call = await asyncio.to_thread(self._client.calls(provider_call_id).fetch)
        except TwilioRestException as exc:
            log.warning("could not fetch call %s: %s", provider_call_id, exc.msg)
            return CallOutcome.IN_PROGRESS
        return map_call_status(call.status, getattr(call, "answered_by", None))

    async def aclose(self) -> None:
        return None
