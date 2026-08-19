"""The AgentServer must be discoverable by the LiveKit CLI."""

from __future__ import annotations

from livekit.agents import AgentServer


def test_server_is_module_level_and_named_server():
    """`lk` looks for a module attr named app/server/agent that is an AgentServer."""
    from voice_agent import main

    assert isinstance(main.server, AgentServer)


def test_root_shim_reexports_the_same_server():
    import agent
    from voice_agent import main

    assert agent.server is main.server


def test_entrypoint_is_registered():
    """AgentServer.run() raises if no rtc_session was registered."""
    from voice_agent import main

    assert main.server._entrypoint_fnc is not None
