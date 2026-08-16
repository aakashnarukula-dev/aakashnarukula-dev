"""TwiML generation — what the person actually hears when they pick up."""

from __future__ import annotations

from xml.sax.saxutils import escape, quoteattr

#: Read aloud after the reminder when we need a keypress to count it as answered.
CONFIRM_PROMPT = "Press {digit} to confirm you have taken your medicine."
NO_RESPONSE_LINE = "We did not get a response. We will call you again shortly. Goodbye."
THANK_YOU_LINE = "Thank you. Take care. Goodbye."


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
    confirm_digit: str | None = None,
    gather_timeout: int = 8,
    action_url: str | None = None,
) -> str:
    """Build the reminder call script.

    With ``action_url`` + ``confirm_digit`` the message is wrapped in a <Gather> so a
    keypress proves someone actually heard it. Without them the message is simply
    spoken twice — used when no public webhook URL is available.
    """
    spoken = message.strip()
    body: list[str] = []

    if action_url and confirm_digit:
        prompt = CONFIRM_PROMPT.format(digit=confirm_digit)
        inner = "".join(
            [
                _say(spoken, voice, language),
                "<Pause length=\"1\"/>",
                _say(prompt, voice, language),
                "<Pause length=\"2\"/>",
                _say(spoken, voice, language),
                _say(prompt, voice, language),
            ]
        )
        body.append(
            f'<Gather input="dtmf" numDigits="1" timeout="{int(gather_timeout)}" '
            f"action={quoteattr(action_url)} method=\"POST\" actionOnEmptyResult=\"true\">"
            f"{inner}</Gather>"
        )
        # Reached only if <Gather> collected nothing and the action URL was unreachable.
        body.append(_say(NO_RESPONSE_LINE, voice, language))
    else:
        body.append(_say(spoken, voice, language))
        body.append("<Pause length=\"1\"/>")
        body.append(_say(spoken, voice, language))
        body.append(_say(THANK_YOU_LINE, voice, language))

    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{"".join(body)}</Response>'


def closing_twiml(text: str, voice: str, language: str) -> str:
    """Short spoken reply used by the keypress webhook before hanging up."""
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><Response>'
        f"{_say(text, voice, language)}<Hangup/></Response>"
    )
