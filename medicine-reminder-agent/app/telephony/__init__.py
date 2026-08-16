"""Telephony providers."""

from __future__ import annotations

from ..config import Config
from .base import TelephonyProvider, map_call_status
from .console_provider import ConsoleProvider

__all__ = ["TelephonyProvider", "ConsoleProvider", "build_provider", "map_call_status"]


def build_provider(config: Config) -> TelephonyProvider:
    """Instantiate the provider named by ``CALL_PROVIDER``."""
    if config.provider.name == "console":
        return ConsoleProvider()

    from .twilio_provider import TwilioProvider  # imported lazily: needs credentials

    return TwilioProvider(config.provider, config.call)
