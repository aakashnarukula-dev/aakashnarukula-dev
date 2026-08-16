# Medicine Reminder Agent 💊📞

A voice-calling agent that phones the people you care about at fixed times, reminds
them to take their medicine, and tells **you** on Telegram if they never picked up.

```
  08:00  ──▶  call Amma  ──▶  she presses 1  ──▶  ✅ done
                    │
                    └── no answer / voicemail / no keypress
                                │
                        wait 5 minutes
                                │
                    ──▶  call again  ──▶  she presses 1  ──▶  ✅ done
                                │
                                └── still nothing
                                        │
                                        ▼
                             🔴 Telegram alert to you
```

## What it does

- **Calls on a schedule** — any number of people, any number of times a day, in your
  own timezone. Text-to-speech reads the reminder you wrote.
- **Knows whether they actually responded** — the call asks them to press `1`.
  Ringing out, voicemail, a busy line, or picking up and saying nothing all count as
  *missed*.
- **Retries once after 5 minutes** — delay and attempt count are configurable.
- **Escalates to you on Telegram** when every attempt is missed, with who, which
  reminder, and what happened on each call.
- **Survives restarts** — pending retries live in SQLite, not in memory. Duplicate
  webhooks, lost callbacks and crashes mid-call are all handled.

## Quick start

```bash
cd medicine-reminder-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # credentials and phone numbers
cp config.example.yaml config.yaml   # who to call, when, and what to say
$EDITOR .env config.yaml

python run.py check             # validate config and print the plan
python run.py serve             # start the scheduler + webhook server
```

### Try it without a phone bill

Set `CALL_PROVIDER=console` in `.env` and the agent simulates calls in your terminal —
the whole retry-and-escalate chain runs for real, only the dialling is fake.

```bash
CALL_PROVIDER=console CONSOLE_OUTCOME=no_answer python run.py call-now morning
```

`CONSOLE_OUTCOME` can be `no_answer`, `busy`, `failed`, `answered_human` or
`answered_machine`.

## Setup

### 1. Twilio (the calls)

