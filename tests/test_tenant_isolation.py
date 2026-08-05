"""4.0 Step 10 — the one genuinely breaking change: a tenant key on global.db.

``~/.codevira/global.db`` holds cross-project state — distilled
communication preferences, learned rules. Every row is implicitly "the
person whose $HOME this is", which is correct on a laptop and wrong the
moment the home is shared: a CI runner where several people's jobs run as
one OS user, a devcontainer with a baked $HOME, or codevira running
server-side (the 4.1 direction).

`memory-strategy-2026-07.md:193` lists this as the one true 4.0 breaking
change. It breaks in the READ direction: a 3.x client has no WHERE tenant
clause, so on a shared home it reads every tenant's preferences as its own.

The subtle half is the UNIQUE constraint. `UNIQUE(category, signal)` means
tenant A holding "communication/be-concise" BLOCKS tenant B from storing
it — the upsert finds no row for B, inserts, and hits the constraint.
Without widening that, the tenant column would be decorative.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from indexer.global_db import GlobalDB
from mcp_server import tenant


@pytest.fixture
def db(tmp_path: Path) -> GlobalDB:
    g = GlobalDB(tmp_path / "global.db")
    yield g
    g.close()


class TestTheKeyItself:
    def test_it_defaults_to_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(tenant.TENANT_ENV, raising=False)
        assert tenant.current_tenant() == "local"

    def test_the_env_var_sets_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(tenant.TENANT_ENV, "alice@example.com")
        assert tenant.current_tenant() == "alice@example.com"

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_a_blank_override_is_not_a_tenant(
        self, blank: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An empty string would be a second, invisible partition that no
        other process could ever match."""
        monkeypatch.setenv(tenant.TENANT_ENV, blank)
        assert tenant.current_tenant() == "local"

    def test_it_is_bounded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(tenant.TENANT_ENV, "x" * 5000)
        assert len(tenant.current_tenant()) == tenant.MAX_TENANT_LEN

    def test_it_is_not_the_device_id(self) -> None:
        """A tenant is a PERSON. Keying on device_id would split someone
        with a laptop and a desktop into two tenants whose preferences
        never merge — the opposite of what global.db is for."""
        from mcp_server.storage import origin

        assert tenant.DEFAULT_TENANT != origin.device_id()


