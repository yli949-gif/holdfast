"""Holdfast: a stateful constraint guard for coding agents.

Rules stated in natural language are compiled into a small closed vocabulary
(see rules.py), persisted outside the model context (state.py) and enforced at
every tool call through Claude Code hooks (hooks.py).
"""

__version__ = "0.1.0"
