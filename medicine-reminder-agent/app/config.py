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
    #: Every sentence the call speaks that isn't the reminder itself. Override
    #: these in the recipient's own language — the voice pronounces whatever
    #: text it is given, so English defaults read by a Telugu voice come out as
    #: nonsense. Keys: confirm_prompt, thanks, no_reply, snooze_ack,
    #: refused_ack, unclear_ack. "{digit}" is substituted with the confirm key.
    phrases: dict[str, str] = field(default_factory=dict)

    def phrase(self, key: str, default: str) -> str:
        return self.phrases.get(key) or default


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
    # "Call me back in 20 minutes" — honoured this many times per dose before
    # the reminder falls back to the normal retry-then-escalate ladder.
    max_snoozes: int = 1
    speech_timeout: str = "auto"  # Twilio end-of-speech detection
    # What to do when they clearly spoke but the recogniser produced nothing
    # usable. "confirmed" trusts that answering and replying means they heard
    # the reminder; "missed" demands a clean transcript and retries otherwise.
    # Default is confirmed: for a recipient who cannot work a keypad, speech is
    # the only channel there is, and treating every garbled transcript as a
    # miss produces an alert on doses they actually took — which trains you to
    # ignore the alerts that matter.
    unclear_speech_counts_as: str = "confirmed"



#: base URL, default model, and the env vars each vendor conventionally uses.
PROVIDER_PRESETS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "gemini": (
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "gemini-2.5-flash-lite",
        ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    ),
    "openai": ("https://api.openai.com/v1", "gpt-4.1-nano", ("OPENAI_API_KEY",)),
    "groq": (
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        ("GROQ_API_KEY",),
    ),
    "xai": ("https://api.x.ai/v1", "grok-4.1-fast", ("XAI_API_KEY",)),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat", ("DEEPSEEK_API_KEY",)),
    "mistral": (
        "https://api.mistral.ai/v1",
        "mistral-small-latest",
        ("MISTRAL_API_KEY",),
    ),
    "anthropic": ("", "claude-haiku-4-5", ("ANTHROPIC_API_KEY",)),
    "custom": ("", "", ()),
}


@dataclass(frozen=True)
class LLMSettings:
    """Which model understands spoken replies, and how patient we are with it."""

    enabled: bool = True
    provider: str = "gemini"
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    # A person is holding a phone: give up and fall back rather than make
    # them wait. Two providers' worth of retry already fits inside this.
    timeout_seconds: float = 8.0
    max_tokens: int = 300
    default_snooze_minutes: int = 15
    max_snooze_minutes: int = 60


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
    # The master credential. Needed for webhook signature validation — Twilio
    # signs X-Twilio-Signature with it and an API key cannot verify that.
    auth_token: str = ""
    # Optional scoped credential, preferred for placing calls because it can be
    # revoked on its own without rotating the account's master token.
    api_key_sid: str = ""
    api_key_secret: str = ""

    @property
    def api_credentials(self) -> tuple[str, str]:
        """Username/password for Twilio's REST API."""
        if self.api_key_sid and self.api_key_secret:
            return self.api_key_sid, self.api_key_secret
        return self.account_sid, self.auth_token
    from_number: str = ""
    public_base_url: str = ""
    validate_signatures: bool = True
    machine_detection: bool = True


@dataclass
class Config:
    timezone: ZoneInfo
    call: CallSettings
    telegram: TelegramSettings
    llm: LLMSettings
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
            phrases={
                str(k): str(v)
                for k, v in (raw.get("phrases") or {}).items()
                if str(v).strip()
            },
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
    if mode not in {"speech", "dtmf", "answered"}:
        raise ConfigError(
            "call.confirmation_mode must be 'speech', 'dtmf' or 'answered'"
        )

    digit = str(raw.get("confirm_digit") or defaults.confirm_digit)
    if len(digit) != 1 or digit not in "0123456789*#":
        raise ConfigError("call.confirm_digit must be a single key (0-9, * or #)")

    unclear = str(
        raw.get("unclear_speech_counts_as") or defaults.unclear_speech_counts_as
    ).lower()
    if unclear not in {"confirmed", "missed"}:
        raise ConfigError(
            "call.unclear_speech_counts_as must be 'confirmed' or 'missed'"
        )

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
        max_snoozes=max(0, int(raw.get("max_snoozes", defaults.max_snoozes))),
        speech_timeout=str(raw.get("speech_timeout") or defaults.speech_timeout),
        unclear_speech_counts_as=unclear,
    )