class TestTwoTenantsDoNotSeeEachOther:
    def test_preferences_are_scoped(
        self, db: GlobalDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(tenant.TENANT_ENV, "alice")
        db.upsert_preference("communication", "keep answers short", "e", "p1")
        monkeypatch.setenv(tenant.TENANT_ENV, "bob")
        assert db.get_preferences(min_frequency=1) == []

        monkeypatch.setenv(tenant.TENANT_ENV, "alice")
        assert len(db.get_preferences(min_frequency=1)) == 1

    def test_the_same_signal_can_exist_for_both(
        self, db: GlobalDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The UNIQUE-constraint half. Without widening UNIQUE to include
        tenant, Alice storing this signal permanently blocks Bob."""
        for who in ("alice", "bob"):
            monkeypatch.setenv(tenant.TENANT_ENV, who)
            db.upsert_preference("communication", "keep answers short", None, "p1")

        rows = db.conn.execute(
            "SELECT tenant FROM global_preferences WHERE signal = ?",
            ("keep answers short",),
        ).fetchall()
        assert sorted(r[0] for r in rows) == ["alice", "bob"]

    def test_rules_are_scoped(
        self, db: GlobalDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for who in ("alice", "bob"):
            monkeypatch.setenv(tenant.TENANT_ENV, who)
            db.upsert_rule("always write tests first", 0.9, "p1")
        rows = db.conn.execute("SELECT tenant FROM global_rules").fetchall()
        assert sorted(r[0] for r in rows) == ["alice", "bob"]

    def test_reading_rules_does_not_return_another_tenants(
        self, db: GlobalDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The READ is the actual leak. Writing scoped rows means nothing
        if the SELECT has no WHERE tenant."""
        monkeypatch.setenv(tenant.TENANT_ENV, "alice")
        db.upsert_rule("alice's rule", 0.9, "p1")
        monkeypatch.setenv(tenant.TENANT_ENV, "bob")
        assert db.get_rules(min_confidence=0.1) == []
        monkeypatch.setenv(tenant.TENANT_ENV, "alice")
        assert [r["rule_text"] for r in db.get_rules(min_confidence=0.1)] == [
            "alice's rule"
        ]

    def test_language_filtered_rule_reads_are_scoped_too(
        self, db: GlobalDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The language-filtered branch is a separate query; scoping only
        the default branch would leave a hole."""
        monkeypatch.setenv(tenant.TENANT_ENV, "alice")
        db.upsert_rule("py rule", 0.9, "p1", language="python")
        monkeypatch.setenv(tenant.TENANT_ENV, "bob")
        assert db.get_rules(min_confidence=0.1, language="python") == []

    def test_frequency_does_not_bleed_across_tenants(
        self, db: GlobalDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Alice signalling something ten times must not make it look
        established for Bob."""
        monkeypatch.setenv(tenant.TENANT_ENV, "alice")
        for _ in range(10):
            db.upsert_preference("communication", "be terse", None, "p1")
        monkeypatch.setenv(tenant.TENANT_ENV, "bob")
        db.upsert_preference("communication", "be terse", None, "p1")
        got = db.get_preferences(min_frequency=1)
        assert len(got) == 1 and got[0]["frequency"] == 1


class TestASingleUserSeesNoChange:
    """The overwhelmingly common case, and the one least entitled to be
    disrupted by a multi-tenant feature."""

    def test_default_tenant_round_trips(
        self, db: GlobalDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(tenant.TENANT_ENV, raising=False)
        db.upsert_preference("communication", "short answers", None, "p1")
        assert len(db.get_preferences(min_frequency=1)) == 1

    def test_tenancy_is_invisible_unless_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(tenant.TENANT_ENV, raising=False)
        assert tenant.is_shared_home() is False
        monkeypatch.setenv(tenant.TENANT_ENV, "alice")
        assert tenant.is_shared_home() is True

    def test_the_project_registry_is_not_scoped(
        self, db: GlobalDB, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`projects` is an inventory of what is on this FILESYSTEM — a
        property of the machine, not a person. Scoping it would hide a
        colleague's project and make the registry lie."""
        monkeypatch.setenv(tenant.TENANT_ENV, "alice")
        db.register_project("/tmp/shared", "shared", "python")
        monkeypatch.setenv(tenant.TENANT_ENV, "bob")
        assert db.get_project_count() == 1


class TestMigratingARealPre40Database:
    """Nothing may be lost. These build the ACTUAL 3.x schema."""

    @staticmethod
    def _legacy(path: Path) -> None:
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE projects (
                path TEXT PRIMARY KEY, name TEXT NOT NULL, language TEXT,
                git_remote TEXT, last_synced_at DATETIME DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE global_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL,
                signal TEXT NOT NULL, example TEXT, frequency INTEGER DEFAULT 1,
                source_projects TEXT DEFAULT '[]',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category, signal));
            CREATE TABLE global_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_text TEXT NOT NULL UNIQUE, confidence REAL DEFAULT 0.5,
                source_projects TEXT DEFAULT '[]', category TEXT, language TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        """)
        c.execute(
            "INSERT INTO global_preferences (category, signal, frequency) VALUES (?,?,?)",
            ("communication", "existing preference", 7),
        )
        c.execute(
            "INSERT INTO global_rules (rule_text, confidence) VALUES (?,?)",
            ("existing rule", 0.8),
        )
        c.execute("INSERT INTO projects (path, name) VALUES (?,?)", ("/tmp/old", "old"))
        c.commit()
        c.close()

    def test_existing_rows_survive(self, tmp_path: Path) -> None:
        p = tmp_path / "global.db"
        self._legacy(p)
        g = GlobalDB(p)
        try:
            assert (
                g.conn.execute("SELECT COUNT(*) FROM global_preferences").fetchone()[0]
                == 1
            )
            assert (
                g.conn.execute("SELECT COUNT(*) FROM global_rules").fetchone()[0] == 1
            )
            assert g.get_project_count() == 1
        finally:
            g.close()

    def test_existing_rows_become_the_local_tenant(self, tmp_path: Path) -> None:
        """Not a guess: those rows DO belong to exactly one person."""
        p = tmp_path / "global.db"
        self._legacy(p)
        g = GlobalDB(p)
        try:
            assert (
                g.conn.execute("SELECT tenant FROM global_preferences").fetchone()[0]
                == "local"
            )
            assert (
                g.conn.execute("SELECT tenant FROM global_rules").fetchone()[0]
                == "local"
            )
        finally:
            g.close()

    def test_a_single_user_still_reads_their_own_data_after_upgrade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The upgrade must not orphan someone's learned preferences."""
        monkeypatch.delenv(tenant.TENANT_ENV, raising=False)
        p = tmp_path / "global.db"
        self._legacy(p)
        g = GlobalDB(p)
        try:
            got = g.get_preferences(min_frequency=1)
            assert [r["signal"] for r in got] == ["existing preference"]
        finally:
            g.close()

    def test_the_unique_constraint_is_widened(self, tmp_path: Path) -> None:
        p = tmp_path / "global.db"
        self._legacy(p)
        g = GlobalDB(p)
        try:
            for table in ("global_preferences", "global_rules"):
                sql = g.conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name=?", (table,)
                ).fetchone()[0]
                assert "UNIQUE(tenant" in sql.replace(" ", "").replace("\n", ""), table
        finally:
            g.close()

    def test_migration_is_idempotent(self, tmp_path: Path) -> None:
        """It runs on EVERY open. A second pass must be a no-op, not a
        second rebuild."""
        p = tmp_path / "global.db"
        self._legacy(p)
        for _ in range(3):
            g = GlobalDB(p)
            g.close()
        g = GlobalDB(p)
        try:
            assert (
                g.conn.execute("SELECT COUNT(*) FROM global_preferences").fetchone()[0]
                == 1
            )
            leftovers = g.conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%__new'"
            ).fetchall()
            assert leftovers == [], "a failed swap left a temp table behind"
        finally:
            g.close()

    def test_an_already_migrated_db_is_untouched(self, tmp_path: Path) -> None:
        g = GlobalDB(tmp_path / "global.db")
        g.upsert_preference("communication", "x", None, "p1")
        g.close()
        g2 = GlobalDB(tmp_path / "global.db")
        try:
            assert len(g2.get_preferences(min_frequency=1)) == 1
        finally:
            g2.close()
