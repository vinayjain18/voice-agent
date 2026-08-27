# Voice Agent

A low-latency voice agent that answers a phone call, holds a real conversation
about your business, and books a callback.

The same code runs four ways with no changes: **your terminal**, **a browser**,
**a real phone number**, and **WhatsApp**.

```
Caller ──► STT ──► LLM ──►     TTS      ──► Caller
        Deepgram   Groq   Deepgram / Rumik
          Flux
```

Built on [LiveKit Agents](https://docs.livekit.io/agents/). It speaks
US-accented English out of the box, and switches to Hindi and Hinglish with one
environment variable. Every provider is swappable the same way.

---

## Table of contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Ways to run it](#ways-to-run-it)
  - [Terminal](#terminal)
  - [Browser](#browser)
  - [Hosted, with no laptop running](#hosted-with-no-laptop-running)
  - [A real phone number](#a-real-phone-number)
  - [WhatsApp](#whatsapp)
- [Configuration](#configuration)
  - [Environment variables](#environment-variables)
  - [Language](#language)
  - [Switching providers](#switching-providers)
  - [Choosing a voice](#choosing-a-voice)
  - [Turn-taking and latency](#turn-taking-and-latency)
- [Making it your agent](#making-it-your-agent)
  - [Business facts](#business-facts)
  - [The prompt](#the-prompt)
  - [Adding a tool](#adding-a-tool)
- [What the agent does on a call](#what-the-agent-does-on-a-call)
- [Where calls end up](#where-calls-end-up)
- [Observability](#observability)
  - [The conversation log](#the-conversation-log)
  - [Latency](#latency)
  - [Cost per call](#cost-per-call)
- [Project structure](#project-structure)
- [Development](#development)
- [Why a framework](#why-a-framework)
- [Limitations](#limitations)
- [License](#license)

---

## Features

- **Sub-two-second replies.** End-of-turn detection runs inside the speech model,
  and the LLM starts generating before the caller has finished speaking.
- **A US-accented English voice out of the box**, and one variable away from
  Hindi and Hinglish with mid-sentence switching.
- **Books callbacks** into a CSV, with the caller's number captured from the call
  rather than asked for.
- **Interruptible.** Talk over it and it stops, like a person would.
- **Knows when to hang up**, and will not cut a caller off mid-sentence.
- **Costs are itemised** per call: tokens, characters, audio seconds, and a total.
- **Everything the agent says is data**, not code. Facts live in JSON, behaviour
  lives in a Markdown prompt.
- **195 tests**, none of which touch the network, so the suite runs in about a
  second.

---

## How it works

```
                    ┌──────────────────────────────────────┐
   phone / browser  │            LiveKit room              │
   / WhatsApp  ────►│  audio in                 audio out  │◄────┐
                    └───────┬──────────────────────────────┘     │
                            │                                    │
                            ▼                                    │
                   ┌─────────────────┐                           │
                   │  Deepgram Flux  │  speech ──► text          │
                   │      (STT)      │  + end-of-turn signal     │
                   └────────┬────────┘                           │
                            ▼                                    │
                   ┌─────────────────┐                           │
                   │      Groq       │  text ──► reply           │
                   │      (LLM)      │  + tool calls             │
                   └────────┬────────┘                           │
                            ▼                                    │
                   ┌─────────────────┐                           │
                   │ Deepgram/Rumik  │  text ──► speech ─────────┘
                   │      (TTS)      │
                   └─────────────────┘
```

Deepgram Flux decides when the caller has stopped talking, so there is no
separate turn-detection model and no extra network round trip. It also emits an
*early* end-of-turn signal, which lets the LLM start generating before the turn
is confirmed. That single setting is the largest latency win in the pipeline.

Silero VAD runs locally alongside, purely to detect barge-in.

---

## Requirements

- **Python 3.13+** (uv installs it for you)
- **[uv](https://docs.astral.sh/uv/)** for dependency management
- **[LiveKit CLI](https://docs.livekit.io/home/cli/)** to run the agent
- API keys, both with free tiers: **Deepgram** and **Groq**. A **Rumik** key is
  needed only for Hindi and Hinglish.
- A **LiveKit Cloud** account, only for the browser, phone and WhatsApp modes.
  Terminal mode needs no LiveKit account at all.

---

## Quick start

**1. Clone and install**

```bash
git clone <your-fork-url> voice-agent
cd voice-agent
uv sync
```

**2. Install the LiveKit CLI**

```bash
brew install livekit-cli                          # macOS
curl -sSL https://get.livekit.io/cli | bash       # Linux
```

**3. Add your API keys**

```bash
cp .env.example .env
```

Then fill in two keys:

| Key | Where to get it |
| --- | --- |
| `DEEPGRAM_API_KEY` | [console.deepgram.com](https://console.deepgram.com) |
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) |

A third, `RUMIK_API_KEY` from [playground.rumik.ai](https://playground.rumik.ai),
is needed only if you switch to Hindi or Hinglish. See
[Language](#language).

**4. Talk to it**

```bash
lk agent console
```

Speak into your microphone. Press `Ctrl+C` to hang up, and a cost summary for
the call prints on the way out.

Prefer typing to talking while you iterate on the prompt:

```bash
lk agent console --text
```

Missing keys fail at startup with a message naming the variable, rather than
halfway through a call.

---

## Ways to run it

### Terminal

```bash
lk agent console          # microphone and speakers
lk agent console --text   # type instead of talk
```

No LiveKit account required. This is the fastest loop for changing the prompt or
the business facts.

### Browser

Register a worker and talk to it from LiveKit's web console.

```bash
lk cloud auth                    # once
lk app env -w .env               # writes LIVEKIT_URL / API key / secret
lk agent dev
```

Open the printed URL and allow microphone access.

<details>
<summary>Running against a local LiveKit server instead of the cloud</summary>

```bash
docker run --rm -p 7880:7880 livekit/livekit-server --dev
```

Set `LIVEKIT_MODE=local` in `.env` and start the agent with `lk agent dev`.

A local server handles media only; it has no inference gateway. Set
`TURN_DETECTION=stt` and `INTERRUPTION_MODE=vad`, which are the defaults anyway.

</details>

### Hosted, with no laptop running

Deploy the agent to LiveKit Cloud so calls are answered whether or not your
machine is on.

```bash
lk agent create                            # first time only
lk agent deploy --secrets-file .env        # every update
lk agent status
lk agent logs                              # live logs
```

Two things to know:

- **Cold starts.** On the free tier an idle agent takes a few seconds to wake, so
  the first call after a quiet period is slower to answer.
- **`data/` is ephemeral.** Leads and transcripts written by the hosted agent are
  lost on restart or redeploy. Send them somewhere durable if you need to keep
  them.

### A real phone number

LiveKit sells numbers directly, or you can bring a SIP trunk from your own
carrier.

```bash
# 1. Buy a number
lk number search --country-code US
lk number purchase --numbers +1XXXXXXXXXX

# 2. Create a dispatch rule (livekit/dispatch-rule.json ships with this repo)
lk sip dispatch create livekit/dispatch-rule.json

# 3. Bind the number to the rule
lk number update --id <PHONE_NUMBER_ID> --sip-dispatch-rule-id <RULE_ID>
```

Set `LIVEKIT_AGENT_NAME` in `.env` to match `agentName` in the dispatch rule.
Telephony uses explicit dispatch, so the two must match exactly.

> If step 3 returns `Failed to update phone number`, assign the rule from the
> LiveKit dashboard instead: **Phone Numbers → ⋮ → Assign dispatch rule**.

**Outbound calls** dial from the same agent:

```bash
uv run python scripts/make_call.py +91XXXXXXXXXX
```

It creates a room and puts the agent in it *before* dialling, so the person
never answers to silence. The agent notices it placed the call and opens by
saying who is calling and asking whether it is a good moment, rather than
thanking them for calling.

Outbound needs a SIP trunk from a carrier. Which countries you can dial, and
what proof of business you need first, is set by the carrier and by local
telecoms regulation, not by this project.

### WhatsApp

Callers ring your WhatsApp Business number and the agent answers.

```
Caller ──► Meta ──► your webhook ──► LiveKit ──► agent
```

The webhook accepts the incoming call, hands the SDP to LiveKit, and LiveKit
bridges the audio into a room the agent joins. The agent itself has no idea the
call came from WhatsApp rather than a phone line.

**Setup**

1. Create a Meta app with the **WhatsApp** product.
2. Enable **voice calling** on your WhatsApp Business number.
3. Deploy the webhook (see below) and set it as the app's callback URL.
4. Subscribe the app to the **`calls`** webhook field.
5. Subscribe the app to your WhatsApp Business Account:
   ```
   POST https://graph.facebook.com/v25.0/{waba-id}/subscribed_apps
   ```
6. Fill in `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`,
   `WHATSAPP_VERIFY_TOKEN` and `WHATSAPP_APP_SECRET` in `.env`.

> **Step 5 is easy to miss and gives no error when skipped.** Subscribing to the
> `calls` field is app-level; the app must *also* be subscribed to the business
> account. Without it, verification passes and manual webhook tests appear to
> work, but real calls never reach you and the phone just rings. Confirm with
> `GET /{waba-id}/subscribed_apps`.

`WHATSAPP_VERIFY_TOKEN` is a string you invent. Meta echoes it back during
verification to prove the endpoint is yours.

**Running the webhook locally**

```bash
uv run python -m voice_agent.whatsapp     # serves on :8000
ngrok http 8000                           # public URL for Meta
```

**Deploying the webhook**

`deploy/webhook/` is a self-contained copy that depends only on FastAPI,
`livekit-api` and `python-dotenv`, so it deploys to any serverless Python host.

```bash
cd deploy/webhook
vercel deploy --prod
```

Set the four `WHATSAPP_*` variables plus `LIVEKIT_URL`, `LIVEKIT_API_KEY` and
`LIVEKIT_API_SECRET` as **project-level** environment variables. Variables
attached to a single deployment do not persist to the next one.

Check it with `GET /` on the deployed URL, which reports whether WhatsApp
credentials and signature verification are configured.

---

## Configuration

Everything is environment variables. `.env.example` documents all of them with
their defaults; the ones you are most likely to change are below.

### Environment variables

| Variable | Default | What it does |
| --- | --- | --- |
| `LANGUAGE_PROFILE` | `english` | `english` (US accent) or `hinglish`. Sets STT, TTS and prompt together. |
| `STT_MODEL` | `flux-general-en` | Deepgram model. The profile sets this. |
| `LLM_MODEL` | `openai/gpt-oss-120b` | Any Groq-hosted model. |
| `LLM_TEMPERATURE` | `0.4` | Higher wanders, lower repeats. |
| `LLM_MAX_TOKENS` | `200` | Caps reply length. A voice reply should be short. |
| `TTS_PROVIDER` | from profile | `deepgram`, `rumik` or `cartesia`. |
| `TTS_MODEL` | `aura-2-asteria-en` | The voice. Aura-2 names carry the accent. |
| `RUMIK_SPEAKER` | `ira` | Rumik voice preset, used only with `TTS_PROVIDER=rumik`. |
| `TURN_DETECTION` | `auto` | `stt`, `vad`, `livekit` or `auto`. |
| `INTERRUPTION_MODE` | `auto` | `vad` or `adaptive`. |
| `STT_EAGER_EOT_THRESHOLD` | `0.4` | Early end-of-turn. The main latency lever. |
| `LIVEKIT_MODE` | `cloud` | `cloud` or `local`. |
| `DATA_DIR` | `./data` | Where leads and transcripts are written. |
| `LOG_TRANSCRIPT` | `true` | Log the conversation as it happens. |
| `LOG_INTERIM_TRANSCRIPT` | `false` | Also log partial transcripts. Noisy. |

### Language

```bash
LANGUAGE_PROFILE=english    # English only, US accent  (default)
LANGUAGE_PROFILE=hinglish   # English + Hindi + Hinglish
```

One variable sets the speech model, its language hints, the voice **and** the
language instructions in the prompt, so no two of them can drift apart.

| | `english` (default) | `hinglish` |
| --- | --- | --- |
| STT | `flux-general-en` | `flux-general-multi` |
| TTS | Deepgram `aura-2-asteria-en` | Rumik `mulberry` |
| Extra key | none | `RUMIK_API_KEY` |
| Prompt says | "You speak English" | "You speak English and Hindi" |

Switching to Hinglish is two lines:

```bash
LANGUAGE_PROFILE=hinglish
RUMIK_API_KEY=your-key
```

The profile picks Rumik on its own, so `TTS_PROVIDER` can stay unset.

Asking for a language the voice cannot speak is refused at startup rather than
producing mangled audio on a live call. Deepgram's Aura voices have no Hindi at
all, which is why the Hinglish profile switches to Rumik.

### Switching providers

```bash
STT_PROVIDER=deepgram
LLM_PROVIDER=groq
TTS_PROVIDER=rumik          # or deepgram, cartesia
```

Every model is built in `providers/`, and nowhere else, so swapping one is a
single environment variable. Each provider has a sensible default model, so you
do not have to set both.

### Choosing a voice

The default voice is **`aura-2-asteria-en`**, an American English voice from
Deepgram's Aura-2 range. To use a different one:

```bash
TTS_MODEL=aura-2-orion-en
```

That is the whole change. Accent is carried by the voice name; there is no
separate accent setting.

**American English voices:**

| | |
| --- | --- |
| Female | `andromeda` `asteria` `aurora` `cordelia` `electra` `harmonia` `hera` `iris` `janus` `juno` `luna` `minerva` `phoebe` `selene` |
| Male | `apollo` `arcas` `aries` `atlas` `hermes` `jupiter` `mars` `neptune` `odysseus` `orion` `orpheus` `pluto` `saturn` `zeus` |

Use them as `aura-2-<name>-en`.

**Other accents:** `aura-2-draco-en` (British male), `aura-2-theia-en`
(Australian female), `aura-2-hyperion-en` (Australian male).

Deepgram publishes the full catalogue with audio samples at
[developers.deepgram.com/docs/tts-models](https://developers.deepgram.com/docs/tts-models).
Any Aura voice name works here, including ones newer than the list above.

#### Voices for Hindi and Hinglish

Aura has no Hindi voice, so `LANGUAGE_PROFILE=hinglish` switches to Rumik. Pick
a preset:

```bash
LANGUAGE_PROFILE=hinglish
RUMIK_API_KEY=your-key
RUMIK_SPEAKER=ira           # female: emma mia sophia ava ira siya aisha zoya
                            # male:   lucas noah theo adam
```

Always leave a voice set. An unpinned Rumik voice is generated fresh on every
request, so it can change between one sentence and the next. `ira` is used if
you set nothing.

Rumik can also generate a voice from a written description:

```bash
RUMIK_SPEAKER=
RUMIK_DESCRIPTION="A warm American woman in her thirties, calm and unhurried"
```

`RUMIK_SPEAKER` **must be empty** for this to apply. A speaker name always wins
over a description, so leaving both set silently ignores the description. A
described voice is also regenerated per request and can drift between
utterances, which is why a named preset is the default.

#### All three providers

| Provider | Voices | Languages |
| --- | --- | --- |
| `deepgram` | Aura-2, dozens of voices with named accents | English only |
| `rumik` | `mulberry` presets, or a written description | English, Hindi, Hinglish |
| `cartesia` | `sonic-3` | English |

### Turn-taking and latency

```bash
TURN_DETECTION=auto         # auto picks the speech model's own detection
INTERRUPTION_MODE=auto      # auto picks the best available for your setup
STT_EAGER_EOT_THRESHOLD=0.4 # 0 disables; must be <= STT_EOT_THRESHOLD
```

`STT_EAGER_EOT_THRESHOLD` is the setting worth understanding. Deepgram emits an
early end-of-turn signal at this confidence, and the LLM starts generating
immediately, so tokens are already flowing when the caller actually stops. It is
disabled unless set.

---

## Making it your agent

Three files hold everything the agent knows and says. None of them are code.

| File | Contains |
| --- | --- |
| `business/profile.json` | Who you are, what you sell, hours, pricing policy |
| `business/faq.json` | Spoken question and answer pairs |
| `prompts/receptionist.md` | How it behaves, and the booking flow |

### Business facts

Edit `profile.json`. Every key is available to the prompt as `{key}`, and lists
are rendered for you. Keys beginning with `_` are treated as editorial comments
and never reach the model.

```json
{
  "business_name": "Acme Ltd",
  "agent_name": "Emma",
  "hours": "Monday to Friday, 9am to 7pm",
  "greeting_inbound": "Thank you for calling Acme. My name is Emma. How may I help you today?",
  "services": ["Web development", "Mobile apps"]
}
```

The greeting is spoken exactly as written, so keep it to one breath.

Content here is read aloud, so write it the way you would say it: no bullet
points, no markdown, no symbols, and use contractions. A test enforces this.

### The prompt

`prompts/receptionist.md` is the agent's behaviour: tone, what it refuses, how
it takes a booking, when it hangs up. A `{placeholder}` with no matching profile
key fails at startup rather than being read out loud.

The most effective part of the file is the worked dialogues at the end. Models
copy the register of an example far more faithfully than they follow a rule, so
if you change a rule, change an example to match.

### Adding a tool

Tools are methods on the agent, decorated and documented. The docstring is what
the model reads to decide when to call it.

```python
@function_tool
async def check_availability(self, context: RunContext, day: str) -> str:
    """Check whether the team has time on a given day.

    Args:
        day: The date as YYYY-MM-DD.
    """
    return "There's space on Thursday afternoon."
```

Return a plain sentence. It goes back to the model, not to the caller, so it can
also be an instruction: returning `"Ask for a time first, then call this again"`
is a reliable way to stop a tool being called too early.

---

## What the agent does on a call

- **Opens** with a fixed greeting from `profile.json`, spoken immediately rather
  than generated, so there is no delay before the caller hears anything.
- **Answers** from the FAQ and profile, and says it will follow up rather than
  guessing when it does not know.
- **Books a callback** once it has a name, a day and a time, and confirms the
  time back in the caller's own words rather than converting it.
- **Never asks for a phone number or email.** The number comes from the call.
- **Declines** health, legal, financial and programming questions, and points to
  the right kind of professional instead.
- **Handles the calls a business line actually gets**: sales calls, job
  applications, existing clients, wrong numbers.
- **Hangs up** only after asking whether there is anything else *and* hearing the
  answer. If the caller is mid-sentence, it stays on the line.

Everything above is prompt and configuration. None of it requires code changes.

---

## Where calls end up

**Leads** land in `data/leads.csv`, one row per booking or message:

```
timestamp_utc,kind,name,contact,reason,preferred_date,preferred_time,raw_request,caller_number,room
```

`raw_request` keeps what the caller actually said about timing ("today at two,
your time"), so a human can sanity-check the parsed date. Writes are locked, so
concurrent calls cannot interleave rows, and a failure to save is logged rather
than allowed to crash a live call.

Changing the columns rotates the old file to `leads.legacy-<timestamp>.csv`
automatically, because appending new-shaped rows under an old header silently
misaligns every column.

**Transcripts** are written to `data/transcripts/<timestamp>_<room>.json`, with
the full conversation, the duration and the itemised cost. Set
`SAVE_TRANSCRIPTS=false` to turn them off.

`data/` is gitignored.

---

## Observability

### The conversation log

Every call is logged as it happens:

```
<< agent | Thank you for calling Acme. My name is Emma. How may I help you today?
>> user  | Hi, do you build mobile apps?  [en]
<< agent | We do, yeah. iOS and Android both. What are you looking to build?  [waited 1.2s]
-- tool  | book_callback(name='Ravi' preferred_time='17:30') -> Saved. Now say it back...
!! user  | 1.4s of speech produced no transcript
```

| Prefix | Meaning |
| --- | --- |
| `>> user` | What the speech model finally settled on |
| `<< agent` | What the LLM produced, and how long the caller waited for it |
| `-- tool` | Which tool ran, with what arguments, and what it returned |
| `!! user` | Speech was heard but produced no transcript |

The agent line carries the measured wait because the line is written when the
reply is *committed*, which is after it has finished playing. Timing a call by
the log timestamps alone would overstate latency by the length of every reply.

Turn it off with `LOG_TRANSCRIPT=false` if you would rather not put caller words
into your log provider.

### Latency

```
turn latency | e2e=740ms eot=210ms llm_ttft=280ms tts_ttfb=190ms playback=60ms
```

| Field | Meaning |
| --- | --- |
| `e2e` | Caller stopped speaking → agent started speaking |
| `eot` | Deciding the caller had finished |
| `llm_ttft` | Time to the first token |
| `tts_ttfb` | Time to the first audio byte |
| `playback` | Buffering before audio starts |

The fields do not add up to `e2e`, because generation overlaps end-of-turn
detection. That is the point of it.

### Cost per call

Printed when the call ends:

```
  SESSION SUMMARY
  Duration        1m 8s

  LLM  groq:openai/gpt-oss-120b
      22,853 in (10,496 cached) + 439 out tokens       $0.00290
  TTS  Rumik AI:mulberry
      631 characters, 44s spoken                       $0.00316
  STT  Deepgram:flux-general-multi
      1m 4s of audio                                   $0.00843

  TOTAL           $0.0145   (about Rs 1.28)
```

Prices live in `pricing.py`, each with a source and the date checked. Cached LLM
input is billed at half rate and counted separately, because on a voice agent the
system prompt dominates input tokens and is almost entirely cached. Ignoring that
overstates LLM cost by roughly double.

LiveKit and telephony minutes are not included.

---

## Project structure

```
voice-agent/
├── src/voice_agent/
│   ├── main.py                  Agent server, entrypoint, shutdown
│   ├── config.py                Typed settings from the environment
│   ├── session.py               Pipeline assembly and turn-taking
│   ├── text_filters.py          Cleans LLM output before it is spoken
│   ├── pricing.py               Provider list prices, with sources
│   ├── agents/
│   │   └── receptionist.py      The agent, its tools, and its guards
│   ├── prompts/
│   │   └── receptionist.md      Behaviour and booking flow
│   ├── business/
│   │   ├── profile.json         Business facts
│   │   └── faq.json             Spoken question and answer pairs
│   ├── providers/               The only place models are constructed
│   │   ├── stt.py  llm.py  tts.py  vad.py
│   ├── storage/
│   │   ├── leads.py             CSV, locked, with schema rotation
│   │   └── transcripts.py       Per-call JSON
│   ├── observability/
│   │   ├── conversation.py      The conversation log
│   │   ├── metrics.py           Per-turn latency
│   │   └── usage.py             Cost summary
│   └── whatsapp/                Webhook for local development
├── deploy/webhook/              Self-contained webhook for serverless
├── livekit/                     Dispatch rule and trunk config
├── scripts/make_call.py         Place an outbound call
├── tests/                       195 tests, no network calls
└── .env.example                 Every setting, documented
```

Two rules keep this navigable. **Models are only ever constructed in
`providers/`**, which is what makes a provider swap one environment variable.
And **`livekit.plugins` imports stay at module scope**, because plugin
registration must happen on the main thread; a test enforces this statically.

---

## Development

```bash
uv sync                  # install
uv run pytest            # 195 tests, about a second, no network calls
uv run ruff check .      # lint
```

The test suite is the verification gate. It never touches the network, and it
reads a fixture environment rather than your `.env`.

Conventions worth knowing before you send a patch:

- Content the agent speaks has no markdown, no symbols and no dashes, and uses
  contractions. Tests enforce all of it.
- New behaviour gets a test. The suite is fast; keep it that way.
- Comments explain *why*, particularly where code looks odd to work around a
  library behaviour.
- The WhatsApp webhook exists twice, once for local development and once for
  deployment. Changes go in both.

---

## Why a framework

You could wire the providers together yourself. What LiveKit Agents actually
gives you is the part that is tedious to get right:

- **Turn-taking.** Knowing the caller has finished, rather than merely paused.
- **Barge-in.** Stopping mid-word when they interrupt, and discarding the audio
  already queued.
- **Streaming everywhere.** Speech, tokens and audio all overlap. Doing this by
  hand means managing several concurrent streams and their cancellation.
- **Transport.** Jitter buffers, packet loss, echo cancellation, WebRTC.

None of that is business logic, and all of it decides whether a call feels
natural. What this repo adds on top is the agent itself: the prompt, the tools,
the guards on when to hang up, and the accounting.

---

## Limitations

- **Hosted `data/` is ephemeral.** Leads captured by a cloud-deployed agent are
  lost on redeploy. Write them to an external store if you need to keep them.
- **Indian languages beyond Hindi are not supported.** Deepgram Flux covers
  Hindi; Tamil, Telugu, Marathi and Bengali would need a different speech model.
- **The WhatsApp webhook is duplicated** between local and deployment copies, so
  changes must be applied twice. A test checks they agree on call teardown.
- **Caller number capture on SIP is best-effort.** The SDK exposes no constant
  for the caller attribute, so the code scans participant attributes for
  something phone-shaped. The WhatsApp path passes the call id explicitly.
- **Outbound calling depends on your carrier.** Which countries you can dial, and
  what business verification is required first, is set by the carrier and by
  local regulation.
- **There is no CI configuration** in this repo. Run `uv run pytest` and
  `uv run ruff check .` before you commit.

---

## License

[MIT](LICENSE). Use it, change it, ship it.

The business content under `src/voice_agent/business/` is example data for a
specific company and is not covered by any warranty of accuracy. Replace it with
your own before using this with real callers.
