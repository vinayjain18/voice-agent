"""Entrypoint shim for the LiveKit CLI.

`lk agent <command>` with no path argument looks for `main.py`, `app.py` or
`agent.py` in the working directory, then imports it and picks up the
`AgentServer` defined there. The real definition lives in the package; this file
just re-exports it so the commands stay short:

    lk agent console
    lk agent dev
"""

from voice_agent.main import server

__all__ = ["server"]
