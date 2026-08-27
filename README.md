# Voice Agent

A low-latency live voice agent that answers a phone call, holds a real
conversation about the business, and books a callback into a CSV.

Runs four ways from the same code: your terminal, a browser, a real phone
number, and **WhatsApp**. The agent is identical in all of them.

---

## The stack

| Layer | Choice | Why this one |
|---|---|---|
| Orchestration | [LiveKit Agents](https://docs.livekit.io/agents/) 1.6 | Handles turn-taking, barge-in, and audio transport. See "Why a framework" below. |
| STT | Deepgram **Flux** (`flux-general-multi`) | End-of-turn detection is *inside* the model (~260 ms), so there is no separate turn model and no extra round trip. Biggest latency win in the stack. |
| LLM | Groq **`openai/gpt-oss-120b`** | Time to first token is what matters on a call, not tokens per second. |
| TTS | Rumik **`mulberry`** | Speaks English, Hindi and Hinglish with mid-sentence switching. Deepgram's Aura has no Hindi voice at all. |
| VAD | Silero (local) | Drives barge-in. Runs on-device, no key, no download. |

All swappable with one environment variable. See [Switching models](#switching-models).

---

## Quick start

### Prerequisites

```bash
# Package manager (installs Python 3.13 for you)
curl -LsSf https://astral.sh/uv/install.sh | sh

# The LiveKit CLI - this is what runs the agent
brew install livekit-cli        # macOS
# Linux: curl -sSL https://get.livekit.io/cli | bash
```

### Setup

```bash
git clone <this-repo> && cd voice-agent
uv sync                 # creates .venv, installs everything
cp .env.example .env
```

### API keys

Put these in `.env`:

| Key | Where | Cost | Needed for |
|---|---|---|---|
| `DEEPGRAM_API_KEY` | [console.deepgram.com](https://console.deepgram.com) | $200 free credit, no card | STT (and TTS on the english profile) |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) | Free tier | The LLM |
| `RUMIK_API_KEY` | [playground.rumik.ai](https://playground.rumik.ai) | Pay as you go, ~Rs 0.50/1k chars | Hindi/Hinglish speech |

Only the first two are needed if you run `LANGUAGE_PROFILE=english`.

### Talk to it

```bash
lk agent console
```

Your microphone and speakers connect straight to the agent. **No LiveKit account
required.** Say "hi" and it should answer in about a second.

`Ctrl+C` to quit. It does not exit on its own.

> **macOS:** the first run asks for microphone permission for your terminal app.
> No audio input usually means it was denied. Grant it under
> System Settings → Privacy & Security → Microphone.

Useful flags:

```bash
lk agent console --list-devices     # pick a mic
lk agent console --text             # type instead of talk, no mic needed
```

`--text` is the fastest way to check prompt and tool behaviour.

---

## Running in a browser

Needs a free [LiveKit Cloud](https://cloud.livekit.io) project.

```bash
lk cloud auth                    # then add the three values to .env
lk agent dev
```

The worker registers and prints an **Agent console** URL. Open it, click *Start a
session*, allow the mic. You get a live transcript, event timeline and tool-call
inspection, which beats reading terminal logs. Works from a phone too.

`lk agent dev` hot-reloads on file changes.

`.env` needs:

```bash
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
```

### Local LiveKit instead of cloud

```bash
brew install livekit     # the server, a separate package from livekit-cli
livekit-server --dev     # ws://127.0.0.1:7880, key "devkey", secret "secret"
```

Set `LIVEKIT_MODE=local` and run `lk agent dev --dev`.

Those dev credentials are fixed and public by design; they are built into
`livekit-server`, not secrets.

**What you lose locally.** A self-hosted server is media only, with no inference
gateway. So `TURN_DETECTION=livekit`, `INTERRUPTION_MODE=adaptive`, the browser
Agent Console, noise cancellation and SIP telephony are all LiveKit Cloud
features. The core pipeline (Deepgram, Groq, Rumik, Flux turn detection) is
unaffected. Asking for a cloud-only feature in local mode fails at startup with
an explanation.

---

## Running it hosted (no laptop required)

Both halves can run without anything on your machine.

### The agent, on LiveKit Cloud

```bash
lk agent create --secrets-file .env --region ap-south   # first time
lk agent deploy --secrets-file .env                     # subsequent updates
lk agent status
lk agent logs
```

`livekit.toml` pins the agent id, so later commands need no arguments. The free
Build plan allows **1 agent deployment**; billing is per agent-session minute,
not per replica.

**The generated Dockerfile needed three fixes**, all commented in the file:

1. Its default `CMD` ran the module file directly, which only imports it and
   exits, because this project uses the `AgentServer` API and has no `__main__`
   block. It now runs `python -m livekit.agents start agent.py`.
2. `uv sync` ran before the package existed. This is a package project
   (`uv_build` backend), so a stub module is created before the real source is
   copied in.
3. `README.md` is declared as the project readme, is read during `uv sync`, and
   had to be copied in the early layer as well as un-ignored.

**Cold starts:** "cold start prevention" starts at LiveKit's Ship plan, so on the
free tier the first call after an idle period may take a moment to answer.

**Leads written by the deployed agent are lost.** The container filesystem is
ephemeral, so `data/` does not survive a restart or redeploy. Local runs are
unaffected. Persisting cloud leads needs an external sink (a webhook to a sheet,
or a database).

### The WhatsApp webhook, on Vercel

Deployed from `deploy/webhook/` as its **own** Vercel project, deliberately
separate from the marketing site.

```bash
cd deploy/webhook
vercel deploy --prod
```

Environment variables live in the Vercel project settings, not in the deploy
command. Setting them with `-e` flags applies to a single deployment only and
leaves the dashboard empty, which breaks the next deploy.

That directory keeps its own tiny `requirements.txt` (`fastapi`, `livekit-api`,
`python-dotenv`) so the serverless bundle stays small and cold starts stay fast.
The agent's heavy ML dependencies are not needed there.

---

## WhatsApp calls

A caller rings your WhatsApp Business number and the agent answers. The call
travels over data rather than the phone network, so there is no carrier, no DID
and no telephony regulation involved.

### How it works

```
1. caller dials the WhatsApp Business number
2. Meta POSTs a "call connect" webhook containing an SDP offer
3. the webhook calls AcceptWhatsAppCall with that SDP
4. LiveKit bridges the call into a room and dispatches the agent
5. on hangup, DisconnectWhatsAppCall ends the WhatsApp leg
```

### Setup

Meta App Dashboard, in this order:

1. **WhatsApp -> API Setup** -> add your number under *Manage phone number list*
   and verify the code (test numbers reach at most 5 verified recipients)
2. **Phone number call settings** -> enable calling and set `call_hours`
3. **Configuration -> Webhooks** -> callback URL `<your-url>/webhook`, plus the
   verify token from `.env`
4. **Webhook fields -> Manage** -> subscribe to **`calls`**
5. **Subscribe your app to the WABA** (see below)

> **The step that is easy to miss.** Meta has two separate subscription layers.
> The webhook-fields toggle is app-level; the app must *also* be subscribed to
> the WhatsApp Business Account:
>
> ```bash
> curl -X POST "https://graph.facebook.com/v25.0/<WABA_ID>/subscribed_apps" \
>   -H "Authorization: Bearer $WHATSAPP_ACCESS_TOKEN"
> ```
>
> Without it, verification passes, manual tests appear to work, and real call
> events are silently delivered only to Meta's own internal app. Check with
> `GET /<WABA_ID>/subscribed_apps` - your app id must be listed.

### Running the webhook locally

```bash
uv run python -m voice_agent.whatsapp     # listens on :8000
ngrok http 8000                           # public URL for Meta
```

Set `WHATSAPP_APP_SECRET` to enable signature verification. Without it the code
warns on every request and accepts anything, which is acceptable behind a
temporary ngrok URL and not acceptable on a public one.

### Direction support

| Direction | Works? |
|---|---|
| User calls the agent | **Yes**, everywhere Cloud API is available |
| Agent calls a user | Not from a **US** business number. Meta excludes US, Canada, Egypt, Vietnam and Nigeria from business-initiated calling |

---

## Answering a real phone call

LiveKit Phone Numbers are **US only and inbound only** as of August 2026
([docs](https://docs.livekit.io/telephony/start/phone-numbers/)). Outbound and
international are on their roadmap. For outbound or an Indian number you would
bring your own SIP carrier (Twilio, Telnyx, Plivo and Wavix are supported).

```bash
# 1. Buy a number
lk number search --country-code US
lk number purchase --numbers +1XXXXXXXXXX

# 2. Create a dispatch rule (livekit/dispatch-rule.json is in this repo)
lk sip dispatch create livekit/dispatch-rule.json

# 3. Bind the number to the rule
lk number update --id <PHONE_NUMBER_ID> --sip-dispatch-rule-id <RULE_ID>
```

Set `LIVEKIT_AGENT_NAME` in `.env` to match the `agentName` in the dispatch rule.
SIP uses *explicit* dispatch, so the name must match exactly.

> **Known issue:** step 3 currently fails with
> `twirp error invalid_argument: Failed to update phone number`. The request is
> well formed and the CLI is current; this is a LiveKit-side bug. Workaround:
> assign it in the dashboard instead, under Phone Numbers → ⋮ → *Assign dispatch
> rule*.

The free tier includes 1 US number and 50 inbound minutes.

---

## Outbound calls (the agent dials out)

```bash
uv run python scripts/make_call.py +91XXXXXXXXXX
```

It creates a room, dispatches the agent into it *before* dialling (otherwise the
person answers to silence), then places the call through the LiveKit outbound
trunk.

The agent detects the direction from the dispatch metadata and opens
differently: on an outbound call it says who is calling and asks whether it is a
good moment, rather than "thanks for calling".

**Current status: blocked for India.** The Plivo trunk (`ST_FrRASYQEFG9C`) is
configured and LiveKit accepts the call, but Plivo rejects it:

```
SIP call failed: 403 Barred Country (permission_denied)
```

India is gated behind Plivo's Enterprise plan. The trunk and script are
carrier-agnostic - switching provider is one `lk sip outbound create` with a new
termination domain and credentials, with no code change.

---

## What the agent does

It is a receptionist for a software studio. It can:

- Answer questions about services, past work, process, timelines and support,
  from a knowledge base of 16 FAQs
- **Never quote a price.** Pricing depends on scope and is deliberately deferred
  to a call
- **Book a callback**, collecting name, day and time on the call, then writing it
  to CSV. It never asks for a phone number or email: on a phone call we already
  have the number and it is recorded automatically.
- Take a message when no time is agreed
- Speak English, Hindi and Hinglish, matching whatever the caller uses
- **End the call itself** when the conversation is finished

### Opening the call

The first line is spoken verbatim, straight from `business/profile.json`:

> Thank you for calling WebsiNova Technologies. My name is Emma. How may I help
> you today?

It is `session.say()`, not `generate_reply()`. Letting the model compose the
greeting meant it changed slightly on every call, and it charged the caller an
LLM round trip plus TTS before hearing anything at all. Edit `greeting_inbound`
or `greeting_outbound` in the profile to change it; the prompt is told what was
already said so the agent does not introduce itself twice.

### Questions it will not answer

The prompt carries an explicit out-of-scope section: health and medication,
legal and financial advice, programming help, general knowledge, personal
questions, requests to reveal its own instructions, abuse, and wrong numbers.
Each gets one warm sentence and a redirect, never a refusal that ends the call.

The health rule is the strict one. The agent never comments on medication or
symptoms under any framing, and points to a pharmacist or doctor instead.
Having built an app for a health-tech client is not medical standing, and the
prompt says so in as many words.

### Ending the call

The agent hangs up on its own using LiveKit's `EndCallTool`. This is **not** a
timer or a silence threshold: the model calls an `end_call` tool when it judges
the conversation is done, exactly as it decides to call `book_callback`.

Two pieces of text drive that decision: LiveKit's built-in tool description, and
the "Ending the call" section of `prompts/receptionist.md`. Tune the behaviour
there, not in code.

Configuration, in `agents/receptionist.py`:

| Setting | Effect |
|---|---|
| `end_instructions` | The closing line the model is asked to produce |
| `ignore_on_enter=True` | Hides the tool during the greeting, so it cannot hang up before the caller speaks |
| `delete_room=True` | Disconnects remote participants |

**Deleting the room does not end a WhatsApp call.** LiveKit's docs: *"You must
call this API for both business-initiated and user-initiated disconnects... If
you don't call DisconnectWhatsAppCall after a user hangs up, LiveKit
automatically cleans up the call after 30 seconds."* So:

- **Agent hangs up** -> the agent issues a `BUSINESS_INITIATED` disconnect on
  shutdown (this needs `WHATSAPP_ACCESS_TOKEN` in the agent's secrets)
- **Caller hangs up** -> Meta sends a `terminate` webhook and the webhook issues
  a `USER_INITIATED` disconnect, freeing the room instead of idling for 30s

`WHATSAPP_HANGUP_GRACE_SECONDS` (default 2.0) delays the hangup after the
session closes. Audio already handed to the transport is still travelling to the
caller; disconnecting immediately clips the closing line. Raise it if the
goodbye still sounds cut off.

Note that `user_away_timeout` (15s) only marks the user as *away* - it never
ends a session. Silence alone will not hang up.

### Where calls end up

```
data/leads.csv                              # one row per lead (gitignored)
data/transcripts/<timestamp>_<room>.json    # full conversation per call
```

`leads.csv` columns:

```
timestamp_utc, kind, name, contact, reason,
preferred_date, preferred_time, raw_request, caller_number, room
```

`kind` is `booking` or `message`. `raw_request` stores what the caller literally
said about timing ("next Tuesday afternoon") next to the resolved
`preferred_date`, so a human can check how the model interpreted it. Speech to
date is where language models fail quietly.

CSV rather than `.xlsx` deliberately: no dependency, opens directly in Excel, and
appending cannot corrupt earlier rows the way rewriting a whole workbook can.
Each call runs in its own OS process, so writes are serialised with an `fcntl`
lock. If the columns ever change, the old file is rotated to
`leads.legacy-<timestamp>.csv` rather than being appended to with mismatched
fields.

---

### What the agent says, versus what it speaks

LLM output passes through `tts_text_transforms` before the TTS sees it:
`filter_markdown` and `filter_emoji` (the library's own), then
`text_filters.strip_parentheticals`.

The last one exists because `gpt-oss-120b` habitually appends a parenthetical
gloss - "We can help with that (the caller sounds interested in mobile)". Every
character of that is spoken aloud, including the half that was clearly the
model talking to itself about the caller. The filter removes any bracketed span,
tracks nesting, and tidies the spacing so "that (aside)." does not become
"that .".

It works on a live token stream, so a bracket split across chunks is the normal
case, not an edge case. If a bracket never closes it gives up after 200
characters and speaks the text anyway - going silent for the rest of a reply is
the worse failure.

The transcript keeps the original, unfiltered text. That is deliberate: it is
how you tell what the model actually produced.

## Configuration

Everything is environment variables. Full list in `.env.example`.

### Language

One variable sets STT model, language hints and TTS provider together, so they
cannot drift apart:

| `LANGUAGE_PROFILE` | STT | TTS | Extra key |
|---|---|---|---|
| `hinglish` (default) | `flux-general-multi`, hints en+hi | Rumik `mulberry` | `RUMIK_API_KEY` |
| `english` | `flux-general-en` | Deepgram `aura-2-andromeda-en` | none |

Rumik `mulberry` handles English fine, so `LANGUAGE_PROFILE=english` plus
`TTS_PROVIDER=rumik` is a valid combination if you want an English-only agent
with the Rumik voice.

Asking for Hindi with an English-only TTS fails at startup rather than producing
garbled audio mid-call.

### Switching models

Any of these override the profile:

```bash
TTS_PROVIDER=deepgram | rumik | cartesia
TTS_MODEL=...            # defaults per provider, so usually leave it unset
STT_MODEL=flux-general-multi | flux-general-en | nova-3
LLM_MODEL=openai/gpt-oss-120b | openai/gpt-oss-20b
```

Each provider has its own default model, so `TTS_PROVIDER=rumik` alone is enough.

`pricing.py` carries rates for both `gpt-oss` models. Any other model the Groq
plugin accepts will run, but the session summary reports it as unpriced until
you add a rate for it.

### Voice

```bash
RUMIK_SPEAKER=ira        # default. female: emma mia sophia ava ira siya aisha zoya
                         #          male:   lucas noah theo adam
RUMIK_DESCRIPTION=...    # alternative: describe a voice in words instead
```

**Rumik must always be pinned to a voice, and the code makes sure it is.** If
neither `speaker` nor `description` is sent, Rumik generates a voice from
scratch, and because those fields go out on every request the voice changes
between utterances. In testing that produced an English greeting in a female
voice and the next Hindi reply in a male one. A misspelled speaker name has the
same effect, so the code warns when it does not recognise one.

### Turn-taking

```bash
TURN_DETECTION=auto             # auto | stt | vad | livekit
INTERRUPTION_MODE=auto          # auto | vad | adaptive
STT_EOT_THRESHOLD=0.7           # 0.5-0.9. Lower replies sooner, risks cutting people off
STT_EAGER_EOT_THRESHOLD=0.4     # early signal so the LLM starts sooner. 0 disables
LLM_MAX_TOKENS=200              # cap reply length
```

`auto` picks whatever needs no LiveKit credentials, so console mode works
standalone. With Flux that means `stt`, which is also the fastest option.

`STT_EOT_THRESHOLD` is the dial that most changes how the conversation *feels*.

**`STT_EAGER_EOT_THRESHOLD` is the biggest latency lever here.** Deepgram signals
a likely end of turn at this confidence, so the LLM starts generating before the
caller has finished and the reply is often already streaming by the time they
stop. The plugin leaves it **off** unless set. It must be less than or equal to
`STT_EOT_THRESHOLD`, and lower values start sooner at the cost of more discarded
speculative work.

`LLM_MAX_TOKENS` caps reply length. A rambling answer is slow to generate and
slow to speak, which on a call reads as the agent being sluggish.

### Storage

```bash
DATA_DIR=./data          # leads.csv and transcripts/ land here
SAVE_TRANSCRIPTS=true
USD_INR=88               # only affects the rupee figure in the cost summary
```

The full list of every variable is in `.env.example`.

---

## Reading the latency logs

Every agent turn logs a breakdown:

```
turn latency | e2e=740ms eot=210ms llm_ttft=280ms tts_ttfb=190ms playback=60ms
```

| Field | Meaning | Healthy |
|---|---|---|
| `e2e` | Caller stopped talking to agent started talking. The number they feel. | under 900 ms |
| `eot` | Time to decide the turn ended | 200-300 ms |
| `llm_ttft` | LLM time to first token | under 400 ms |
| `tts_ttfb` | TTS time to first audio byte | under 250 ms |
| `playback` | Buffer to speaker | small |

**These will not sum to `e2e`, and that is correct.** Preemptive generation is
on, so the LLM starts before end-of-turn is confirmed and the stages overlap.

Watch the worst turns, not the average. A pipeline that sits at 700 ms but spikes
to 3 s once in twenty turns feels broken, and the mean hides it.

---

## Session cost and usage

When a call ends - caller hangs up, the LiveKit console stops the session, or
`Ctrl+C` in terminal mode - a summary is printed:

```
==================================================================
  SESSION SUMMARY
==================================================================
  Duration        3m 5s

  STT  deepgram:flux-general-multi
      3m 2s of audio                                   $0.02366
  LLM  groq:openai/gpt-oss-120b
      14,200 in (9,800 cached) + 640 out tokens        $0.00178
  TTS  rumik:mulberry
      1,840 characters, 1m 28s spoken                  $0.00920

------------------------------------------------------------------
  TOTAL           $0.0346   (about Rs 3.05)
==================================================================
```

Token counts and audio durations come from LiveKit's own `session.usage`, not
from estimates. Prices are provider list rates in `src/voice_agent/pricing.py`,
each annotated with its source and the date checked (currently 2026-08-19):

| Model | Rate | Source |
|---|---|---|
| Deepgram `flux-general-multi` | $0.0078 / min | deepgram.com/pricing |
| Groq `openai/gpt-oss-120b` | $0.15 / $0.60 per Mtok, cached input half | console.groq.com/docs/models |
| Rumik `mulberry` | $0.005 / 1k chars | rumik.ai/silk-api |
| Deepgram `aura-2-*` | $0.030 / 1k chars | deepgram.com/pricing |

Cached LLM input is billed at half rate and counted separately, because on a
voice agent the system prompt dominates input tokens and is nearly all cached.
Ignoring that overstates LLM cost by roughly 2x.

**What the total excludes:** LiveKit Cloud agent minutes and any telephony
charges. Both are usage-based on their own bills and are not per-model costs.
Volume tiers are not applied either, so treat the number as a close estimate.

Set `USD_INR` to get accurate rupee figures; it defaults to an approximate rate.

The same figures are written into each transcript JSON as `duration_seconds`,
`estimated_cost_usd` and a `usage` breakdown.

---

## Project structure

```
voice-agent/
├── agent.py                   entrypoint shim - `lk` looks for this
├── Dockerfile                 LiveKit Cloud build (3 fixes, see Running it hosted)
├── livekit.toml               pins the deployed agent id
├── livekit/
│   ├── dispatch-rule.json     inbound SIP dispatch rule
│   └── outbound-trunk.json    outbound trunk config (no secrets)
├── scripts/make_call.py       place an outbound call
├── deploy/webhook/            SEPARATE Vercel project for the WhatsApp webhook
│   ├── main.py                self-contained FastAPI app
│   ├── wa/payload.py          Meta payload parser
│   └── requirements.txt       3 deps only, keeps the bundle small
├── src/voice_agent/
│   ├── main.py                AgentServer, prewarm, entrypoint, shutdown
│   ├── session.py             pipeline: turn detection, interruption, endpointing
│   ├── config.py              env -> typed Settings, fails loudly and early
│   ├── pricing.py             list prices, each with source and date checked
│   ├── providers/             the ONLY place models are constructed
│   │   ├── stt.py             STTv2 for Flux models, STT otherwise
│   │   ├── llm.py
│   │   ├── tts.py             deepgram | rumik | cartesia
│   │   └── vad.py
│   ├── agents/receptionist.py the agent: instructions, tools, EndCallTool
│   ├── prompts/receptionist.md behaviour, booking flow, when to end the call
│   ├── business/
│   │   ├── profile.py         loads the two JSON files below
│   │   ├── profile.json       business facts
│   │   └── faq.json           16 spoken-style Q&A pairs
│   ├── whatsapp/
│   │   ├── webhook.py         local dev webhook (FastAPI)
│   │   ├── payload.py         Meta `calls` payload parser
│   │   ├── disconnect.py      hangs up the WhatsApp leg
│   │   └── __main__.py        `python -m voice_agent.whatsapp`
│   ├── storage/
│   │   ├── leads.py           CSV append, locked, schema-rotating
│   │   └── transcripts.py     per-call JSON
│   └── observability/
│       ├── metrics.py         per-turn latency breakdown
│       └── usage.py           session cost summary, printed on hangup
└── tests/                     143 tests, no network calls
```

**The one structural rule:** models are constructed in `providers/` and nowhere
else. That is what makes a provider swap a one-line environment change, which
matters because this layer of the stack turns over fast.

---

## Editing what the agent says

No Python required.

| File | Contains |
|---|---|
| `business/profile.json` | Business facts: services, hours, case studies, pricing policy |
| `business/faq.json` | Q&A pairs. Written to be *spoken*: short, no lists, no markup |
| `prompts/receptionist.md` | Behaviour: tone, booking flow, hard rules |

`{placeholders}` in the prompt are filled from `profile.json`. Reference one that
does not exist and you get an error at startup rather than the agent reading
`{missing_key}` aloud.

> The demo content is grounded in real projects but written for a portfolio
> demo. Both JSON files carry a `_comment` marking this. Review every line before
> using it with a real prospect.

### Adding a tool

Add an `async` method decorated with `@function_tool` to `ReceptionistAgent`. The
docstring becomes the description the model uses to decide when to call it, and
the type hints become the argument schema. See `book_callback`.

---

## Development

```bash
uv run pytest          # 143 tests, no network calls
uv run ruff check .    # lint
```

Direct invocation without `lk`, if needed:

```bash
uv run python -m livekit.agents start --dev agent.py
```

---

## Why a framework, rather than wiring it yourself

The naive model is mic to STT to LLM to TTS to speaker. If that were the job, a
couple of hundred lines would do it. What sits between those boxes is the real
work:

- **Turn-taking.** Has the caller finished, or paused mid-thought? Too slow feels
  sluggish, too fast interrupts them.
- **Barge-in.** When someone talks over the agent you must stop playback, flush
  queued audio, cancel the in-flight LLM stream, and truncate history to what
  they actually *heard*. Get that last part wrong and the agent's memory silently
  diverges from reality.
- **Streaming across three async boundaries.** Partial transcripts, streamed
  tokens, sentence-chunked synthesis, all paced to real time, each able to stall
  independently.
- **Audio plumbing.** Resampling (telephony is 8 kHz), jitter, packet loss.

You would get a demo working in a day, then spend months rediscovering these one
at a time. That is why LiveKit Agents and Pipecat exist as separate projects.

---

## Known limitations

- **Outbound calling to India is blocked.** Plivo gates India behind Enterprise
  (`403 Barred Country`). Every route to an Indian number needs Indian business
  KYC (DoT regulation), which is provider-independent, so switching CPaaS does
  not avoid it.
- **LiveKit phone numbers are US-only and inbound-only.** Outbound needs a BYO
  SIP trunk.
- **WhatsApp business-initiated calls need a non-US business number.** Meta
  excludes US, Canada, Egypt, Vietnam and Nigeria. User-initiated calls work
  everywhere Cloud API is available, which is the direction a receptionist needs.
- **The deployed agent's `data/` is ephemeral.** Leads captured by the cloud
  agent are lost on restart or redeploy. Persisting them needs an external sink.
- **The WhatsApp webhook exists in two copies** - `src/voice_agent/whatsapp/`
  for local development and `deploy/webhook/` for Vercel, kept dependency-light
  on purpose. Changes must be applied to both; a test asserts both handle
  `terminate`.
- **`caller_number` capture is unverified for SIP.** The SDK exposes no constant
  for the SIP caller attribute, so the code scans participant attributes for
  something phone-shaped. The WhatsApp path passes the call id explicitly and is
  exercised.
- **Sarvam is not wired in.** Deepgram Flux covers Hindi but no other Indian
  language, so Tamil, Telugu, Marathi and Bengali are out of scope for now.
- **The Meta app is on Cloud API v26.0**, while LiveKit documents support for
  v23.0-v25.0. We send `25.0`. Not currently causing problems, but worth knowing
  if calls start failing at the accept step.
