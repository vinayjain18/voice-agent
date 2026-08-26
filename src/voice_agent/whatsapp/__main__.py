"""Run the webhook: uv run python -m voice_agent.whatsapp"""

from __future__ import annotations

import logging
import os

import uvicorn
from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    port = int(os.environ.get("WHATSAPP_WEBHOOK_PORT", "8000"))
    logging.getLogger("voice_agent.whatsapp").info(
        "webhook listening on http://127.0.0.1:%d/webhook", port
    )
    uvicorn.run("voice_agent.whatsapp.webhook:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
