"""
Tests for indexer/global_db.py — Global SQLite database for cross-project intelligence.

Covers:
  - GlobalDB.__init__(): schema creation, WAL mode, foreign keys
  - register_project(): all params, upsert behavior
  - get_project_count(): empty, 1, multiple
  - find_project_by_remote(): found, not found, None url
  - get_preferences(): frequency filter, language param (unused but tested)
  - get_rules(): confidence filter, language filter
  - upsert_preference(): insert + update (frequency aggregation, source merging)
  - upsert_rule(): insert + update (weighted average confidence)
  - get_stats(): returns project_count, total_preferences, total_rules
  - close(): closes connection

Chaos tests:
  - Register same project twice (upsert)
  - Very long strings in all fields
  - Empty strings
  - Unicode in project names
  - Concurrent access patterns
  - Fresh database schema creation
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from indexer.global_db import GlobalDB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(tmp_path, name="global.db") -> GlobalDB:
    """Create a fresh GlobalDB instance."""
    return GlobalDB(tmp_path / name)


# ===================================================================
# Schema creation
# ===================================================================


class TestSchemaCreation:
    """Test that fresh databases get correct schema."""

    def test_creates_all_tables(self, tmp_path):
        db = _make_db(tmp_path)
        tables = {
            row[0]
            for row in db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "projects" in tables
        assert "global_preferences" in tables
        assert "global_rules" in tables
        db.close()

    def test_wal_mode_enabled(self, tmp_path):
        db = _make_db(tmp_path)
        mode = db.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        db.close()

    def test_foreign_keys_enabled(self, tmp_path):
        db = _make_db(tmp_path)
        fk = db.conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert fk == 1
        db.close()

    def test_projects_table_columns(self, tmp_path):
        db = _make_db(tmp_path)
        cols = {
            row[1] for row in db.conn.execute("PRAGMA table_info(projects)").fetchall()
        }
        assert "path" in cols
        assert "name" in cols
        assert "language" in cols
        assert "git_remote" in cols
        assert "last_synced_at" in cols
        db.close()

    def test_global_preferences_table_columns(self, tmp_path):
        db = _make_db(tmp_path)
        cols = {
            row[1]
            for row in db.conn.execute(
                "PRAGMA table_info(global_preferences)"
            ).fetchall()
        }
        assert "category" in cols
        assert "signal" in cols
        assert "example" in cols
        assert "frequency" in cols
        assert "source_projects" in cols
        db.close()

    def test_global_rules_table_columns(self, tmp_path):
        db = _make_db(tmp_path)
        cols = {
            row[1]
            for row in db.conn.execute("PRAGMA table_info(global_rules)").fetchall()
        }
        assert "rule_text" in cols
        assert "confidence" in cols
        assert "source_projects" in cols
        assert "category" in cols
        assert "language" in cols
        db.close()

    def test_creates_parent_directory(self, tmp_path):
        """GlobalDB creates parent dirs for the db file if needed."""
        nested = tmp_path / "deep" / "nested" / "dir"
        db = GlobalDB(nested / "global.db")
        assert nested.exists()
        db.close()

    def test_reopen_existing_database(self, tmp_path):
        """Opening an existing database does not destroy data."""
        db1 = _make_db(tmp_path)
        db1.register_project("/proj", "test", "python")
        db1.close()

        db2 = _make_db(tmp_path)
        assert db2.get_project_count() == 1
        db2.close()


# ===================================================================
# register_project
# ===================================================================


class TestRegisterProject:
    """Test project registration."""

    def test_register_basic(self, tmp_path):
        db = _make_db(tmp_path)
        db.register_project("/path/to/project", "my-project", "python")
        assert db.get_project_count() == 1
        db.close()

    def test_register_with_git_remote(self, tmp_path):
        db = _make_db(tmp_path)
        db.register_project(
            path="/proj",
            name="TestProj",
            language="python",
            git_remote="https://github.com/org/repo.git",
        )

        # Verify git_remote column was set
        row = db.conn.execute(
            "SELECT git_remote FROM projects WHERE path = ?", ("/proj",)
        ).fetchone()
        assert row is not None
        assert row["git_remote"] == "https://github.com/org/repo.git"
        db.close()

    def test_register_without_git_remote(self, tmp_path):
        """register_project with git_remote=None should not crash."""
        db = _make_db(tmp_path)
        db.register_project(
            path="/no-git",
            name="NoGit",
            language="python",
            git_remote=None,
        )
        assert db.get_project_count() == 1
        db.close()

    def test_register_all_params(self, tmp_path):
        """All parameters are stored correctly."""
        db = _make_db(tmp_path)
        db.register_project(
            path="/full/path",
            name="FullProject",
            language="typescript",
            git_remote="git@github.com:org/full.git",
        )

        row = db.conn.execute(
            "SELECT * FROM projects WHERE path = '/full/path'"
        ).fetchone()
        assert row["name"] == "FullProject"
        assert row["language"] == "typescript"
        assert row["git_remote"] == "git@github.com:org/full.git"
        assert row["last_synced_at"] is not None
        db.close()

    def test_register_upsert_updates_existing(self, tmp_path):
        """Registering the same path twice updates (INSERT OR REPLACE)."""
        db = _make_db(tmp_path)
        db.register_project("/proj", "Original", "python")
        db.register_project(
            "/proj", "Updated", "typescript", git_remote="https://new.git"
        )

        assert db.get_project_count() == 1
        row = db.conn.execute("SELECT * FROM projects WHERE path = '/proj'").fetchone()
        assert row["name"] == "Updated"
        assert row["language"] == "typescript"
        assert row["git_remote"] == "https://new.git"
        db.close()

    def test_register_multiple_projects(self, tmp_path):
        db = _make_db(tmp_path)
        for i in range(5):
            db.register_project(f"/proj-{i}", f"Project-{i}", "python")
        assert db.get_project_count() == 5
        db.close()


# ===================================================================
# get_project_count
# ===================================================================


class TestGetProjectCount:
    def test_empty_database(self, tmp_path):
        db = _make_db(tmp_path)
        assert db.get_project_count() == 0
        db.close()

    def test_one_project(self, tmp_path):
        db = _make_db(tmp_path)
        db.register_project("/a", "a", "python")
        assert db.get_project_count() == 1
        db.close()

    def test_multiple_projects(self, tmp_path):
        db = _make_db(tmp_path)
        for i in range(10):
            db.register_project(f"/proj-{i}", f"proj-{i}", "python")
        assert db.get_project_count() == 10
        db.close()


# ===================================================================
# upsert_preference
# ===================================================================


class TestUpsertPreference:
    def test_insert_new(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_preference("naming", "snake_case", "my_var", "/proj-a")

        prefs = db.get_preferences(min_frequency=1)
        assert len(prefs) == 1
        assert prefs[0]["category"] == "naming"
        assert prefs[0]["signal"] == "snake_case"
        assert prefs[0]["example"] == "my_var"
        assert prefs[0]["frequency"] == 1
        sources = json.loads(prefs[0]["source_projects"])
        assert "/proj-a" in sources
        db.close()

    def test_update_increments_frequency(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_preference("naming", "snake_case", "my_var", "/proj-a")
        db.upsert_preference("naming", "snake_case", "other_var", "/proj-b")

        prefs = db.get_preferences(min_frequency=1)
        assert len(prefs) == 1
        assert prefs[0]["frequency"] == 2
        sources = json.loads(prefs[0]["source_projects"])
        assert "/proj-a" in sources
        assert "/proj-b" in sources
        db.close()

    def test_same_source_not_duplicated(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_preference("naming", "snake_case", "v1", "/proj-a")
        db.upsert_preference("naming", "snake_case", "v2", "/proj-a")

        prefs = db.get_preferences(min_frequency=1)
        sources = json.loads(prefs[0]["source_projects"])
        assert sources.count("/proj-a") == 1
        # Frequency still incremented
        assert prefs[0]["frequency"] == 2
        db.close()

    def test_custom_frequency(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_preference("style", "tabs", "\\t", "/proj-a", frequency=5)
        prefs = db.get_preferences(min_frequency=1)
        assert prefs[0]["frequency"] == 5
        db.close()

    def test_none_example(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_preference("structure", "flat-imports", None, "/proj-a")
        prefs = db.get_preferences(min_frequency=1)
        assert len(prefs) == 1
        # example is None
        assert prefs[0]["example"] is None
        db.close()

    def test_update_preserves_existing_example_when_new_is_none(self, tmp_path):
        """COALESCE(?, example) keeps old example when new is None."""
        db = _make_db(tmp_path)
        db.upsert_preference("naming", "camelCase", "myVar", "/proj-a")
        db.upsert_preference("naming", "camelCase", None, "/proj-b")

        prefs = db.get_preferences(min_frequency=1)
        assert prefs[0]["example"] == "myVar"
        db.close()


# ===================================================================
# get_preferences
# ===================================================================


class TestGetPreferences:
    def test_min_frequency_filter(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_preference("naming", "snake_case", "v", "/a")
        db.upsert_preference("naming", "snake_case", "v", "/b")
        db.upsert_preference("naming", "snake_case", "v", "/c")
        db.upsert_preference("style", "tabs", "\\t", "/a")  # frequency=1

        # min_frequency=3 should only return snake_case
        prefs = db.get_preferences(min_frequency=3)
        assert len(prefs) == 1
        assert prefs[0]["signal"] == "snake_case"
        db.close()

    def test_default_min_frequency(self, tmp_path):
        """Default min_frequency=3 filters out low-frequency prefs."""
        db = _make_db(tmp_path)
        db.upsert_preference("naming", "rare_pref", "x", "/a")  # freq=1
        prefs = db.get_preferences()  # default min_frequency=3
        assert len(prefs) == 0
        db.close()

    def test_empty_database(self, tmp_path):
        db = _make_db(tmp_path)
        prefs = db.get_preferences(min_frequency=1)
        assert prefs == []
        db.close()

    def test_ordered_by_frequency_desc(self, tmp_path):
        db = _make_db(tmp_path)
        # "rare" gets 1, "common" gets 5
        db.upsert_preference("naming", "rare", "x", "/a")
        for i in range(5):
            db.upsert_preference("naming", "common", "x", f"/p{i}")

        prefs = db.get_preferences(min_frequency=1)
        assert prefs[0]["signal"] == "common"
        assert prefs[1]["signal"] == "rare"
        db.close()


# ===================================================================
# upsert_rule
# ===================================================================


class TestUpsertRule:
    def test_insert_new(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule("Always use early returns", 0.8, "/proj-a", "patterns", "python")

        rules = db.get_rules(min_confidence=0.0)
        assert len(rules) == 1
        assert rules[0]["rule_text"] == "Always use early returns"
        assert abs(rules[0]["confidence"] - 0.8) < 0.01
        assert rules[0]["category"] == "patterns"
        assert rules[0]["language"] == "python"
        sources = json.loads(rules[0]["source_projects"])
        assert "/proj-a" in sources
        db.close()

    def test_update_weighted_average(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule("Always use early returns", 0.8, "/proj-a", "patterns", "python")
        db.upsert_rule("Always use early returns", 1.0, "/proj-b", "patterns", "python")

        rules = db.get_rules(min_confidence=0.0)
        assert len(rules) == 1
        # Weighted: 0.8 * 0.6 + 1.0 * 0.4 = 0.88
        assert abs(rules[0]["confidence"] - 0.88) < 0.01
        db.close()

    def test_update_merges_sources(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule("Rule X", 0.7, "/a")
        db.upsert_rule("Rule X", 0.9, "/b")

        rules = db.get_rules(min_confidence=0.0)
        sources = json.loads(rules[0]["source_projects"])
        assert "/a" in sources
        assert "/b" in sources
        db.close()

    def test_same_source_not_duplicated(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule("Rule Y", 0.7, "/a")
        db.upsert_rule("Rule Y", 0.9, "/a")

        rules = db.get_rules(min_confidence=0.0)
        sources = json.loads(rules[0]["source_projects"])
        assert sources.count("/a") == 1
        db.close()

    def test_no_category_or_language(self, tmp_path):
        """Rule with category=None and language=None."""
        db = _make_db(tmp_path)
        db.upsert_rule("Generic rule", 0.5, "/a")

        rules = db.get_rules(min_confidence=0.0)
        assert rules[0]["category"] is None
        assert rules[0]["language"] is None
        db.close()


# ===================================================================
# get_rules
# ===================================================================


class TestGetRules:
    def test_confidence_filter(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule("High conf", 0.9, "/a", "patterns")
        db.upsert_rule("Low conf", 0.3, "/a", "patterns")

        rules = db.get_rules(min_confidence=0.6)
        assert len(rules) == 1
        assert rules[0]["rule_text"] == "High conf"
        db.close()

    def test_language_filter_strict_excludes_null_language(self, tmp_path):
        """2026-05-18 v2.1.2 Item 9: when a language is supplied to
        get_rules(), STRICT match is the default. Rules without a
        language (NULL) no longer leak across projects.

        Report 1 §3.3 caught a Go-project rule (NULL language) appearing
        in a Python project — this test guards against that regression.
        """
        db = _make_db(tmp_path)
        db.upsert_rule("Go exported names capitalized", 0.9, "/a", "naming", "go")
        db.upsert_rule("Universal early returns", 0.9, "/b", "patterns", None)

        rules = db.get_rules(min_confidence=0.5, language="python")
        names = [r["rule_text"] for r in rules]
        assert (
            "Universal early returns" not in names
        ), "Item 9 regression: NULL-language rule leaked across languages."
        assert "Go exported names capitalized" not in names
        db.close()

    def test_language_filter_loose_includes_null_language(self, tmp_path):
        """Item 9: pass strict_language=False to opt back into the legacy
        cross-language fan-out (rule with NULL language matches every
        language query). Provided for backward compat.
        """
        db = _make_db(tmp_path)
        db.upsert_rule("Go exported names capitalized", 0.9, "/a", "naming", "go")
        db.upsert_rule("Universal early returns", 0.9, "/b", "patterns", None)

        rules = db.get_rules(
            min_confidence=0.5, language="python", strict_language=False
        )
        names = [r["rule_text"] for r in rules]
        assert "Universal early returns" in names
        assert "Go exported names capitalized" not in names
        db.close()

    def test_language_filter_includes_matching(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule("Python type hints", 0.9, "/a", "patterns", "python")
        db.upsert_rule("Go error handling", 0.9, "/b", "patterns", "go")

        rules = db.get_rules(min_confidence=0.5, language="python")
        names = [r["rule_text"] for r in rules]
        assert "Python type hints" in names
        assert "Go error handling" not in names
        db.close()

    def test_no_language_filter_returns_all(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule("Python rule", 0.9, "/a", "p", "python")
        db.upsert_rule("Go rule", 0.9, "/b", "p", "go")
        db.upsert_rule("Universal rule", 0.9, "/c", "p", None)

        rules = db.get_rules(min_confidence=0.5)
        assert len(rules) == 3
        db.close()

    def test_default_confidence_filter(self, tmp_path):
        """Default min_confidence=0.6 filters out low-confidence rules."""
        db = _make_db(tmp_path)
        db.upsert_rule("Low", 0.3, "/a")
        db.upsert_rule("High", 0.8, "/b")

        rules = db.get_rules()  # default min_confidence=0.6
        assert len(rules) == 1
        assert rules[0]["rule_text"] == "High"
        db.close()

    def test_empty_database(self, tmp_path):
        db = _make_db(tmp_path)
        rules = db.get_rules(min_confidence=0.0)
        assert rules == []
        db.close()

    def test_ordered_by_confidence_desc(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule("Medium", 0.6, "/a")
        db.upsert_rule("High", 0.95, "/b")
        db.upsert_rule("Low", 0.1, "/c")

        rules = db.get_rules(min_confidence=0.0)
        assert rules[0]["rule_text"] == "High"
        assert rules[1]["rule_text"] == "Medium"
        assert rules[2]["rule_text"] == "Low"
        db.close()


# ===================================================================
# get_stats
# ===================================================================


class TestGetStats:
    def test_empty_stats(self, tmp_path):
        db = _make_db(tmp_path)
        stats = db.get_stats()
        assert stats["project_count"] == 0
        assert stats["total_preferences"] == 0
        assert stats["total_rules"] == 0
        db.close()

    def test_populated_stats(self, tmp_path):
        db = _make_db(tmp_path)
        db.register_project("/a", "a", "python")
        db.register_project("/b", "b", "go")
        db.upsert_preference("naming", "snake_case", None, "/a")
        db.upsert_preference("style", "tabs", None, "/a")
        db.upsert_rule("Rule 1", 0.8, "/a")
        db.upsert_rule("Rule 2", 0.7, "/b")
        db.upsert_rule("Rule 3", 0.6, "/b")

        stats = db.get_stats()
        assert stats["project_count"] == 2
        assert stats["total_preferences"] == 2
        assert stats["total_rules"] == 3
        db.close()


# ===================================================================
# close
# ===================================================================


class TestClose:
    def test_close_closes_connection(self, tmp_path):
        db = _make_db(tmp_path)
        db.close()
        # After close, executing on the connection should raise
        with pytest.raises(Exception):
            db.conn.execute("SELECT 1")

    def test_close_idempotent_raises_but_no_crash(self, tmp_path):
        """Calling close twice does not cause a crash (just a ProgrammingError)."""
        db = _make_db(tmp_path)
        db.close()
        # Second close may raise ProgrammingError but should not hard-crash
        try:
            db.close()
        except Exception:
            pass  # Expected


# ===================================================================
# Git remote column upgrade
# ===================================================================


class TestGitRemoteColumnUpgrade:
    """Test that register_project auto-adds git_remote column for old schemas."""

    def test_register_adds_column_if_missing(self, tmp_path):
        """register_project on a pre-v1.6 schema auto-adds git_remote column."""
        db_path = tmp_path / "old.db"
        # Create old schema without git_remote
        conn = sqlite3.connect(str(db_path))
        conn.execute("""
            CREATE TABLE projects (
                path TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                language TEXT,
                last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS global_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                signal TEXT NOT NULL,
                UNIQUE(category, signal)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS global_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_text TEXT NOT NULL UNIQUE
            )
        """)
        conn.commit()
        conn.close()

        # GlobalDB _init_schema uses CREATE TABLE IF NOT EXISTS, so it
        # should not fail on existing tables. register_project handles the upgrade.
        db = GlobalDB(db_path)
        db.register_project(
            "/proj", "Test", "python", git_remote="https://git.example.com/repo"
        )

        # Verify the column was added and the value persisted
        row = db.conn.execute(
            "SELECT git_remote FROM projects WHERE path = ?", ("/proj",)
        ).fetchone()
        assert row is not None
        assert row["git_remote"] == "https://git.example.com/repo"
        db.close()


# ===================================================================
# CHAOS Tests
# ===================================================================


class TestGlobalDBChaos:
    """Edge cases, corruptions, and adversarial inputs."""

    def test_register_same_project_twice_upsert(self, tmp_path):
        """Registering the same path twice replaces data (INSERT OR REPLACE)."""
        db = _make_db(tmp_path)
        db.register_project("/p", "Original", "python")
        db.register_project("/p", "Replaced", "go", git_remote="https://new.git")

        assert db.get_project_count() == 1
        row = db.conn.execute(
            "SELECT name, language FROM projects WHERE path='/p'"
        ).fetchone()
        assert row["name"] == "Replaced"
        assert row["language"] == "go"
        db.close()

    def test_very_long_strings(self, tmp_path):
        """Very long strings in all fields do not crash."""
        db = _make_db(tmp_path)
        long_str = "x" * 10000

        db.register_project(long_str, long_str, long_str, git_remote=long_str)
        assert db.get_project_count() == 1

        db.upsert_preference(long_str, long_str, long_str, long_str)
        prefs = db.get_preferences(min_frequency=1)
        assert len(prefs) == 1

        db.upsert_rule(long_str, 0.5, long_str, long_str, long_str)
        rules = db.get_rules(min_confidence=0.0)
        assert len(rules) == 1
        db.close()

    def test_empty_strings(self, tmp_path):
        """Empty strings in fields do not crash."""
        db = _make_db(tmp_path)
        db.register_project("", "", "")
        assert db.get_project_count() == 1

        db.upsert_preference("", "", "", "")
        prefs = db.get_preferences(min_frequency=1)
        assert len(prefs) == 1

        db.upsert_rule("", 0.0, "")
        rules = db.get_rules(min_confidence=0.0)
        assert len(rules) == 1
        db.close()

    def test_unicode_project_names(self, tmp_path):
        """Unicode in project names is handled correctly."""
        db = _make_db(tmp_path)
        db.register_project(
            "/home/user/\u30d7\u30ed\u30b8\u30a7\u30af\u30c8",
            "\u30c6\u30b9\u30c8",
            "python",
        )
        db.register_project("/home/user/cafe\u0301", "Caf\u00e9 App", "typescript")
        db.register_project("/home/user/\U0001f680-app", "Rocket App", "go")

        assert db.get_project_count() == 3

        # Verify data round-trips correctly
        row = db.conn.execute(
            "SELECT name FROM projects WHERE path = ?",
            ("/home/user/\u30d7\u30ed\u30b8\u30a7\u30af\u30c8",),
        ).fetchone()
        assert row["name"] == "\u30c6\u30b9\u30c8"
        db.close()

    def test_unicode_in_preferences(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_preference(
            "\u547d\u540d\u898f\u5247",
            "\u30b9\u30cd\u30fc\u30af\u30b1\u30fc\u30b9",
            "\u4f8b\u3048\u3070_\u5909\u6570",
            "/\u30d7\u30ed\u30b8\u30a7\u30af\u30c8",
        )
        prefs = db.get_preferences(min_frequency=1)
        assert len(prefs) == 1
        assert prefs[0]["category"] == "\u547d\u540d\u898f\u5247"
        db.close()

    def test_unicode_in_rules(self, tmp_path):
        db = _make_db(tmp_path)
        db.upsert_rule(
            "\u65e9\u671f\u30ea\u30bf\u30fc\u30f3\u3092\u4f7f\u3046\u3053\u3068",
            0.9,
            "/\u30d7\u30ed\u30b8\u30a7\u30af\u30c8",
            "\u30d1\u30bf\u30fc\u30f3",
            "python",
        )
        rules = db.get_rules(min_confidence=0.0)
        assert len(rules) == 1
        assert (
            rules[0]["rule_text"]
            == "\u65e9\u671f\u30ea\u30bf\u30fc\u30f3\u3092\u4f7f\u3046\u3053\u3068"
        )
        db.close()

    def test_concurrent_access_from_threads(self, tmp_path):
        """Multiple threads can safely write to the same database file."""
        db_path = tmp_path / "concurrent.db"
        errors = []

        def writer(thread_id):
            try:
                db = GlobalDB(db_path)
                for i in range(10):
                    db.register_project(
                        f"/t{thread_id}/p{i}", f"proj-{thread_id}-{i}", "python"
                    )
                db.close()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Errors during concurrent access: {errors}"

        # Verify all projects were registered
        db = GlobalDB(db_path)
        count = db.get_project_count()
        assert count == 40  # 4 threads * 10 projects
        db.close()

    def test_preference_frequency_accumulates_across_many_upserts(self, tmp_path):
        """Frequency accumulates correctly across many upserts."""
        db = _make_db(tmp_path)
        for i in range(20):
            db.upsert_preference("naming", "consistent_signal", f"ex-{i}", f"/proj-{i}")

        prefs = db.get_preferences(min_frequency=1)
        assert prefs[0]["frequency"] == 20
        sources = json.loads(prefs[0]["source_projects"])
        assert len(sources) == 20
        db.close()

    def test_rule_confidence_converges(self, tmp_path):
        """Repeated updates with high confidence converge confidence upward."""
        db = _make_db(tmp_path)
        db.upsert_rule("Converging rule", 0.5, "/a")
        for i in range(10):
            db.upsert_rule("Converging rule", 1.0, f"/p{i}")

        rules = db.get_rules(min_confidence=0.0)
        # After 10 updates with 1.0, confidence should be high
        assert rules[0]["confidence"] > 0.9
        db.close()

    def test_special_sql_chars_in_strings(self, tmp_path):
        """SQL special characters in strings do not cause injection."""
        db = _make_db(tmp_path)
        evil_path = "'; DROP TABLE projects; --"
        db.register_project(evil_path, "Evil", "python")
        assert db.get_project_count() == 1

        evil_rule = "Rule with 'quotes' and \"double quotes\" and --comments"
        db.upsert_rule(evil_rule, 0.5, evil_path)
        rules = db.get_rules(min_confidence=0.0)
        assert rules[0]["rule_text"] == evil_rule
        db.close()

    def test_null_values_in_optional_fields(self, tmp_path):
        """None/NULL in optional fields is handled correctly."""
        db = _make_db(tmp_path)
        db.register_project("/p", "P", "python", git_remote=None)
        db.upsert_preference("cat", "sig", None, "/p")
        db.upsert_rule("rule", 0.5, "/p", None, None)

        prefs = db.get_preferences(min_frequency=1)
        assert prefs[0]["example"] is None

        rules = db.get_rules(min_confidence=0.0)
        assert rules[0]["category"] is None
        assert rules[0]["language"] is None
        db.close()
