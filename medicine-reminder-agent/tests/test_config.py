"""Config loading: schedules, validation, and env expansion."""

from __future__ import annotations

import pytest

from app.config import ConfigError, load_config


def build_yaml(
    schedules: str,
    *,
    phone: str = '"+919876543210"',
    call: str = "  confirmation_mode: answered\n",
) -> str:
    return (
        "timezone: Asia/Kolkata\n"
        "call:\n" + call +
        "telegram:\n"
        "  enabled: false\n"
        "recipients:\n"
        "  - id: mom\n"
        "    name: Amma\n"
        f"    phone: {phone}\n"
        "schedules:\n" + schedules
    )


@pytest.fixture(autouse=True)
def console_provider(monkeypatch):
    monkeypatch.setenv("CALL_PROVIDER", "console")
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)


def write(tmp_path, body: str):
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return path


SIMPLE = '  - id: m\n    recipient: mom\n    at: "08:00"\n    message: hi\n'


def test_at_and_days_become_a_cron_expression(tmp_path):
    path = write(
        tmp_path,
        build_yaml(
            '  - id: morning\n    recipient: mom\n    at: "08:05"\n'
            "    days: [mon, wed, Friday]\n    message: take your tablet\n"
        ),
    )
    assert load_config(path).schedule("morning").cron == "5 8 * * mon,wed,fri"


def test_raw_cron_is_passed_through(tmp_path):
    path = write(
        tmp_path,
        build_yaml(
            '  - id: night\n    recipient: mom\n    cron: "30 21 * * *"\n'
            "    message: night tablet\n"
        ),
    )
    assert load_config(path).schedule("night").cron == "30 21 * * *"


def test_phone_numbers_must_be_e164(tmp_path):
    path = write(tmp_path, build_yaml(SIMPLE, phone='"9876543210"'))
    with pytest.raises(ConfigError, match="E.164"):
        load_config(path)


def test_unknown_recipient_is_rejected(tmp_path):
    path = write(
        tmp_path,
        build_yaml('  - id: m\n    recipient: nobody\n    at: "08:00"\n    message: hi\n'),
    )
    with pytest.raises(ConfigError, match="unknown recipient"):
        load_config(path)


def test_a_schedule_without_a_time_is_rejected(tmp_path):
    path = write(
        tmp_path, build_yaml("  - id: m\n    recipient: mom\n    message: hi\n")
    )
    with pytest.raises(ConfigError, match="HH:MM"):
        load_config(path)


def test_env_placeholders_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("MOM_PHONE", "+919999988888")
    path = write(tmp_path, build_yaml(SIMPLE, phone="${MOM_PHONE}"))
    assert load_config(path).recipient("mom").phone == "+919999988888"


def test_dtmf_mode_requires_a_public_url(tmp_path):
    path = write(
        tmp_path, build_yaml(SIMPLE, call="  confirmation_mode: dtmf\n")
    )
    with pytest.raises(ConfigError, match="PUBLIC_BASE_URL"):
        load_config(path)


def test_telegram_must_be_configured_when_enabled(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    path = write(tmp_path, build_yaml(SIMPLE).replace("  enabled: false", "  enabled: true"))
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(path)
