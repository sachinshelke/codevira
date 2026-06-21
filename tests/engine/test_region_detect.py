"""Unit tests for _region_detect — mapping an edit to its enclosing symbol(s).

Pins the predicate symbol-level decision locking depends on: which named
symbol(s) a diff's `before` text lands in, and the crucial determinate-empty
(module-level) vs undeterminable (None) distinction.
"""

from __future__ import annotations

from pathlib import Path

from mcp_server.engine.policies._region_detect import symbols_touched_by_edit


def _envelope(before: str, after: str) -> str:
    return f"--- before\n{before}\n--- after\n{after}"


_PY = """\
import os


def login(user):
    token = make_token(user)
    return token


def logout(user):
    return None


class Session:
    def refresh(self):
        return refresh_token(self)
"""


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


class TestPythonRegion:
    def test_edit_inside_function_returns_that_function(self, tmp_path):
        f = _write(tmp_path, "auth.py", _PY)
        diff = _envelope("    token = make_token(user)", "    token = mint(user)")
        assert symbols_touched_by_edit(f, diff) == {"login"}

    def test_edit_in_other_function_excludes_target(self, tmp_path):
        f = _write(tmp_path, "auth.py", _PY)
        diff = _envelope("    return None", "    return False")
        touched = symbols_touched_by_edit(f, diff)
        assert touched == {"logout"}
        assert "login" not in touched  # the scoped symbol is NOT touched

    def test_edit_in_method_maps_to_method_and_enclosing_class(self, tmp_path):
        f = _write(tmp_path, "auth.py", _PY)
        diff = _envelope(
            "        return refresh_token(self)",
            "        return refresh_token(self, True)",
        )
        touched = symbols_touched_by_edit(f, diff)
        assert touched == {"Session", "refresh"}

    def test_module_level_edit_is_determinate_empty(self, tmp_path):
        f = _write(tmp_path, "auth.py", _PY)
        diff = _envelope("import os", "import os\nimport sys")
        # Determinate: the edit is outside every named symbol → empty SET,
        # NOT None. (Lets the caller treat a symbol-scoped lock as orthogonal.)
        assert symbols_touched_by_edit(f, diff) == set()


class TestUndeterminable:
    def test_before_not_in_file_returns_none(self, tmp_path):
        f = _write(tmp_path, "auth.py", _PY)
        diff = _envelope("    nonexistent = 1", "    nonexistent = 2")
        assert symbols_touched_by_edit(f, diff) is None

    def test_pure_insertion_returns_none(self, tmp_path):
        f = _write(tmp_path, "auth.py", _PY)
        diff = _envelope("", "    new_line = 1")
        assert symbols_touched_by_edit(f, diff) is None

    def test_malformed_diff_returns_none(self, tmp_path):
        f = _write(tmp_path, "auth.py", _PY)
        assert symbols_touched_by_edit(f, "not an envelope") is None
        assert symbols_touched_by_edit(f, None) is None

    def test_unsupported_language_returns_none(self, tmp_path):
        f = _write(tmp_path, "notes.txt", "hello\nworld\n")
        diff = _envelope("hello", "HELLO")
        assert symbols_touched_by_edit(f, diff) is None

    def test_missing_file_returns_none(self, tmp_path):
        diff = _envelope("x = 1", "x = 2")
        assert symbols_touched_by_edit(tmp_path / "nope.py", diff) is None
