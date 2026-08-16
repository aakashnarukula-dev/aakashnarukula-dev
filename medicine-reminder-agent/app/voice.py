"""TwiML generation — what the person actually hears when they pick up."""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

CONFIRM_PROMPT = "Press {digit} to confirm you have taken your medicine."
SPEECH_PROMPT = (
    "Have you taken it? You can just answer me, or press {digit} to confirm."
)
NO_RESPONSE_LINE = "We did not get a response. We will call you again shortly. Goodbye."
THANK_YOU_LINE = "Thank you. Take care. Goodbye."

#: Nudges the speech recogniser toward the words that actually decide the call.
#: Twilio weights these, which matters most for accented and non-English yes/no.
DEFAULT_SPEECH_HINTS = (
    "yes,no,yeah,okay,already,taken,took it,done,not yet,later,"
    "after lunch,after dinner,in ten minutes,haan,nahi,ho gaya,le liya,"
    "kha liya,baad mein,thodi der"
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
    confirm_prompt: str = "",
) -> str:
    """Build the reminder call script.

    ``mode`` is one of ``speech`` (say anything — a model reads the meaning),
    ``dtmf`` (a keypress is required), or ``answered`` (nothing is collected;
    picking up is the whole signal). Speech mode still accepts the keypress,
    so a recogniser failure never strands the call.
    """
    spoken = message.strip()

    if mode == "answered" or not action_url:
        body = [
            _say(spoken, voice, language),
            '<Pause length="1"/>',
            _say(spoken, voice, language),
            _say(THANK_YOU_LINE, voice, language),
        ]
        return f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(body)}</Response>'

    listening_for_speech = mode == "speech"
    template = confirm_prompt or (
        SPEECH_PROMPT if listening_for_speech else CONFIRM_PROMPT
    )
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
        _say(NO_RESPONSE_LINE, voice, language),
    ]
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(body)}</Response>'


def closing_twiml(text: str, voice: str, language: str) -> str:
    """Short spoken reply used by the reply webhook before hanging up."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"{_say(text, voice, language)}<Hangup/></Response>"
    )
