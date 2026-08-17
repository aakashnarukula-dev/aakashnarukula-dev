# Medicine Reminder Agent 💊📞

A voice-calling agent that phones the people you care about at fixed times, reminds
them to take their medicine, listens to what they say back, and tells **you** on
Telegram if they never took it.

```
  08:00  ──▶  call Amma  ──▶  "haan, le liya"      ──▶  ✅ done
                    │
                    ├────────  "khana ke baad"     ──▶  ⏳ calls back in 20 min
                    │
                    ├────────  "I don't want it"   ──▶  🔴 Telegram, immediately
                    │
                    └── no answer / voicemail / nothing understood
                                │
                        wait 5 minutes
                                │
                    ──▶  call again  ──▶  still nothing
                                              │
                                              ▼
                                   🔴 Telegram alert to you
```

## What it does

- **Calls on a schedule** — any number of people, any number of times a day, in your
  own timezone. Text-to-speech reads the reminder you wrote.
- **Understands the reply in their own words** — they just answer the phone and talk.
  A small language model reads what they meant and the call responds in the same
  language. The keypad still works as a fallback.
- **Handles "not now"** — "call me after lunch" postpones the reminder instead of
  marking it missed. A flat refusal skips the retry and alerts you straight away,
  because calling back in five minutes was never going to change their mind.
- **Retries once after 5 minutes**, then escalates — delay and attempt count are
  configurable.
- **Tells you what happened** on Telegram, quoting what they actually said.
- **Survives restarts** — pending retries live in SQLite, not in memory. Duplicate
  webhooks, lost callbacks and crashes mid-call are all handled.

## Which model, and why

The short answer: **Gemini 2.5 Flash-Lite** (`gemini-2.5-flash-lite`), and the
choice barely matters on cost. Any provider works — it's one config line.

**The cost question is not close, and not in the direction people expect.** Each
reminder sends roughly 450 input and 60 output tokens — one short exchange, not a
conversation. So the model costs a fraction of a cent, while the phone call costs
five to fifteen cents:

| Model (Aug 2026) | $/1M in | $/1M out | Per call | Per year¹ |
| --- | ---: | ---: | ---: | ---: |
| Groq Llama 3.1 8B | $0.05 | $0.08 | ~$0.00003 | ~$0.07 |
| **Gemini 2.5 Flash-Lite** ← default | $0.10 | $0.40 | ~$0.00007 | ~$0.15 |
| GPT-4.1 nano | $0.10 | $0.40 | ~$0.00007 | ~$0.15 |
| DeepSeek V4 Flash | $0.14 | $0.28 | ~$0.0001 | ~$0.22 |
| Grok 4.1 Fast | $0.20 | $0.50 | ~$0.0001 | ~$0.24 |
| Claude Haiku 4.5 | $1.00 | $5.00 | ~$0.0008 | ~$1.64 |
| — the phone calls themselves — | | | ~$0.0075 | **~$30** |

¹ Two people × three doses a day, every day.

Telephony is ~$16/year of calls plus ~$14/year of number rental, at India's
$0.0075/min. **So the entire spread between the cheapest and the priciest model is
about $1.50 a year — around 5% of an already-small phone bill, and under 1% of it
for every option except Claude.** Optimising the token price is the wrong game. What actually matters on a live call is:

1. **Latency.** Someone is holding a phone waiting for an answer. Flash-Lite is
   built for exactly this and is near the top of the speed rankings.
2. **The languages your family actually speaks.** This is the real differentiator.
   Google has the strongest Indic-language coverage of the major providers, and
   "haan", "ho gaya", "baad mein" are the entire job here.
3. **Not falling over.** A tier-1 provider with real uptime beats a fractionally
   cheaper one.

Flash-Lite is tied-cheapest among the big providers *and* wins on 1 and 2, so it
is the default. Two deliberate non-choices:

