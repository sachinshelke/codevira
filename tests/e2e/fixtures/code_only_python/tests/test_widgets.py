"""Tests for the widgets module."""

from __future__ import annotations

import pytest

from src.widgets import Widget, make_widget, total_count


def test_make_widget_basic() -> None:
    w = make_widget("foo", 5)
    assert w.name == "foo"
    assert w.count == 5


def test_make_widget_rejects_empty_name() -> None:
    with pytest.raises(ValueError):
        make_widget("")


def test_total_count() -> None:
    widgets = [Widget("a", 1), Widget("b", 2), Widget("c", 3)]
    assert total_count(widgets) == 6
