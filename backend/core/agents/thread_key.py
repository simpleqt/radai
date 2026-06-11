"""Compatibility re-exports for internal thread id helpers.

Canonical definitions live in :mod:`backend.core.thread_key` so config and
agents can share one implementation without ``backend.core.agents`` needing
to import the full config package at package init time.
"""

from backend.core.thread_key import build_internal_thread_key, parse_internal_thread_key

__all__ = ["build_internal_thread_key", "parse_internal_thread_key"]