- **Not the absolute cheapest** (Groq's 8B, or the sub-cent Chinese flash models).
  They'd save about ten cents a year, and small models are materially worse at the
  Hindi/Telugu yes-or-no nuance that the whole feature rests on.
- **Not a realtime speech-to-speech model** (`gpt-realtime` and friends). Those are
  billed per audio token, cost 10–50× more, and are built for open conversations.
  This is one eight-second exchange — Twilio's own speech-to-text plus a cheap text
  model is simpler *and* cheaper.

> **Prices in this tier moved repeatedly through 2026.** That's precisely why the
> provider is a config line rather than a hard-coded dependency — re-check the
> numbers before you commit to one, and switch in a minute if they move again.

### Switching provider

Set two things and restart. No code changes.

```yaml
# config.yaml
llm:
  provider: openai            # gemini | openai | groq | xai | deepseek | mistral
  model: gpt-4.1-nano         #        | anthropic | custom
```

```bash
# .env  — LLM_API_KEY works for any provider
LLM_API_KEY=sk-...
```

Everything except Anthropic goes through the vendor's OpenAI-compatible endpoint,
so `provider: custom` plus `LLM_BASE_URL` reaches anything else, including a model
you host yourself. The adapter probes for `response_format` and
`max_completion_tokens` support on the first call and remembers what each vendor
accepts.

**With no key set at all**, the agent still runs: it falls back to keyword matching
(`yes/haan/took it` → confirmed, `nahi/no` → refused, `later/baad` → snooze). Less
accurate, but a reminder agent that stops working because an API is down is worse
than a crude one that keeps going. The same fallback catches timeouts and outages
mid-call.

## Quick start

```bash
cd medicine-reminder-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                 # credentials and phone numbers
cp config.example.yaml config.yaml   # who to call, when, and what to say
$EDITOR .env config.yaml

python run.py check                  # validate config and print the plan
python run.py serve                  # start the scheduler + webhook server
```

### Try it without a phone bill

Set `CALL_PROVIDER=console` and the agent simulates calls in your terminal — the
whole retry-and-escalate chain runs for real, only the dialling is fake.

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

> **Calling Indian numbers:** buy a **US number**, not an Indian one. Twilio cannot
> place outbound calls *from* `+91` numbers at all — per its
> [India voice guidelines](https://www.twilio.com/en-us/guidelines/in/voice), "outbound
> calls to India can only be made from international (non-Indian) numbers". Calling
> *to* India from a US number is fine, needs no regulatory bundle or DLT registration,
> and the rental is cheaper. Pick a plain **US local** number ($1.15/month) — it is the
> cheapest voice-capable option, and the per-minute rate is set by the destination, so
> calling India costs the same from it as from anywhere else. Any area code will do.
> Twilio does require recipient consent for *commercial* calls; reminding your own
> family to take medicine is not commercial.
>
> **Save the number in their contacts.** Caller ID is the same US number on every
> call, so have them store it under a name they recognise. An unknown foreign number
> often goes unanswered — which is the one failure this whole system cannot retry
> its way out of.

### 2. A public URL (so the reply can reach the app)

Twilio has to call back into this app with what the person said. Any HTTPS tunnel
works:

```bash
ngrok http 8080          # then put the https URL in PUBLIC_BASE_URL
# or: cloudflared tunnel --url http://localhost:8080
```

**No public URL?** Set `confirmation_mode: answered` in `config.yaml`. The agent then
hands Twilio the whole script up front and polls for the result — picking up counts
as responding. Much less precise, but zero setup.

### 3. A model key

Whichever provider you picked above. For the default:
[aistudio.google.com](https://aistudio.google.com/apikey) → `GEMINI_API_KEY` in
`.env`. Optional — without it the agent falls back to keyword matching.

### 4. Telegram (the alert)

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token into
   `TELEGRAM_BOT_TOKEN`.
2. Message [@userinfobot](https://t.me/userinfobot) to get your numeric chat id →
   `TELEGRAM_CHAT_ID`.
3. **Send your new bot any message first** — Telegram bots cannot start a conversation.
4. Verify: `python run.py test-telegram`

## Configuring reminders

`config.yaml` holds everything human; `.env` holds everything secret. `${VAR}`
placeholders in the YAML are filled from the environment, so real phone numbers
never have to be committed.

```yaml
timezone: Asia/Kolkata

call:
  max_attempts: 2            # first call + one retry
  retry_delay_seconds: 300   # the "call again within 5 minutes" rule
  confirmation_mode: speech  # speech | dtmf | answered
  max_snoozes: 1             # how often "call me back later" is honoured

llm:
  provider: gemini
  model: gemini-2.5-flash-lite

recipients:
  - id: mom
    name: Amma
    phone: ${MOM_PHONE}      # E.164, e.g. +919876543210
    language: hi-IN
    voice: Polly.Aditi
    # Ask the follow-up question in the language they actually speak.
    confirm_prompt: "Aapne dawai le li? Bata dijiye, ya {digit} dabaiye."

schedules:
  - id: morning
    recipient: mom
    at: "08:00"              # local time, in the timezone above
    days: [mon, tue, wed, thu, fri, sat, sun]   # optional, defaults to daily
    message: Namaste Amma, apni subah ki dawai le lijiye.

  - id: night
    recipient: mom
    cron: "30 21 * * *"      # raw crontab, if you prefer
    message: Raat ki dawai lene ka samay ho gaya hai.
```

Voices for Indian English and Hindi: `Polly.Aditi`, `Polly.Raveena`,
`Polly.Kajal-Neural`. Set `language` to match (`en-IN`, `hi-IN`, `te-IN`, …) — it
drives both the speech recogniser and the voice.

**Write `confirm_prompt` in their language.** The reminder being in Hindi while the
follow-up question is in English is exactly the moment an elderly listener gets
confused and hangs up.

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

Keep it running somewhere always on — a small VPS, a Raspberry Pi at home, or any
container host:

```bash
docker compose up -d       # reads .env, mounts config.yaml and ./data
```

The SQLite database lives in `./data`, so pending retries and snoozes survive
restarts. Point `PUBLIC_BASE_URL` at the host's HTTPS address.

## How it decides someone took their medicine

| What happened on the call | Result |
| --- | --- |
| Said yes, in any language | ✅ done |
| Pressed `1` | ✅ done — the keypad never needs the model |
| "After lunch" / "in ten minutes" | ⏳ calls back then (up to `max_snoozes` times) |
| "No, I don't want it" | 🔴 alerts you immediately — no pointless retry |
| Someone else answered | ❌ missed — retries |
| Nothing said, or nothing understood | ❌ missed — retries |
| Rang out / declined / busy / voicemail | ❌ missed — retries, never talks to voicemail |
| Twilio refused to place the call | ❌ missed — retries |

Every transition is idempotent: a webhook Twilio delivers twice, a status callback
that races the reply, or a restart mid-call can never produce a duplicate call or a
duplicate alert. If a callback never arrives at all, the engine polls the provider
and resolves the call itself. A snooze restarts the retry ladder rather than burning
the attempts left for the dose.

## Using another telephony provider

`app/telephony/` is a two-method interface — `place_call()` and `fetch_outcome()`.
To add Exotel, Plivo or anything else, implement those two against
`app/telephony/base.TelephonyProvider`, map the provider's call statuses through
`map_call_status()`, and register it in `app/telephony/__init__.py::build_provider`.
`console_provider.py` is a 40-line reference implementation. Nothing in the retry or
escalation logic needs to change.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```

The suite covers the state machine (retry timing, snooze, refusal, escalation,
duplicate webhooks, provider failures, alert retries), reply understanding (parsing,
bounds, the offline fallback), the webhook layer, and config validation — no Twilio
account, model key, or network access needed.

```
app/
  config.py      YAML + env config, validated with human-readable errors
  models.py      shared types: run status, call outcome, reply intent
  db.py          SQLite store; idempotency lives in its UNIQUE constraints
  engine.py      the state machine: call → understand → retry/snooze → escalate
  llm/           reply understanding
    base.py        prompt, schema, lenient parsing, keyword fallback
    providers.py   one OpenAI-compatible adapter + one for Claude
  scheduler.py   APScheduler cron wiring + the periodic tick
  web.py         FastAPI: Twilio webhooks, status API, lifecycle
  voice.py       TwiML — what the person actually hears
  notifier.py    Telegram Bot API client
  telephony/     provider interface, Twilio, and a console simulator
```

## A word of caution

This is an assistive reminder, not a medical device. Calls fail for reasons no
software controls — the phone is off, the network drops, speech recognition
mishears. Treat the Telegram alert as the safety net it is, and don't rely on this
alone for anything critical.

Sources for the model comparison:
[Anthropic models overview](https://platform.claude.com/docs/en/about-claude/models/overview),
[CloudZero LLM API pricing](https://www.cloudzero.com/blog/llm-api-pricing-comparison/),
[Requesty cheapest LLM APIs 2026](https://www.requesty.ai/blog/cheapest-llm-api-prices-compared-2026),
[Softcery: best LLMs for voice agents 2026](https://softcery.com/lab/ai-voice-agents-choosing-the-right-llm),
[Layer3Labs: best LLM for voice agents](https://www.layer3labs.io/guides/best-llm-for-voice-agents),
[CarmaOne: Indian-language voice agents](https://www.carmaone.ai/blog/best-voice-ai-agents-indian-languages-2026).
