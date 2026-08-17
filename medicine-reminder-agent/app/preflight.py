"""Check every external dependency before the first real call.

A reminder call that fails at 08:00 tells you almost nothing about why. This
runs the same checks up front, in the order they would break, and names the fix.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from .config import Config
from .llm import build_llm

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str

    @property
    def mark(self) -> str:
        return {OK: "✔", WARN: "!", FAIL: "✘"}[self.status]


def _placeholder(value: str) -> bool:
    stubs = ("ACxxx", "your_", "123456789:AA", "+15551234567", "https://your-tunnel")
    return not value or value.startswith(stubs)


def check_telephony(config: Config) -> list[Check]:
    """Credentials valid, account funded, and the from-number usable."""
    provider = config.provider
    if provider.name != "twilio":
        return [Check("telephony", WARN, f"provider is '{provider.name}', not twilio")]

    username, password = provider.api_credentials
    if _placeholder(provider.account_sid) or _placeholder(password) or not password:
        return [Check("twilio credentials", FAIL,
                      "set TWILIO_ACCOUNT_SID plus either TWILIO_AUTH_TOKEN or "
                      "TWILIO_API_KEY_SID/TWILIO_API_KEY_SECRET in .env")]

    from twilio.base.exceptions import TwilioRestException
    from twilio.rest import Client

    client = Client(username, password, provider.account_sid)
    checks: list[Check] = []

    try:
        account = client.api.accounts(provider.account_sid).fetch()
    except TwilioRestException as exc:
        return [Check("twilio credentials", FAIL, f"rejected: {exc.msg}")]
    kind = "api key" if provider.api_key_sid else "auth token"
    checks.append(
        Check("twilio credentials", OK, f"account '{account.friendly_name}' via {kind}")
    )
    if provider.validate_signatures and not provider.auth_token:
        checks.append(Check(
            "webhook signatures", FAIL,
            "an API key cannot verify X-Twilio-Signature — set TWILIO_AUTH_TOKEN, "
            "or VALIDATE_WEBHOOK_SIGNATURES=false while testing locally",
        ))

    if account.type == "Trial":
        checks.append(Check(
            "twilio account", FAIL,
            "still a Trial account — phone numbers and outbound calls are "
            "blocked until you add funds",
        ))
    else:
        checks.append(Check("twilio account", OK, f"{account.type}, {account.status}"))

    try:
        numbers = client.incoming_phone_numbers.list(limit=20)
    except TwilioRestException as exc:
        return checks + [Check("from number", FAIL, f"could not list numbers: {exc.msg}")]

    if not numbers:
        return checks + [Check("from number", FAIL, "no phone numbers on the account")]

    owned = {n.phone_number: n for n in numbers}
    configured = provider.from_number
    if _placeholder(configured):
        checks.append(Check("from number", FAIL,
                            f"TWILIO_FROM_NUMBER unset — you own {', '.join(owned)}"))
    elif configured not in owned:
        checks.append(Check("from number", FAIL,
                            f"{configured} is not on this account (own: {', '.join(owned)})"))
    elif not owned[configured].capabilities.get("voice"):
        checks.append(Check("from number", FAIL, f"{configured} has no voice capability"))
    else:
        checks.append(Check("from number", OK, configured))
    return checks


def check_public_url(config: Config) -> Check:
    url = config.provider.public_base_url
    if config.call.confirmation_mode == "answered":
        return Check("public url", OK, "not needed in 'answered' mode")
    if _placeholder(url):
        return Check("public url", FAIL,
                     "PUBLIC_BASE_URL unset — start ngrok and paste the https URL")
    if not url.startswith("https://"):
        return Check("public url", FAIL, f"must be https, got {url}")
    return Check("public url", OK, url)


def check_recipients(config: Config) -> list[Check]:
    """Every scheduled recipient needs a voice the platform can actually speak."""
    checks = []
    for recipient in config.recipients.values():
        voice, lang = recipient.voice, recipient.language
        if lang.startswith("te") and voice.startswith("Polly."):
            checks.append(Check(
                f"voice for {recipient.name}", FAIL,
                f"{voice} cannot speak {lang} — Amazon Polly has no Telugu. "
                f"Use Google.te-IN-Standard-Female or Google.te-IN-Chirp3-HD-Kore",
            ))
        elif not recipient.phrases:
            checks.append(Check(
                f"voice for {recipient.name}", WARN,
                f"{voice}/{lang}, but no `phrases` set — the call will speak "
                f"English for everything except the reminder itself",
            ))
        else:
            checks.append(Check(f"voice for {recipient.name}", OK, f"{voice} / {lang}"))
    return checks


async def check_llm(config: Config) -> Check:
    if not config.llm.enabled:
        return Check("reply model", WARN, "disabled — keyword matching only")
    if not config.llm.api_key:
        return Check("reply model", WARN,
                     "no API key — falls back to keyword matching")
    llm = build_llm(config.llm)
    try:
        reading = await llm.read_reply(
            recipient=next(iter(config.recipients.values())),
            schedule=config.schedules[0],
            transcript="అవును వేసుకున్నాను",
        )
    except Exception as exc:
        return Check("reply model", FAIL, f"{config.llm.model}: {exc}")
    finally:
        await llm.aclose()

    if reading.intent != "confirmed":
        return Check("reply model", WARN,
                     f"{llm.describe} read a plain Telugu yes as '{reading.intent}'")
    return Check("reply model", OK, f"{llm.describe} understood a Telugu yes")


async def check_telegram(config: Config) -> Check:
    settings = config.telegram
    if not settings.enabled:
        return Check("telegram", WARN, "disabled — you get no alerts")
    if _placeholder(settings.bot_token) or not settings.chat_id:
        return Check("telegram", FAIL, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID unset")
    async with httpx.AsyncClient(timeout=10) as http:
        try:
            response = await http.get(
                f"https://api.telegram.org/bot{settings.bot_token}/getMe"
            )
        except httpx.HTTPError as exc:
            return Check("telegram", FAIL, str(exc))
    if response.status_code != 200:
        return Check("telegram", FAIL, f"bot token rejected ({response.status_code})")
    name = response.json()["result"]["username"]
    return Check("telegram", OK, f"@{name}")


async def run_checks(config: Config) -> list[Check]:
    checks: list[Check] = []
    checks.extend(check_telephony(config))
    checks.append(check_public_url(config))
    checks.extend(check_recipients(config))
    llm_check, telegram_check = await asyncio.gather(
        check_llm(config), check_telegram(config)
    )
    checks.append(llm_check)
    checks.append(telegram_check)
    return checks
