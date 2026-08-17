"""TwiML generation — what the person actually hears when they pick up."""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

#: Every line the call can speak besides the reminder itself. These are
#: English fallbacks — set `phrases:` on the recipient to say them in their
#: own language. A voice pronounces whatever text it is handed, so a Telugu
#: voice reading these English defaults is worse than useless.
DEFAULT_PHRASES = {
    "confirm_prompt": (
        "Have you taken it? You can just answer me, or press {digit} to confirm."
    ),
    "dtmf_prompt": "Press {digit} to confirm you have taken your medicine.",
    "thanks": "Thank you. Take care. Goodbye.",
    "no_reply": "We did not get a response. We will call you again shortly. Goodbye.",
    "snooze_ack": "Alright, I will call you again shortly.",
    "refused_ack": "Alright. I will let the family know.",
    "unclear_ack": "Sorry, I did not catch that. We will call again shortly.",
}

#: Nudges the speech recogniser toward the words that actually decide the call.
#: Twilio weights these, which matters most for accented and non-English yes/no.
#: Telugu first, English second — elderly speakers code-switch constantly.
DEFAULT_SPEECH_HINTS = (
    "అవును,సరే,వేసుకున్నాను,తీసుకున్నాను,అయ్యింది,అయిపోయింది,"
    "లేదు,వద్దు,తర్వాత,తిన్నాక,కొంచెం సేపు,"
    "avunu,sare,vesukunnanu,teesukunnanu,ayyindi,ledu,vaddu,tarvata,tinnaka,"
    "yes,no,okay,taken,took it,done,not yet,later,after lunch,after dinner"
)


def _say(text: str, voice: str, language: str) -> str:
    return (
        f"<Say voice={quoteattr(voice)} language={quoteattr(language)}>"
        f"{escape(text)}</Say>"
    )


def reminder_twiml(
    *,
    message: str,
    voice: str,
    language: str,
    mode: str = "answered",
    confirm_digit: str = "1",
    gather_timeout: int = 8,
    speech_timeout: str = "auto",
    action_url: str | None = None,
    speech_hints: str = DEFAULT_SPEECH_HINTS,
    phrases: dict[str, str] | None = None,
) -> str:
    """Build the reminder call script.

    ``mode`` is one of ``speech`` (say anything — a model reads the meaning),
    ``dtmf`` (a keypress is required), or ``answered`` (nothing is collected;
    picking up is the whole signal). Speech mode still accepts the keypress,
    so a recogniser failure never strands the call.
    """
    spoken = message.strip()
    say_phrase = lambda key: (phrases or {}).get(key) or DEFAULT_PHRASES[key]

    if mode == "answered" or not action_url:
        body = [
            _say(spoken, voice, language),
            '<Pause length="1"/>',
            _say(spoken, voice, language),
            _say(say_phrase("thanks"), voice, language),
        ]
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(body)}</Response>'

    listening_for_speech = mode == "speech"
    template = say_phrase("confirm_prompt" if listening_for_speech else "dtmf_prompt")
    prompt = template.format(digit=confirm_digit)
    inner = "".join(
        [
            _say(spoken, voice, language),
            '<Pause length="1"/>',
            _say(prompt, voice, language),
        ]
    )

    attrs = [
        'input="speech dtmf"' if listening_for_speech else 'input="dtmf"',
        'numDigits="1"',
        f'timeout="{int(gather_timeout)}"',
        f"action={quoteattr(action_url)}",
        'method="POST"',
        'actionOnEmptyResult="true"',
    ]
    if listening_for_speech:
        attrs.insert(1, f"speechTimeout={quoteattr(speech_timeout)}")
        attrs.insert(2, f"language={quoteattr(language)}")
        if speech_hints:
            attrs.insert(3, f"hints={quoteattr(speech_hints)}")

    body = [
        f"<Gather {' '.join(attrs)}>{inner}</Gather>",
        # Reached only if <Gather> collected nothing and the action URL was
        # unreachable — otherwise the action URL owns the goodbye.
        _say(say_phrase("no_reply"), voice, language),
    ]
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(body)}</Response>'


def closing_twiml(text: str, voice: str, language: str) -> str:
    """Short spoken reply used by the reply webhook before hanging up."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"{_say(text, voice, language)}<Hangup/></Response>"
    )
