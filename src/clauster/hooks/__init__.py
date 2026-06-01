"""Standalone hook scripts Clauster wires into the runtime user's Claude config.

These run as separate processes invoked by Claude Code (not inside the Clauster
server), so they MUST stay dependency-free (stdlib only) and fail safe.
"""