def _load_llm_settings(raw: dict[str, Any]) -> LLMSettings:
    defaults = LLMSettings()
    enabled = _as_bool(raw.get("enabled"), defaults.enabled)

    provider = str(
        raw.get("provider") or os.environ.get("LLM_PROVIDER") or defaults.provider
    ).strip().lower()
    if provider not in PROVIDER_PRESETS:
        raise ConfigError(
            f"unknown llm.provider '{provider}'; choose one of "
            f"{sorted(PROVIDER_PRESETS)}"
        )
    preset_url, preset_model, key_vars = PROVIDER_PRESETS[provider]

    # An explicit LLM_API_KEY always wins, so one variable works for any vendor.
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    for var in key_vars:
        if api_key:
            break
        api_key = os.environ.get(var, "").strip()

    model = str(raw.get("model") or os.environ.get("LLM_MODEL") or preset_model).strip()
    base_url = str(
        raw.get("base_url") or os.environ.get("LLM_BASE_URL") or preset_url
    ).strip()

    if enabled and provider == "custom" and not base_url:
        raise ConfigError("llm.provider 'custom' needs llm.base_url (or LLM_BASE_URL)")
    if enabled and not model:
        raise ConfigError(f"llm.provider '{provider}' needs llm.model (or LLM_MODEL)")

    return LLMSettings(
        enabled=enabled,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(
            raw.get("timeout_seconds", defaults.timeout_seconds)
        ),
        max_tokens=int(raw.get("max_tokens", defaults.max_tokens)),
        default_snooze_minutes=int(
            raw.get("default_snooze_minutes", defaults.default_snooze_minutes)
        ),
        max_snooze_minutes=int(
            raw.get("max_snooze_minutes", defaults.max_snooze_minutes)
        ),
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
    llm = _load_llm_settings(raw.get("llm") or {})

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
        api_key_sid=os.environ.get("TWILIO_API_KEY_SID", "").strip(),
        api_key_secret=os.environ.get("TWILIO_API_KEY_SECRET", "").strip(),
        from_number=os.environ.get("TWILIO_FROM_NUMBER", "").strip(),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/"),
        validate_signatures=_as_bool(os.environ.get("VALIDATE_WEBHOOK_SIGNATURES"), True),
        machine_detection=_as_bool(os.environ.get("MACHINE_DETECTION"), True),
    )
    if provider.name not in {"twilio", "console"}:
        raise ConfigError(f"unsupported CALL_PROVIDER '{provider.name}'")
    if provider.name == "twilio":
        has_secret = provider.auth_token or (
            provider.api_key_sid and provider.api_key_secret
        )
        missing = [
            name
            for name, value in (
                ("TWILIO_ACCOUNT_SID", provider.account_sid),
                ("TWILIO_AUTH_TOKEN or TWILIO_API_KEY_SID/SECRET", has_secret),
                ("TWILIO_FROM_NUMBER", provider.from_number),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"missing environment variables: {', '.join(missing)}")
    if call.confirmation_mode in {"speech", "dtmf"} and not provider.public_base_url:
        raise ConfigError(
            f"call.confirmation_mode: {call.confirmation_mode} needs PUBLIC_BASE_URL "
            "so the reply can reach this app. Expose it (ngrok/cloudflared/a server) "
            "or use confirmation_mode: answered"
        )

    return Config(
        timezone=tz,
        call=call,
        telegram=telegram,
        llm=llm,
        provider=provider,
        recipients=recipients,
        schedules=schedules,
        database_path=str(
            raw.get("database_path") or os.environ.get("DATABASE_PATH") or "reminders.db"
        ),
    )
