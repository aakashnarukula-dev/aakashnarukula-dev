"""Reading spoken replies — parsing, bounds, and the offline fallback."""

from __future__ import annotations

import json

import pytest

from app.config import LLMSettings, Recipient, Schedule
from app.llm import ReminderLLM
from app.llm.base import keyword_reading, parse_reading
from app.models import ReplyIntent

from .conftest import ScriptedReader, make_llm

RECIPIENT = Recipient(id="mom", name="Amma", phone="+919876543210", language="en-IN")
SCHEDULE = Schedule(
    id="morning", recipient_id="mom", message="Take your tablet.", cron="0 8 * * *"
)


async def read(reader: ScriptedReader | None, transcript: str, **settings):
    llm = ReminderLLM(LLMSettings(enabled=True, api_key="test", **settings), reader)
    return await llm.read_reply(
        recipient=RECIPIENT, schedule=SCHEDULE, transcript=transcript
    )


@pytest.mark.asyncio
async def test_reads_a_spoken_confirmation():
    reader = ScriptedReader()
    reader.script(intent="confirmed", spoken_reply="Lovely, thank you.")

    reading = await read(reader, "yes I took it just now")

    assert reading.as_intent() is ReplyIntent.CONFIRMED
    assert reading.spoken_reply == "Lovely, thank you."
    assert "Take your tablet." in reader.prompts[0]


@pytest.mark.asyncio
async def test_json_wrapped_in_a_code_fence_is_still_read():
    reader = ScriptedReader(
        "```json\n" + json.dumps({
            "intent": "snooze", "snooze_minutes": 20,
            "spoken_reply": "Okay.", "summary": "asked for 20 minutes",
        }) + "\n```"
    )

    reading = await read(reader, "call me in twenty minutes")

    assert reading.as_intent() is ReplyIntent.SNOOZE
    assert reading.snooze_minutes == 20


@pytest.mark.asyncio
async def test_snooze_is_capped_and_never_zero():
    reader = ScriptedReader()
    reader.script(intent="snooze", snooze_minutes=999)
    assert (await read(reader, "later", max_snooze_minutes=60)).snooze_minutes == 60

    reader.script(intent="snooze", snooze_minutes=0)
    capped = await read(reader, "later", default_snooze_minutes=15)
    assert capped.snooze_minutes == 15


@pytest.mark.asyncio
async def test_unreachable_model_falls_back_to_keywords():
    reading = await read(ScriptedReader(response=None), "haan le liya")

    assert reading.as_intent() is ReplyIntent.CONFIRMED
    assert reading.spoken_reply  # the call still has something to say


@pytest.mark.asyncio
async def test_unparseable_response_falls_back_to_keywords():
    reading = await read(ScriptedReader("I'm afraid I can't do that"), "no not taking")

    assert reading.as_intent() is ReplyIntent.REFUSED


@pytest.mark.asyncio
async def test_silence_never_reaches_the_model():
    reader = ScriptedReader()
    reader.script(intent="confirmed")

    reading = await read(reader, "   ")

    assert reading.as_intent() is ReplyIntent.UNCLEAR
    assert reader.prompts == [], "a blank transcript should not cost a round trip"


@pytest.mark.asyncio
async def test_works_with_no_model_configured():
    llm = make_llm(None)
    assert not llm.enabled

    reading = await llm.read_reply(
        recipient=RECIPIENT, schedule=SCHEDULE, transcript="yes took it"
    )
    assert reading.as_intent() is ReplyIntent.CONFIRMED


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("haan le liya", ReplyIntent.CONFIRMED),
        ("yes done", ReplyIntent.CONFIRMED),
        ("nahi", ReplyIntent.REFUSED),
        ("I'll take it after lunch", ReplyIntent.SNOOZE),
        ("zzzz krrk", ReplyIntent.UNCLEAR),
        ("", ReplyIntent.UNCLEAR),
    ],
)
def test_keyword_fallback_classification(transcript, expected):
    assert keyword_reading(transcript, RECIPIENT).as_intent() is expected


def test_parse_reading_rejects_junk():
    assert parse_reading("") is None
    assert parse_reading("no json here") is None
    assert parse_reading('{"intent": "not-a-real-intent"}') is None
