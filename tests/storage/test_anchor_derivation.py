"""4.0 Step 7 — derive the symbol instead of asking an agent to type it.

`symbol` has existed since v3.6.0 and is what makes region-level locking
possible: decision_lock then blocks only edits INSIDE the named function
rather than anywhere in the file. It sits at 1/123 populated.

That is not agent laziness — it is the documented pattern. Every field a
MACHINE writes gets populated (outcome 21%, origin 68%); every field an
AGENT must type does not (symbol 1%, alternatives_considered 0%). So the
symbol is resolved from the edit the session actually made.

The safety property matters more than the coverage: a WRONG symbol is
worse than none, because it scopes a lock to a region the decision has
nothing to do with. Every ambiguous case must yield None.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import anchor


SRC = '''"""module."""


def alpha(x):
    a = 1
    b = 2
    return a + b


def beta(y):
    return y * 2
'''


@pytest.fixture
def graphed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project whose graph.db knows one file with two functions."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "src" / "m.py").write_text(SRC)

    data = tmp_path / "data"
    (data / "graph").mkdir(parents=True)
    db = data / "graph" / "graph.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE symbols (id TEXT, file_node_id TEXT, name TEXT, kind TEXT,"
        " signature TEXT, parameters TEXT, return_type TEXT, start_line INT,"
        " end_line INT, docstring TEXT, is_public INT, calls TEXT)"
    )
    for name, s, e in (("alpha", 4, 7), ("beta", 10, 11)):
        conn.execute(
            "INSERT INTO symbols (id, file_node_id, name, kind, start_line, end_line)"
            " VALUES (?,?,?,?,?,?)",
            (f"file:src/m.py::{name}", "file:src/m.py", name, "function", s, e),
        )
    conn.commit()
    conn.close()

    monkeypatch.setattr("mcp_server.paths.get_data_dir", lambda *a, **k: data)
    monkeypatch.chdir(root)
    return root


class TestSymbolAt:
    @pytest.mark.parametrize(
        "line,expected", [(4, "alpha"), (6, "alpha"), (10, "beta")]
    )
    def test_resolves_the_enclosing_symbol(
        self, graphed: Path, line: int, expected: str
    ) -> None:
        assert anchor.symbol_at("src/m.py", line) == expected

    def test_module_level_code_has_no_symbol(self, graphed: Path) -> None:
        """Line 1 is a docstring — outside every function."""
        assert anchor.symbol_at("src/m.py", 1) is None

    def test_unknown_file_yields_none(self, graphed: Path) -> None:
        assert anchor.symbol_at("src/nope.py", 5) is None

    @pytest.mark.parametrize("bad", [("", 5), ("src/m.py", 0), ("src/m.py", -3)])
    def test_bad_input_degrades(self, graphed: Path, bad) -> None:
        assert anchor.symbol_at(bad[0], bad[1]) is None

    def test_missing_graph_yields_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "mcp_server.paths.get_data_dir", lambda *a, **k: tmp_path / "absent"
        )
        assert anchor.symbol_at("src/m.py", 5) is None


class TestAmbiguityYieldsNone:
    """A wrong symbol is worse than no symbol."""

    def test_identical_spans_are_not_guessed_between(
        self, graphed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_server.paths import get_data_dir

        conn = sqlite3.connect(get_data_dir() / "graph" / "graph.db")
        conn.execute(
            "INSERT INTO symbols (id, file_node_id, name, kind, start_line, end_line)"
            " VALUES (?,?,?,?,?,?)",
            ("file:src/m.py::twin", "file:src/m.py", "twin", "function", 4, 7),
        )
        conn.commit()
        conn.close()
        assert anchor.symbol_at("src/m.py", 6) is None

    def test_inner_symbol_wins_over_outer(self, graphed: Path) -> None:
        """A method inside a class must resolve to the METHOD — otherwise a
        region lock covers the whole class and blocks unrelated edits."""
        from mcp_server.paths import get_data_dir

        conn = sqlite3.connect(get_data_dir() / "graph" / "graph.db")
        conn.execute(
            "INSERT INTO symbols (id, file_node_id, name, kind, start_line, end_line)"
            " VALUES (?,?,?,?,?,?)",
            ("file:src/m.py::Outer", "file:src/m.py", "Outer", "class", 1, 20),
        )
        conn.commit()
        conn.close()
        assert anchor.symbol_at("src/m.py", 6) == "alpha"


class TestLineOfEdit:
    def test_finds_a_unique_anchor(self, graphed: Path) -> None:
        assert anchor.line_of_edit("    a = 1", SRC) == 5

    def test_repeated_text_is_ambiguous(self, graphed: Path) -> None:
        """Two identical lines cannot pin a location."""
        text = "x = 1\ny = 2\nx = 1\n"
        assert anchor.line_of_edit("x = 1", text) is None

    def test_absent_text_yields_none(self, graphed: Path) -> None:
        assert anchor.line_of_edit("nowhere", SRC) is None

    def test_empty_inputs_yield_none(self, graphed: Path) -> None:
        assert anchor.line_of_edit("", SRC) is None
        assert anchor.line_of_edit("a", "") is None


class TestEndToEnd:
    def test_edit_resolves_to_its_symbol(self, graphed: Path) -> None:
        got = anchor.resolve_from_edit("src/m.py", "    a = 1", project_root=graphed)
        assert got == "alpha"

    def test_edit_outside_any_function_yields_none(self, graphed: Path) -> None:
        assert (
            anchor.resolve_from_edit('"""module."""', "x", project_root=graphed) is None
        )

    def test_record_derives_symbol_from_the_session_edit(
        self, graphed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The full Step 7 chain: an edit is logged with its line, then a
        decision recorded in the SAME session inherits the symbol without
        anyone typing it. This is only possible because Step 3.2 made the
        session id stable — before that, 0 of 116 decisions shared a
        session with an edit row."""
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(graphed))
        from mcp_server.storage import activity_store, decisions_store, paths

        paths.ensure_dirs(graphed)
        monkeypatch.setattr(decisions_store, "_PROCESS_SESSION_ID", None, raising=False)
        sid = decisions_store.default_session_id()

        activity_store.add(
            "src/m.py", kind=activity_store.KIND_EDIT, session_id=sid, line=6
        )
        did = decisions_store.record(
            "Keep alpha's accumulator explicit", file_path="src/m.py"
        )

        assert decisions_store.get(did)["symbol"] == "alpha"

    def test_explicit_symbol_is_never_overridden(
        self, graphed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(graphed))
        from mcp_server.storage import activity_store, decisions_store, paths

        paths.ensure_dirs(graphed)
        monkeypatch.setattr(decisions_store, "_PROCESS_SESSION_ID", None, raising=False)
        sid = decisions_store.default_session_id()
        activity_store.add(
            "src/m.py", kind=activity_store.KIND_EDIT, session_id=sid, line=6
        )
        did = decisions_store.record("X", file_path="src/m.py", symbol="beta")
        assert decisions_store.get(did)["symbol"] == "beta"

    def test_no_matching_edit_leaves_symbol_unset(
        self, graphed: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Derivation must never invent a symbol — file-scoped is the
        correct, unchanged fallback."""
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(graphed))
        from mcp_server.storage import decisions_store, paths

        paths.ensure_dirs(graphed)
        did = decisions_store.record("No edits happened", file_path="src/m.py")
        assert decisions_store.get(did)["symbol"] is None
