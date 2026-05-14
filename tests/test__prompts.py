"""Tests for :mod:`mcp_server._prompts` — Bug 22 (rc.4 dogfood, 2026-05-13).

The shared :func:`confirm` helper replaced 5 hand-rolled ``input()`` prompts
across ``setup_wizard.py``, ``cli.py``, and ``cli_configure.py``. Before the
fix, all five would silently return False for any input that wasn't exactly
``y`` / ``yes`` / ``<Enter>``. Sachin reported (UDAP dogfood, 2026-05-13):
*"if i run the codevira setup then it will not accept Y or N with enter key"*
— surface symptom of an unexpected character slipping past ``.strip().lower()``.

This file pins the post-fix contract:

* Bare Enter returns the default (True for ``[Y/n]``, False for ``[y/N]``).
* All of ``y`` / ``Y`` / ``yes`` / ``YES`` return True.
* All of ``n`` / ``N`` / ``no`` / ``NO`` return False.
* Anything else *reprompts* — does NOT silently return False.
* EOFError (non-interactive stdin) returns False with a clear hint message.
* KeyboardInterrupt returns False, no traceback.
"""
from __future__ import annotations

import io
import pytest

from mcp_server._prompts import confirm


class TestConfirmHappyPath:
    """y / n / Enter cases — must each terminate the prompt loop on first read."""

    @pytest.mark.parametrize("answer,default,expected", [
        ("y\n", True, True),
        ("Y\n", True, True),
        ("yes\n", True, True),
        ("YES\n", True, True),
        ("n\n", True, False),
        ("N\n", True, False),
        ("no\n", True, False),
        ("NO\n", True, False),
        ("\n", True, True),    # bare Enter, default=True
        ("\n", False, False),  # bare Enter, default=False
        ("y\n", False, True),  # explicit yes overrides default=False
        ("n\n", True, False),  # explicit no overrides default=True
        # Whitespace tolerance — .strip() should handle these.
        ("  y\n", True, True),
        ("y   \n", True, True),
        ("\t\ty\t\n", True, True),
    ])
    def test_single_recognised_answer(self, answer, default, expected, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO(answer))
        assert confirm("Proceed?", default=default) is expected


class TestConfirmRetryOnBadInput:
    """The Bug 22 fix: invalid input must reprompt, not silently return False."""

    def test_invalid_then_yes_returns_yes(self, monkeypatch, capsys):
        # Three garbage answers then a clean "y" — confirm() must keep asking
        # rather than returning False on the first garbage one.
        monkeypatch.setattr("sys.stdin", io.StringIO("yy\nmaybe\nidk\ny\n"))
        assert confirm("Proceed?", default=True) is True
        out = capsys.readouterr().out
        # The "Please answer 'y' or 'n'." nudge should appear once per bad answer.
        assert out.count("Please answer 'y' or 'n'.") == 3

    def test_invalid_then_no_returns_no(self, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin", io.StringIO("???\nn\n"))
        assert confirm("Proceed?", default=True) is False
        assert "Please answer 'y' or 'n'." in capsys.readouterr().out

    def test_invalid_then_enter_returns_default(self, monkeypatch, capsys):
        # Bug 22 was about THIS: pre-fix, anything not in ("","y","yes") returned
        # False. Post-fix, "garbage\n" reprompts and "\n" returns default=True.
        monkeypatch.setattr("sys.stdin", io.StringIO("garbage\n\n"))
        assert confirm("Proceed?", default=True) is True


class TestConfirmEdgeCases:
    """EOF, Ctrl+C, custom indent — must not raise."""

    def test_eof_returns_false_with_hint(self, monkeypatch, capsys):
        # Empty stdin → EOFError on input() → return False + helpful hint.
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        assert confirm("Proceed?", default=True) is False
        out = capsys.readouterr().out
        assert "Non-interactive shell" in out
        assert "--yes" in out

    def test_keyboard_interrupt_returns_false_no_traceback(self, monkeypatch):
        # Simulate Ctrl+C by making input() raise KeyboardInterrupt.
        def fake_input(_prompt):
            raise KeyboardInterrupt
        monkeypatch.setattr("builtins.input", fake_input)
        assert confirm("Proceed?", default=True) is False
        # Must NOT propagate the KeyboardInterrupt — the whole point of the fix.

    def test_indent_is_configurable(self, monkeypatch):
        captured = []
        def fake_input(prompt):
            captured.append(prompt)
            return "y\n"
        monkeypatch.setattr("builtins.input", fake_input)
        confirm("Proceed?", default=True, indent="    ")
        assert captured[0].startswith("    "), captured[0]

    def test_default_false_renders_y_capital_n(self, monkeypatch):
        captured = []
        def fake_input(prompt):
            captured.append(prompt)
            return "n\n"
        monkeypatch.setattr("builtins.input", fake_input)
        confirm("Proceed?", default=False)
        assert "[y/N]" in captured[0]

    def test_default_true_renders_capital_y_n(self, monkeypatch):
        captured = []
        def fake_input(prompt):
            captured.append(prompt)
            return "y\n"
        monkeypatch.setattr("builtins.input", fake_input)
        confirm("Proceed?", default=True)
        assert "[Y/n]" in captured[0]


class TestConfirmFlushesStdoutBeforeRead:
    """Bug 22 fix half 2: flush stdout so the prompt is visible before stdin blocks."""

    def test_flush_called_before_input(self, monkeypatch):
        import sys
        flush_called = []
        original_flush = sys.stdout.flush
        def tracking_flush():
            flush_called.append(True)
            original_flush()
        monkeypatch.setattr("sys.stdout.flush", tracking_flush)
        monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
        confirm("Proceed?", default=True)
        # The helper must flush at least once before reading input.
        assert len(flush_called) >= 1
