# Voice Agent

A low-latency live voice agent that answers a phone call, holds a real
conversation about the business, and books a callback into a CSV.

Runs three ways from the same code: your terminal, a browser, or a real phone
number.

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

## What the agent does

It is a receptionist for a software studio. It can:

- Answer questions about services, past work, process, timelines and support,
  from a knowledge base of 16 FAQs
- **Never quote a price.** Pricing depends on scope and is deliberately deferred
  to a call
- **Book a callback**, collecting name, contact, day and time on the call, then
  writing it to CSV
- Take a message when no time is agreed
- Speak English, Hindi and Hinglish, matching whatever the caller uses

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
LLM_MODEL=openai/gpt-oss-120b | llama-3.3-70b-versatile | ...
```

Each provider has its own default model, so `TTS_PROVIDER=rumik` alone is enough.

### Turn-taking

```bash
TURN_DETECTION=auto        # auto | stt | vad | livekit
INTERRUPTION_MODE=auto     # auto | vad | adaptive
STT_EOT_THRESHOLD=0.7      # 0.5-0.9. Lower replies sooner, risks cutting people off
```

`auto` picks whatever needs no LiveKit credentials, so console mode works
standalone. With Flux that means `stt`, which is also the fastest option.

`STT_EOT_THRESHOLD` is the dial that most changes how the conversation *feels*.

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
each annotated with its source and the date checked:

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
├── livekit/dispatch-rule.json SIP dispatch rule
├── src/voice_agent/
│   ├── main.py                AgentServer, prewarm, job entrypoint
│   ├── session.py             pipeline decisions: turn detection, interruption
│   ├── config.py              env -> typed Settings, fails loudly and early
│   ├── providers/             the ONLY place models are constructed
│   │   ├── stt.py             picks STTv2 for Flux models, STT otherwise
│   │   ├── llm.py
│   │   ├── tts.py             deepgram | rumik | cartesia
│   │   └── vad.py
│   ├── agents/receptionist.py the agent: instructions + @function_tool methods
│   ├── prompts/receptionist.md behaviour, as an editable template
│   ├── business/
│   │   ├── profile.json       facts about the business
│   │   └── faq.json           16 spoken-style Q&A pairs
│   ├── storage/
│   │   ├── leads.py           CSV append, locked, schema-rotating
│   │   └── transcripts.py     per-call JSON
│   └── observability/metrics.py  per-turn latency breakdown
└── tests/                     61 tests, no network calls
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
uv run pytest          # 61 tests, no network calls
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
