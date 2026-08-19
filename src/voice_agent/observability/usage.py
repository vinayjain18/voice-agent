"""Session usage and cost summary, printed when a call ends.

LiveKit tracks token counts and audio durations per model on
`AgentSession.usage`. This turns that into a readable breakdown with a cost
estimate from the published list prices in `pricing.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from voice_agent import pricing

logger = logging.getLogger("voice_agent.usage")


@dataclass
class LineItem:
    label: str
    detail: str
    cost_usd: float | None  # None means we have no published price for it


@dataclass
class SessionCost:
    duration_seconds: float
    items: list[LineItem] = field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return sum(item.cost_usd or 0.0 for item in self.items)

    @property
    def has_unpriced(self) -> bool:
        return any(item.cost_usd is None for item in self.items)


def _fmt_duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def summarise(usage: Any, duration_seconds: float) -> SessionCost:
    """Turn an AgentSessionUsage into priced line items."""
    result = SessionCost(duration_seconds=duration_seconds)

    for entry in getattr(usage, "model_usage", []) or []:
        kind = getattr(entry, "type", "")
        model = getattr(entry, "model", "?")
        provider = getattr(entry, "provider", "?")

        if kind == "stt_usage":
            audio_s = float(getattr(entry, "audio_duration", 0.0) or 0.0)
            price = pricing.stt_price(model)
            cost = (audio_s / 60.0) * price.per_minute_usd if price else None
            result.items.append(
                LineItem(
                    f"STT  {provider}:{model}",
                    f"{_fmt_duration(audio_s)} of audio",
                    cost,
                )
            )

        elif kind == "llm_usage":
            total_in = int(getattr(entry, "input_tokens", 0) or 0)
            cached_in = int(getattr(entry, "input_cached_tokens", 0) or 0)
            out = int(getattr(entry, "output_tokens", 0) or 0)
            fresh_in = max(0, total_in - cached_in)

            price = pricing.llm_price(model)
            cost = None
            if price:
                cost = (
                    fresh_in * price.input_per_mtok_usd
                    + cached_in * price.cached_input_per_mtok_usd
                    + out * price.output_per_mtok_usd
                ) / 1_000_000

            detail = f"{total_in:,} in ({cached_in:,} cached) + {out:,} out tokens"
            result.items.append(LineItem(f"LLM  {provider}:{model}", detail, cost))

        elif kind == "tts_usage":
            chars = int(getattr(entry, "characters_count", 0) or 0)
            audio_s = float(getattr(entry, "audio_duration", 0.0) or 0.0)
            price = pricing.tts_price(model)
            cost = (chars / 1000.0) * price.per_1k_chars_usd if price else None
            detail = f"{chars:,} characters, {_fmt_duration(audio_s)} spoken"
            result.items.append(LineItem(f"TTS  {provider}:{model}", detail, cost))

        elif kind in {"eot_usage", "interruption_usage"}:
            requests = int(getattr(entry, "total_requests", 0) or 0)
            if requests:
                result.items.append(
                    LineItem(
                        f"{kind.split('_')[0].upper():4} {provider}:{model}",
                        f"{requests:,} requests",
                        0.0,  # bundled into LiveKit Cloud, not billed per call
                    )
                )

    return result


def format_summary(cost: SessionCost) -> str:
    """Render the block that gets printed when the call ends."""
    inr = pricing.usd_to_inr()
    width = 66
    lines = [
        "",
        "=" * width,
        "  SESSION SUMMARY",
        "=" * width,
        f"  Duration        {_fmt_duration(cost.duration_seconds)}",
        "",
    ]

    if not cost.items:
        lines.append("  No model usage recorded.")
    else:
        for item in cost.items:
            price_str = (
                f"${item.cost_usd:.5f}" if item.cost_usd is not None else "no price"
            )
            lines.append(f"  {item.label}")
            lines.append(f"      {item.detail:<44} {price_str:>12}")

    total = cost.total_usd
    lines += [
        "",
        "-" * width,
        f"  TOTAL           ${total:.4f}   (about Rs {total * inr:.2f})",
        "=" * width,
    ]

    notes = [
        (
            f"  Prices are provider list rates as of {pricing.PRICES_UPDATED}; "
            "volume tiers are not applied."
        ),
        "  Excludes LiveKit Cloud agent minutes and any telephony charges.",
    ]
    if cost.has_unpriced:
        notes.append("  Some models have no published price here and are counted as 0.")
    lines += notes + [""]

    return "\n".join(lines)


def log_session_summary(session: Any, duration_seconds: float) -> SessionCost | None:
    """Compute and log the summary. Never raises - this runs during shutdown."""
    try:
        cost = summarise(session.usage, duration_seconds)
        logger.info(format_summary(cost))
        return cost
    except Exception:
        logger.exception("could not build the session summary")
        return None
