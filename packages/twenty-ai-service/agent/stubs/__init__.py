"""Stub tools — hardcoded placeholders for safety/session/utility tools.

Each stub returns the same ``{ ok, data }`` / ``{ ok, error }`` envelope as the
bridge so swapping in real implementations later is a drop-in replacement.

A module-level ``STUB`` flag makes it obvious which tools are placeholders.
"""

STUB = True
