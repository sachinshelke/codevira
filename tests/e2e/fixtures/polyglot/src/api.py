"""Python API entrypoint for the polyglot fixture."""

from __future__ import annotations


def health_check() -> dict[str, str]:
    """Return server health status."""
    return {"status": "ok"}
