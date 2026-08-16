"""Configuration: recipients + schedules from YAML, secrets from the environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_TIME_PATTERN = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_PHONE_PATTERN = re.compile(r"^\+[1-9]\d{7,14}$")

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class ConfigError(Exception):
    """Raised for any human-fixable problem in config.yaml or the environment."""


@dataclass(frozen=True)
class Recipient:
    id: str
    name: str
    phone: str
    voice: str = "Polly.Joanna"
    language: str = "en-US"


@dataclass(frozen=True)
class Schedule:
    id: str
    recipient_id: str
    message: str
    cron: str  # 5-field crontab expression, evaluated in the configured timezone
    enabled: bool = True


@dataclass(frozen=True)
class CallSettings:
    max_attempts: int = 2
    retry_delay_seconds: int = 300
    ring_timeout_seconds: int = 30
    # "dtmf": they must press a key to count as responding (needs a public webhook URL).
    # "answered": picking up is enough (works without a public URL).
    confirmation_mode: str = "dtmf"
    confirm_digit: str = "1"
    gather_timeout_seconds: int = 8
    # Safety net: if no webhook ever resolves a call, give up on it after this long.
    stale_call_seconds: int = 180


@dataclass(frozen=True)
class TelegramSettings:
    enabled: bool = True
    bot_token: str = ""
    chat_id: str = ""
    notify_on_acknowledged: bool = False


@dataclass(frozen=True)
class ProviderSettings:
    name: str = "twilio"
    account_sid: str = ""
    auth_token: str = ""
    from_number: str = ""
    public_base_url: str = ""
    validate_signatures: bool = True
    machine_detection: bool = True


@dataclass
class Config:
    timezone: ZoneInfo
    call: CallSettings
    telegram: TelegramSettings
    provider: ProviderSettings
    recipients: dict[str, Recipient]
    schedules: list[Schedule] = field(default_factory=list)
    database_path: str = "reminders.db"

    def recipient(self, recipient_id: str) -> Recipient:
        try:
            return self.recipients[recipient_id]
        except KeyError:  # pragma: no cover - guarded at load time
            raise ConfigError(f"unknown recipient '{recipient_id}'") from None

    def schedule(self, schedule_id: str) -> Schedule:
        for schedule in self.schedules:
            if schedule.id == schedule_id:
                return schedule
        raise ConfigError(f"unknown schedule '{schedule_id}'")


def _expand_env(node: Any) -> Any:
    """Recursively expand ${VAR} / ${VAR:-default} so secrets stay out of the YAML."""
    if isinstance(node, dict):
        return {key: _expand_env(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_expand_env(value) for value in node]
    if isinstance(node, str):
        def replace(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), match.group(2) or "")

        return _ENV_PATTERN.sub(replace, node)
    return node


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _cron_from_schedule(raw: dict[str, Any], schedule_id: str) -> str:
    """Accept either a raw `cron:` expression or the friendlier `at:` + `days:` pair."""
    cron = raw.get("cron")
    if cron:
        if len(str(cron).split()) != 5:
            raise ConfigError(
                f"schedule '{schedule_id}': cron must have 5 fields, got '{cron}'"
            )
        return str(cron)

    at = str(raw.get("at", "")).strip()
    match = _TIME_PATTERN.match(at)
    if not match:
        raise ConfigError(
            f"schedule '{schedule_id}': needs `at: \"HH:MM\"` (24h) or a `cron:` expression"
        )
    hour, minute = int(match.group(1)), int(match.group(2))

    days = raw.get("days") or DAY_NAMES
    if isinstance(days, str):
        days = [part.strip() for part in days.split(",") if part.strip()]
    normalised = [str(day).strip().lower()[:3] for day in days]
    unknown = [day for day in normalised if day not in DAY_NAMES]
    if unknown:
        raise ConfigError(
            f"schedule '{schedule_id}': unknown day(s) {unknown}; use {DAY_NAMES}"
        )
    return f"{minute} {hour} * * {','.join(normalised)}"


def _load_recipients(raw_list: Any) -> dict[str, Recipient]:
    if not isinstance(raw_list, list) or not raw_list:
        raise ConfigError("config must define at least one entry under `recipients:`")

    recipients: dict[str, Recipient] = {}
    for index, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            raise ConfigError(f"recipients[{index}] must be a mapping")
        recipient_id = str(raw.get("id", "")).strip()
        if not recipient_id:
            raise ConfigError(f"recipients[{index}] is missing `id`")
        if recipient_id in recipients:
            raise ConfigError(f"duplicate recipient id '{recipient_id}'")

        phone = str(raw.get("phone", "")).strip()
        if not _PHONE_PATTERN.match(phone):
            raise ConfigError(
                f"recipient '{recipient_id}': phone must be E.164, e.g. +919876543210 "
                f"(got '{phone}')"
            )

        recipients[recipient_id] = Recipient(
            id=recipient_id,
            name=str(raw.get("name") or recipient_id),
            phone=phone,
            voice=str(raw.get("voice") or "Polly.Joanna"),
            language=str(raw.get("language") or "en-US"),
        )
    return recipients


def _load_schedules(raw_list: Any, recipients: dict[str, Recipient]) -> list[Schedule]:
    if not isinstance(raw_list, list) or not raw_list:
        raise ConfigError("config must define at least one entry under `schedules:`")

    schedules: list[Schedule] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            raise ConfigError(f"schedules[{index}] must be a mapping")
        schedule_id = str(raw.get("id", "")).strip()
        if not schedule_id:
            raise ConfigError(f"schedules[{index}] is missing `id`")
        if schedule_id in seen:
            raise ConfigError(f"duplicate schedule id '{schedule_id}'")
        seen.add(schedule_id)

        recipient_id = str(raw.get("recipient", "")).strip()
        if recipient_id not in recipients:
            raise ConfigError(
                f"schedule '{schedule_id}': unknown recipient '{recipient_id}'"
            )

        message = str(raw.get("message", "")).strip()
        if not message:
            raise ConfigError(f"schedule '{schedule_id}': `message` is required")

        schedules.append(
            Schedule(
                id=schedule_id,
                recipient_id=recipient_id,
                message=message,
                cron=_cron_from_schedule(raw, schedule_id),
                enabled=_as_bool(raw.get("enabled"), True),
            )
        )
    return schedules


def _load_call_settings(raw: dict[str, Any]) -> CallSettings:
    defaults = CallSettings()
    mode = str(raw.get("confirmation_mode") or defaults.confirmation_mode).lower()
    if mode not in {"dtmf", "answered"}:
        raise ConfigError("call.confirmation_mode must be 'dtmf' or 'answered'")

    digit = str(raw.get("confirm_digit") or defaults.confirm_digit)
    if len(digit) != 1 or digit not in "0123456789*#":
        raise ConfigError("call.confirm_digit must be a single key (0-9, * or #)")

    max_attempts = int(raw.get("max_attempts", defaults.max_attempts))
    if max_attempts < 1:
        raise ConfigError("call.max_attempts must be at least 1")

    retry_delay = int(raw.get("retry_delay_seconds", defaults.retry_delay_seconds))
    if retry_delay < 0:
        raise ConfigError("call.retry_delay_seconds cannot be negative")

    return CallSettings(
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay,
        ring_timeout_seconds=int(
            raw.get("ring_timeout_seconds", defaults.ring_timeout_seconds)
        ),
        confirmation_mode=mode,
        confirm_digit=digit,
        gather_timeout_seconds=int(
            raw.get("gather_timeout_seconds", defaults.gather_timeout_seconds)
        ),
        stale_call_seconds=int(raw.get("stale_call_seconds", defaults.stale_call_seconds)),
    )


def load_config(path: str | Path | None = None) -> Config:
    """Read config.yaml (env-expanded) plus provider secrets from the environment."""
    config_path = Path(path or os.environ.get("CONFIG_PATH", "config.yaml"))
    if not config_path.exists():
        raise ConfigError(
            f"config file not found at {config_path} — copy config.example.yaml to "
            f"config.yaml and edit it"
        )

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")
    raw = _expand_env(raw)

    tz_name = str(raw.get("timezone") or os.environ.get("TZ") or "UTC")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"unknown timezone '{tz_name}'") from exc

    recipients = _load_recipients(raw.get("recipients"))
    schedules = _load_schedules(raw.get("schedules"), recipients)
    call = _load_call_settings(raw.get("call") or {})

    raw_telegram = raw.get("telegram") or {}
    telegram = TelegramSettings(
        enabled=_as_bool(raw_telegram.get("enabled"), True),
        bot_token=str(
            raw_telegram.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        ).strip(),
        chat_id=str(
            raw_telegram.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")
        ).strip(),
        notify_on_acknowledged=_as_bool(
            raw_telegram.get("notify_on_acknowledged"), False
        ),
    )
    if telegram.enabled and not (telegram.bot_token and telegram.chat_id):
        raise ConfigError(
            "Telegram is enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset. "
            "Set them in .env, or set telegram.enabled: false"
        )

    provider = ProviderSettings(
        name=os.environ.get("CALL_PROVIDER", "twilio").strip().lower(),
        account_sid=os.environ.get("TWILIO_ACCOUNT_SID", "").strip(),
        auth_token=os.environ.get("TWILIO_AUTH_TOKEN", "").strip(),
        from_number=os.environ.get("TWILIO_FROM_NUMBER", "").strip(),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        validate_signatures=_as_bool(os.environ.get("VALIDATE_WEBHOOK_SIGNATURES"), True),
        machine_detection=_as_bool(os.environ.get("MACHINE_DETECTION"), True),
    )
    if provider.name not in {"twilio", "console"}:
        raise ConfigError(f"unsupported CALL_PROVIDER '{provider.name}'")
    if provider.name == "twilio":
        missing = [
            name
            for name, value in (
                ("TWILIO_ACCOUNT_SID", provider.account_sid),
                ("TWILIO_AUTH_TOKEN", provider.auth_token),
                ("TWILIO_FROM_NUMBER", provider.from_number),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    if call.confirmation_mode == "dtmf" and not provider.public_base_url:
        raise ConfigError(
            "call.confirmation_mode: dtmf needs PUBLIC_BASE_URL so the phone keypad "
            "reply can reach this app. Expose it (ngrok/cloudflared/a server) or use "
            "confirmation_mode: answered"
        )

    return Config(
        timezone=tz,
        call=call,
        telegram=telegram,
        provider=provider,
        recipients=recipients,
        schedules=schedules,
        database_path=str(
            raw.get("database_path") or os.environ.get("DATABASE_PATH") or "reminders.db"
        ),
    )
