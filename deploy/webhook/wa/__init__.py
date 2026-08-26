"""Minimal WhatsApp webhook package for serverless deployment.

Deliberately independent of the agent package: this only needs fastapi,
livekit-api and python-dotenv, not livekit-agents and its ML plugins. Keeping
the dependency set small keeps the serverless bundle fast to cold start.
"""
