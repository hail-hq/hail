"""Voicebot agent tools — channel-agnostic registry.

Spec: docs/superpowers/specs/2026-07-11-voicebot-agent-tools-design.md.
One module per tool; the voicebot adapts :class:`ToolSpec` entries to
LiveKit function tools. This package must stay livekit-free.
"""
