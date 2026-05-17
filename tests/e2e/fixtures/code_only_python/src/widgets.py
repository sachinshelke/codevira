"""Widget operations — sample module for the code-only-python fixture."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Widget:
    """A widget with a name and a count."""

    name: str
    count: int


def make_widget(name: str, count: int = 1) -> Widget:
    """Construct a Widget. Validates non-empty name."""
    if not name:
        raise ValueError("widget name must be non-empty")
    return Widget(name=name, count=count)


def total_count(widgets: list[Widget]) -> int:
    """Sum the count across all widgets."""
    return sum(w.count for w in widgets)