1. Create an account at [twilio.com](https://www.twilio.com/) and buy a number with
   **Voice** capability.
2. Copy the Account SID, Auth Token and number into `.env`.
3. On a trial account you can only call **verified** numbers — verify each recipient
   under *Phone Numbers → Verified Caller IDs* first.

> **Calling Indian numbers:** Indian regulation restricts outbound automated voice.
> Twilio requires a [regulatory bundle / DLT registration](https://www.twilio.com/en-us/guidelines/in/regulatory)
> before it will deliver calls to `+91` numbers, and personal (non-business) use is
> often refused. If that blocks you, the same agent works with an Indian CPaaS —
> Exotel, Plivo, Knowlarity or MyOperator. See [Using another provider](#using-another-provider).

### 2. A public URL (so a keypress can reach the app)

Twilio has to call back into this app to report the keypress. Any HTTPS tunnel works:

```bash
ngrok http 8080          # then put the https URL in PUBLIC_BASE_URL
# or: cloudflared tunnel --url http://localhost:8080
```

**No public URL?** Set `confirmation_mode: answered` in `config.yaml`. The agent then
hands Twilio the whole script up front and polls for the result — picking up counts as
responding. Less precise (someone can answer and put the phone down), but zero setup.

### 3. Telegram (the alert)

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Message [@userinfobot](https://t.me/userinfobot) to get your numeric chat id →
   `TELEGRAM_CHAT_ID`.
3. **Send your new bot any message first** — Telegram bots cannot start a conversation.
4. Verify: `python run.py test-telegram`

## Configuring reminders

`config.yaml` holds everything human; `.env` holds everything secret. `${VAR}`
placeholders in the YAML are filled from the environment, so real phone numbers never
have to be committed.

```yaml
timezone: Asia/Kolkata

call:
  max_attempts: 2            # first call + one retry
  retry_delay_seconds: 300   # the "call again within 5 minutes" rule
  ring_timeout_seconds: 30
  confirmation_mode: dtmf    # dtmf = must press a key | answered = picking up is enough
  confirm_digit: "1"

recipients:
  - id: mom
    name: Amma
    phone: ${MOM_PHONE}      # E.164, e.g. +919876543210
    language: en-IN
    voice: Polly.Aditi       # any Twilio TTS voice

schedules:
  - id: morning
    recipient: mom
    at: "08:00"              # local time, in the timezone above
    days: [mon, tue, wed, thu, fri, sat, sun]   # optional, defaults to every day
    message: Good morning Amma. Please take your morning tablets with breakfast.

  - id: night
    recipient: mom
    cron: "30 21 * * *"      # raw crontab, if you prefer
    message: Please take your night medicines before sleeping.
```

Voices for Indian English: `Polly.Aditi`, `Polly.Raveena`, `Polly.Kajal-Neural`.
Set `language` to match (`en-IN`, `hi-IN`, `te-IN`, …).

## Commands

| Command | What it does |
| --- | --- |
| `python run.py serve` | Run the scheduler and webhook server (the normal mode) |
| `python run.py check` | Validate config and print every schedule it will run |
| `python run.py call-now <schedule>` | Place one reminder immediately and follow it |
| `python run.py test-telegram` | Send yourself a test alert |

While `serve` is running:

```bash
curl localhost:8080/health
curl localhost:8080/status                        # recent runs + next scheduled times
curl -X POST localhost:8080/admin/trigger/morning # fire a reminder right now
```

Set `ADMIN_TOKEN` in `.env` to require an `X-Admin-Token` header on those two.

## Deploying

Keep it running somewhere that is always on — a small VPS, a Raspberry Pi at home, or
any container host:

```bash
docker compose up -d       # reads .env, mounts config.yaml and ./data
```

The SQLite database lives in `./data`, so pending retries survive restarts and
redeploys. Point `PUBLIC_BASE_URL` at the host's HTTPS address.

## How it decides someone "didn't respond"

| What happened on the call | Counts as responded? |
| --- | --- |
| Answered, pressed `1` | ✅ yes |
| Answered, pressed nothing (or a different key) | ❌ no |
| Rang out / declined / busy | ❌ no |
| Voicemail or an IVR picked up | ❌ no — the agent hangs up rather than talking to it |
| Twilio refused to place the call | ❌ no |

In `confirmation_mode: answered` the first two rows both count as responded.

Every transition is idempotent: a webhook Twilio delivers twice, a status callback that
races the keypress, or a restart mid-call can never produce a duplicate call or a
duplicate alert. If a callback never arrives at all, the engine polls the provider and
resolves the call itself.

## Using another provider

`app/telephony/` is a two-method interface — `place_call()` and `fetch_outcome()`. To
add Exotel, Plivo or anything else, implement those two against
`app/telephony/base.TelephonyProvider`, map the provider's call statuses through
`map_call_status()`, and register it in `app/telephony/__init__.py::build_provider`.
`console_provider.py` is a 40-line reference implementation. Nothing in the retry or
escalation logic needs to change.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The suite covers the state machine (retry timing, escalation, acknowledgement,
duplicate webhooks, provider failures, alert retries), the webhook layer, and config
validation — no Twilio account or network access needed.

```
app/
  config.py      YAML + env config, validated with human-readable errors
  models.py      shared types: run status, call outcome, call request
  db.py          SQLite store; idempotency lives in its UNIQUE constraints
  engine.py      the state machine: call → retry → escalate
  scheduler.py   APScheduler cron wiring + the periodic tick
  web.py         FastAPI: Twilio webhooks, status API, lifecycle
  voice.py       TwiML — what the person actually hears
  notifier.py    Telegram Bot API client
  telephony/     provider interface, Twilio, and a console simulator
```

## A word of caution

This is an assistive reminder, not a medical device. Calls can fail for reasons no
software controls — the phone is off, the network drops, the provider has an outage.
Treat the Telegram alert as the safety net it is, and don't rely on this alone for
anything critical.
