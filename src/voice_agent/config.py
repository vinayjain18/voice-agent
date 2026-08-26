"""Typed settings loaded from the environment.

Every provider credential and model choice funnels through here so that a
missing key fails loudly at startup instead of halfway through a call.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent.parent


class ConfigError(RuntimeError):
    """Raised when the environment is missing something we cannot run without."""


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _require(name: str, *, hint: str) -> str:
    value = _env(name)
    if not value:
        raise ConfigError(f"{name} is not set. {hint}")
    return value


def _csv(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    if not raw:
        return default
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass(frozen=True)
class STTSettings:
    provider: str = "deepgram"
    # flux-general-multi is the multilingual Flux model; flux-general-en is English-only.
    model: str = "flux-general-multi"
    # Only honoured by flux-general-multi. Biases the model toward these languages.
    language_hints: list[str] = field(default_factory=lambda: ["en", "hi"])
    # End-of-turn confidence required to close a turn. Deepgram default 0.7, range 0.5-0.9.
    eot_threshold: float = 0.7
    # Confidence at which Deepgram emits an EARLY end-of-turn signal so the LLM
    # can start generating before the turn is confirmed. Disabled by the plugin
    # unless set, and it is one of the largest latency wins available: the LLM
    # is already producing tokens by the time the caller actually stops. Must be
    # <= eot_threshold. Set to 0 to disable.
    eager_eot_threshold: float = 0.4
    # Silence (ms) before a turn is force-closed. Deepgram default 3000.
    eot_timeout_ms: int = 3000


@dataclass(frozen=True)
class LLMSettings:
    provider: str = "groq"
    model: str = "openai/gpt-oss-120b"
    temperature: float = 0.4
    # Hard cap on reply length. A voice reply should be one or two sentences;
    # capping it stops the model rambling, which is both slow to speak and bad
    # on a call. Roughly 200 tokens is 150 words, well above a normal reply.
    max_completion_tokens: int = 200


# Default model per TTS provider. Without this, switching TTS_PROVIDER while
# leaving TTS_MODEL alone hands one provider another provider's model name.
TTS_DEFAULT_MODELS = {
    "deepgram": "aura-2-andromeda-en",
    "rumik": "mulberry",
    "cartesia": "sonic-3",
}

# Which TTS providers/models can actually speak languages other than English.
# Deepgram's Aura family has no Hindi voice at all - the whole reason Rumik is
# here.
ENGLISH_ONLY_TTS = {"deepgram"}


@dataclass(frozen=True)
class TTSSettings:
    provider: str = "deepgram"
    model: str = "aura-2-andromeda-en"
    # Rumik-only knobs, ignored by other providers.
    rumik_speaker: str | None = None
    rumik_description: str | None = None
    # Cartesia-only.
    cartesia_voice: str | None = None


@dataclass(frozen=True)
class StorageSettings:
    """Where captured leads and transcripts land."""

    data_dir: Path = PROJECT_ROOT / "data"
    save_transcripts: bool = True

    @property
    def leads_file(self) -> Path:
        return self.data_dir / "leads.csv"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"


@dataclass(frozen=True)
class WhatsAppSettings:
    """Meta WhatsApp Cloud API credentials for the call webhook.

    Only the webhook needs these; the agent itself is unaware that a call
    arrived over WhatsApp rather than SIP.
    """

    phone_number_id: str | None = None
    access_token: str | None = None
    verify_token: str | None = None
    app_secret: str | None = None
    cloud_api_version: str = "25.0"
    agent_name: str = "voice-agent-demo"
    room_prefix: str = "whatsapp"
    # accept_whatsapp_call can block until the agent joins. Meta times webhooks
    # out, so on a cold-starting agent this may need to be False.
    wait_until_answered: bool = True
    # Seconds to wait after the session closes before hanging up the WhatsApp
    # leg. Audio already handed to the transport is still travelling to the
    # caller's phone; cutting the call immediately clips the closing line.
    hangup_grace_seconds: float = 2.0

    @property
    def is_configured(self) -> bool:
        return bool(self.phone_number_id and self.access_token and self.verify_token)


@dataclass(frozen=True)
class LanguageProfile:
    """A coherent STT + TTS + turn-detection combination.

    Set as one variable so the parts cannot drift apart - the Phase 1 defect was
    a prompt promising Hindi over an English-only voice.
    """

    name: str = "hinglish"

    @property
    def spoken_languages(self) -> str:
        if self.name == "english":
            return "English"
        return "English and Hindi, including natural Hinglish code-switching"

    @property
    def needs_multilingual_tts(self) -> bool:
        return self.name != "english"


LANGUAGE_PROFILES = {
    # profile: (stt_model, stt_hints, tts_provider)
    "english": ("flux-general-en", ["en"], "deepgram"),
    "hinglish": ("flux-general-multi", ["en", "hi"], "rumik"),
}


@dataclass(frozen=True)
class LiveKitSettings:
    """Which LiveKit server the worker registers with.

    `LIVEKIT_MODE=cloud` uses the standard LIVEKIT_* vars. `local` targets a
    `livekit-server --dev` instance, whose credentials are fixed and public by
    design - they are dev-only defaults, not secrets.
    """

    mode: str = "cloud"
    url: str | None = None
    api_key: str | None = None
    api_secret: str | None = None

    @property
    def is_local(self) -> bool:
        return self.mode == "local"

    @property
    def is_configured(self) -> bool:
        """Credentials are present - enough to register a worker."""
        return bool(self.url and self.api_key and self.api_secret)

    @property
    def has_cloud_inference(self) -> bool:
        """True only for LiveKit Cloud.

        The turn detector and adaptive interruption detector are Cloud
        *inference* services. A self-hosted `livekit-server --dev` is media
        only - it has no inference gateway - so having credentials is not
        enough; they must be Cloud credentials.
        """
        return self.mode == "cloud" and self.is_configured


@dataclass(frozen=True)
class PipelineSettings:
    """Turn-taking behaviour.

    Both default to "auto", which picks the option that needs no LiveKit
    credentials - so console mode works standalone. With LiveKit configured you
    can switch to their cloud models and compare.
    """

    # auto | stt | vad | livekit
    turn_detection: str = "auto"
    # auto | vad | adaptive
    interruption_mode: str = "auto"


@dataclass(frozen=True)
class Settings:
    stt: STTSettings
    llm: LLMSettings
    tts: TTSSettings
    livekit: LiveKitSettings
    pipeline: PipelineSettings
    storage: StorageSettings
    language: LanguageProfile
    whatsapp: WhatsAppSettings
    log_metrics: bool

    def preflight(self) -> None:
        """Resolve every credential the selected providers need.

        Called before the worker starts so a missing key is a one-line message
        at launch rather than a stack trace once a call is already connected.
        """
        needed = {self.stt.provider, self.llm.provider, self.tts.provider}
        for provider in sorted(needed):
            require_api_key(provider)
        self.validate_language_support()

    def validate_language_support(self) -> None:
        """Refuse a pipeline that promises a language it cannot speak.

        The Phase 1 defect: the prompt told the agent to reply in Hindi while
        the TTS was English-only, so Hindi replies came out mangled. Fail at
        startup instead.
        """
        if not self.language.needs_multilingual_tts:
            return
        if self.tts.provider in ENGLISH_ONLY_TTS:
            raise ConfigError(
                f"LANGUAGE_PROFILE={self.language.name} needs a TTS that can speak "
                f"Hindi, but TTS_PROVIDER={self.tts.provider} is English-only "
                f"(Deepgram Aura has no Hindi voice). Use TTS_PROVIDER=rumik, or "
                f"set LANGUAGE_PROFILE=english."
            )

    @classmethod
    def load(cls) -> Settings:
        load_dotenv(PROJECT_ROOT / ".env")

        profile_name = _env("LANGUAGE_PROFILE", "hinglish").lower()
        if profile_name not in LANGUAGE_PROFILES:
            raise ConfigError(
                f"LANGUAGE_PROFILE must be one of "
                f"{', '.join(sorted(LANGUAGE_PROFILES))}. Got '{profile_name}'."
            )
        profile_stt_model, profile_stt_hints, profile_tts_provider = LANGUAGE_PROFILES[
            profile_name
        ]
        # An explicit TTS_PROVIDER always beats the profile.
        tts_provider = _env("TTS_PROVIDER", profile_tts_provider)

        return cls(
            stt=STTSettings(
                provider=_env("STT_PROVIDER", "deepgram"),
                model=_env("STT_MODEL", profile_stt_model),
                language_hints=_csv("STT_LANGUAGE_HINTS", profile_stt_hints),
                eot_threshold=float(_env("STT_EOT_THRESHOLD", "0.7")),
                eager_eot_threshold=float(_env("STT_EAGER_EOT_THRESHOLD", "0.4")),
                eot_timeout_ms=int(_env("STT_EOT_TIMEOUT_MS", "3000")),
            ),
            llm=LLMSettings(
                provider=_env("LLM_PROVIDER", "groq"),
                model=_env("LLM_MODEL", "openai/gpt-oss-120b"),
                temperature=float(_env("LLM_TEMPERATURE", "0.4")),
                max_completion_tokens=int(_env("LLM_MAX_TOKENS", "200")),
            ),
            tts=TTSSettings(
                provider=tts_provider,
                model=_env("TTS_MODEL", TTS_DEFAULT_MODELS.get(tts_provider, "")),
                rumik_speaker=_env("RUMIK_SPEAKER"),
                rumik_description=_env("RUMIK_DESCRIPTION"),
                cartesia_voice=_env("CARTESIA_VOICE"),
            ),
            storage=StorageSettings(
                data_dir=Path(_env("DATA_DIR", str(PROJECT_ROOT / "data"))),
                save_transcripts=_env("SAVE_TRANSCRIPTS", "true").lower()
                in {"1", "true", "yes"},
            ),
            livekit=_livekit_settings(),
            pipeline=PipelineSettings(
                turn_detection=_env("TURN_DETECTION", "auto").lower(),
                interruption_mode=_env("INTERRUPTION_MODE", "auto").lower(),
            ),
            whatsapp=WhatsAppSettings(
                phone_number_id=_env("WHATSAPP_PHONE_NUMBER_ID"),
                access_token=_env("WHATSAPP_ACCESS_TOKEN"),
                verify_token=_env("WHATSAPP_VERIFY_TOKEN"),
                app_secret=_env("WHATSAPP_APP_SECRET"),
                cloud_api_version=_env("WHATSAPP_CLOUD_API_VERSION", "25.0"),
                agent_name=_env("LIVEKIT_AGENT_NAME", "voice-agent-demo"),
                room_prefix=_env("WHATSAPP_ROOM_PREFIX", "whatsapp"),
                wait_until_answered=_env("WHATSAPP_WAIT_UNTIL_ANSWERED", "true").lower()
                in {"1", "true", "yes"},
                hangup_grace_seconds=float(
                    _env("WHATSAPP_HANGUP_GRACE_SECONDS", "2.0")
                ),
            ),
            language=LanguageProfile(name=profile_name),
            log_metrics=_env("LOG_METRICS", "true").lower() in {"1", "true", "yes"},
        )


# `livekit-server --dev` ships these fixed credentials. Public by design.
LOCAL_LIVEKIT_URL = "ws://127.0.0.1:7880"
LOCAL_LIVEKIT_API_KEY = "devkey"
LOCAL_LIVEKIT_API_SECRET = "secret"


def _livekit_settings() -> LiveKitSettings:
    mode = _env("LIVEKIT_MODE", "cloud").lower()
    if mode not in {"cloud", "local"}:
        raise ConfigError(
            f"LIVEKIT_MODE must be 'cloud' or 'local', got '{mode}'."
        )

    if mode == "local":
        return LiveKitSettings(
            mode=mode,
            url=_env("LIVEKIT_LOCAL_URL", LOCAL_LIVEKIT_URL),
            api_key=_env("LIVEKIT_LOCAL_API_KEY", LOCAL_LIVEKIT_API_KEY),
            api_secret=_env("LIVEKIT_LOCAL_API_SECRET", LOCAL_LIVEKIT_API_SECRET),
        )

    return LiveKitSettings(
        mode=mode,
        url=_env("LIVEKIT_URL"),
        api_key=_env("LIVEKIT_API_KEY"),
        api_secret=_env("LIVEKIT_API_SECRET"),
    )


def require_api_key(provider: str) -> str:
    """Resolve the API key for a provider, with an actionable error message."""
    keys = {
        "deepgram": ("DEEPGRAM_API_KEY", "Get one free at https://console.deepgram.com"),
        "groq": ("GROQ_API_KEY", "Get one free at https://console.groq.com/keys"),
        "rumik": ("RUMIK_API_KEY", "Get one at https://playground.rumik.ai"),
        "cartesia": ("CARTESIA_API_KEY", "Get one at https://play.cartesia.ai"),
    }
    if provider not in keys:
        raise ConfigError(f"Unknown provider '{provider}'.")
    name, hint = keys[provider]
    return _require(name, hint=hint)
