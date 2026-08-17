"""Provider-agnostic contract for reading a spoken reply."""

from __future__ import annotations

import json
import logging
import re
from typing import Literal, Protocol

from pydantic import BaseModel, Field, ValidationError

from ..config import Recipient, Schedule
from ..models import ReplyIntent

log = logging.getLogger(__name__)

#: Short on purpose — every token is latency while someone holds a phone.
SYSTEM_PROMPT = """\
You interpret what someone said when an automated call reminded them to take \
their medicine, and you write the sentence the call speaks back to them.

Reply with JSON only, matching exactly this shape:
{"intent": "...", "snooze_minutes": 0, "spoken_reply": "...", "summary": "..."}

intent is one of:
- confirmed: they have taken it, or are taking it right now
- snooze: they intend to take it shortly, but not yet
- refused: they are declining to take it at all
- wrong_person: whoever answered is not the person being reminded
- unclear: the transcript is empty, garbled, or you cannot tell

snooze_minutes: how long they asked for, when the intent is snooze; 0 otherwise.
spoken_reply: one or two short sentences, read aloud by text-to-speech, in the \
same language they replied in. Warm and brief. No emoji, no markdown, no \
abbreviations — every character is spoken aloud.
summary: one short English line for the family member's alert, quoting what \
they actually said.

Speech-to-text is unreliable on elderly voices and on Indian languages, and it \
returns no punctuation. Transcripts may come back in the local script, in \
romanised form, or code-switched with English mid-sentence. If it is anywhere \
close to a yes — "అవును", "సరే", "వేసుకున్నాను", "avunu", "sare", "took it", \
"done", "ok" — read it as confirmed. Reserve unclear for transcripts with \
genuinely nothing to go on."""

#: Words that carry a decision on their own, used when no model is reachable.
# Telugu script has no word boundaries the way \b expects, so script forms are
# matched as plain alternatives and only the romanised/English forms use \b.
_FALLBACK_PATTERNS: list[tuple[re.Pattern[str], ReplyIntent]] = [
    (re.compile(r"లేదు|వద్దు|వొద్దు|\b(no|ledu|vaddu|not|won'?t|refuse|don'?t want)\b",
                re.I), ReplyIntent.REFUSED),
    (re.compile(r"తర్వాత|తరువాత|తిన్నాక|కొంచెం|సేపు|"
                r"\b(later|after|tarvata|tarwata|tinnaka|konchem|minute|minutes|"
                r"shortly|soon)\b", re.I), ReplyIntent.SNOOZE),
    (re.compile(r"అవును|సరే|వేసుకున్న|తీసుకున్న|అయ్యింది|అయిపోయింది|"
                r"\b(yes|yeah|yep|ok|okay|avunu|sare|vesukunna|vesukunnanu|"
                r"teesukunna|teesukunnanu|ayyindi|aipoyindi|took|taken|done|"
                r"finished)\b", re.I), ReplyIntent.CONFIRMED),
]

#: Which recipient phrase answers each intent when no model is reachable.
_FALLBACK_PHRASE_KEYS = {
    ReplyIntent.CONFIRMED: "thanks",
    ReplyIntent.SNOOZE: "snooze_ack",
    ReplyIntent.REFUSED: "refused_ack",
    ReplyIntent.WRONG_PERSON: "unclear_ack",
    ReplyIntent.UNCLEAR: "unclear_ack",
}


class ReminderReading(BaseModel):
    """What the model made of the reply."""

    intent: Literal["confirmed", "snooze", "refused", "wrong_person", "unclear"]
    snooze_minutes: int = Field(
        default=0,
        description="Minutes they asked to be called back in; 0 when not snoozing.",
    )
    spoken_reply: str = Field(description="What the call says back, read aloud.")
    summary: str = Field(description="One short English line for the alert.")

    def as_intent(self) -> ReplyIntent:
        return ReplyIntent(self.intent)


def keyword_reading(transcript: str, recipient: Recipient) -> ReminderReading:
    """Deterministic reading used when no model answers.

    A reminder agent that stops working because an API is down is worse than a
    crude one that keeps going, so this path always returns something usable.
    """
    text = (transcript or "").strip()
    intent = ReplyIntent.UNCLEAR
    for pattern, candidate in _FALLBACK_PATTERNS:
        if pattern.search(text):
            intent = candidate
            break

    from ..voice import DEFAULT_PHRASES

    key = _FALLBACK_PHRASE_KEYS[intent]
    return ReminderReading(
        intent=intent.value,
        snooze_minutes=15 if intent is ReplyIntent.SNOOZE else 0,
        # Spoken aloud, so it must be in their language even with no model.
        spoken_reply=recipient.phrase(key, DEFAULT_PHRASES[key]),
        summary=f"{recipient.name} said: \"{text or '(nothing heard)'}\"",
    )


def build_user_turn(recipient: Recipient, schedule: Schedule, transcript: str) -> str:
    return (
        f"Person being reminded: {recipient.name} (speaks {recipient.language})\n"
        f"The call said: {schedule.message}\n"
        f"They replied: {transcript.strip() or '(silence — nothing was heard)'}"
    )


def parse_reading(raw: str) -> ReminderReading | None:
    """Pull a ReminderReading out of a model's text response.

    Providers differ in how strictly they honour a JSON-only instruction, so
    this tolerates code fences and surrounding prose rather than assuming.
    """
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    if not text.startswith("{"):
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return None
        text = match.group(0)

    try:
        return ReminderReading.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        log.warning("could not parse model reading: %s", exc)
        return None


class ReplyReader(Protocol):
    """Anything that can turn a spoken reply into a decision."""

    name: str

    async def read(self, prompt: str) -> str | None:
        """Return the model's raw response text, or None on failure."""

    async def aclose(self) -> None: ...
