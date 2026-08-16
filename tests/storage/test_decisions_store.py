"""
Tests for mcp_server.storage.decisions_store.

Scope: behaviors and contracts owned directly by decisions_store
(amendment overlay delegation, session_id default generation).
Higher-level surfaces — record_decision MCP tool, conflict checks,
session-context aggregation — are exercised in
``tests/test_tools_learning.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import decisions_store


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fresh temp project rooted at ``tmp_path`` so decisions land in an
    isolated ``.codevira/decisions.jsonl``."""
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


class TestDefaultSessionId:
    """v3.0.1 fix: prior to this, the unattributed default was the
    literal string ``"ad-hoc"``. Every concurrent IDE that didn't pass
    a slug collided into one bucket — masking session boundaries and
    breaking the v3.1.0 working-memory design which keys by session_id.
    """

    _PATTERN = re.compile(r"^ad-hoc-[0-9a-f]{6}$")

    def test_helper_is_stable_within_a_process(self) -> None:
        """4.0 Step 3.2 INVERTS the v3.0.1 contract, deliberately.

        v3.0.1 made the id unique PER CALL so two unattributed writes
        could be told apart. That is not a session id — it is a write id,
        and it made every session-scoped join impossible: measured on the
        dogfood repo, 754 activity rows carried 754 distinct ids and 0 of
        116 decisions shared a session with an edit row.

        The v3.0.1 concern is still honoured: concurrent CLIENTS get
        distinct ids, because each carries its own client session id (or,
        absent one, its own process-stable fallback).

        Known limitation, stated rather than hidden: two concurrent
        sessions of the SAME client in the SAME project both write to
        active_sessions.jsonl, and the latest marker wins — so their
        writes can share an id. That is narrower and more useful than the
        previous behaviour, where the id belonged to neither.
        """
        slug1 = decisions_store.default_session_id()
        slug2 = decisions_store.default_session_id()
        assert slug1 == slug2
        assert self._PATTERN.match(slug1), slug1

    def test_helper_never_returns_literal_ad_hoc(self) -> None:
        """Catches a regression where someone short-circuits the helper
        back to the old literal.
        """
        for _ in range(20):
            assert decisions_store.default_session_id() != "ad-hoc"

    def test_record_without_session_id_uses_new_default(self, project: Path) -> None:
        """End-to-end: two record() calls with no session_id now yield the
        SAME on-disk session_id — that is what makes them joinable to the
        edits from the same session. Never the literal "ad-hoc".
        """
        from mcp_server.storage import jsonl_store, paths

        decisions_store.record(decision="First decision under no slug")
        decisions_store.record(decision="Second decision under no slug")

        raw = jsonl_store.read_all(paths.decisions_path())
        # Only count base records (amendments share a session_id with the
        # base they amend; v3.0.0 records have no amendments at this
        # point in the test).
        sessions = [r.get("session_id") for r in raw if not r.get("_amendment_to_id")]
        assert len(sessions) == 2
        assert sessions[0] == sessions[1], (
            "two unattributed record() calls in one session must share a "
            "session_id — otherwise nothing can be joined to the edits "
            "from that session (4.0 Step 3.2)"
        )
        assert all(s != "ad-hoc" for s in sessions), "v3.0.1 literal regression"
        assert all(self._PATTERN.match(s) for s in sessions), sessions

    def test_record_with_explicit_session_id_preserved(self, project: Path) -> None:
        """Explicit session_id from the caller wins over the default
        generator. (No silent overwrite — agents that DO group their
        work keep their grouping.)
        """
        from mcp_server.storage import jsonl_store, paths

        decisions_store.record(decision="Grouped decision A", session_id="morning-auth")
        decisions_store.record(decision="Grouped decision B", session_id="morning-auth")

        raw = jsonl_store.read_all(paths.decisions_path())
        sessions = [r.get("session_id") for r in raw if not r.get("_amendment_to_id")]
        assert sessions == ["morning-auth", "morning-auth"]

    def test_record_many_shares_one_slug_for_unattributed(self, project: Path) -> None:
        """``record_many`` with mixed explicit + missing session_ids:
        explicit ones are preserved; the unattributed siblings share the
        session's id so they stay joinable (4.0 Step 3.2 — previously each
        got its own random slug, which made them un-correlatable).
        """
        from mcp_server.storage import jsonl_store, paths

        decisions_store.record_many(
            [
                {"decision": "Explicit slug A", "session_id": "explicit-1"},
                {"decision": "No slug 1"},
                {"decision": "No slug 2"},
                {"decision": "Explicit slug B", "session_id": "explicit-2"},
            ]
        )

        raw = jsonl_store.read_all(paths.decisions_path())
        sessions = [r.get("session_id") for r in raw if not r.get("_amendment_to_id")]
        assert sessions[0] == "explicit-1"
        assert self._PATTERN.match(sessions[1])
        assert self._PATTERN.match(sessions[2])
        assert sessions[1] == sessions[2], (
            "unattributed siblings recorded in one call must share a session"
        )
        assert sessions[3] == "explicit-2"


class TestOriginTagging:
    """v3.1.0 M1: every decision write carries origin: {ide,
    agent_model, host_hash, ts}. Reads tolerate absence on legacy
    v3.0.x records (treated as ide="unknown").
    """

    def test_record_stamps_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "claude_code")
        decisions_store.record(decision="Use rate limiting")

        from mcp_server.storage import jsonl_store, paths

        rows = jsonl_store.read_all(paths.decisions_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        assert len(bases) == 1
        origin_field = bases[0].get("origin")
        assert origin_field is not None
        assert origin_field["ide"] == "claude_code"
        assert "host_hash" in origin_field and len(origin_field["host_hash"]) == 12
        assert "ts" in origin_field

    def test_record_many_stamps_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        decisions_store.record_many(
            [{"decision": "A"}, {"decision": "B"}, {"decision": "C"}]
        )

        from mcp_server.storage import jsonl_store, paths

        rows = jsonl_store.read_all(paths.decisions_path())
        for r in rows:
            if r.get("_amendment_to_id"):
                continue
            assert r["origin"]["ide"] == "cursor"

    def test_ide_unknown_when_env_unset(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CODEVIRA_IDE", raising=False)
        decisions_store.record(decision="Anonymous write")

        from mcp_server.storage import jsonl_store, paths

        rows = jsonl_store.read_all(paths.decisions_path())
        bases = [r for r in rows if not r.get("_amendment_to_id")]
        assert bases[0]["origin"]["ide"] == "unknown"

    def test_backcompat_record_without_origin(self, project: Path) -> None:
        """Hand-craft a legacy v3.0.x record (no ``origin`` field) and
        verify every read path tolerates absence. This is the
        regression test for the M1 promise that legacy records read
        as ide="unknown" without crashing.
        """
        from mcp_server.storage import jsonl_store, paths

        legacy = {
            "id": "D000001",
            "ts": "2026-05-01T00:00:00Z",
            "session_id": "ad-hoc",  # the OLD literal default
            "file_path": None,
            "decision": "Legacy decision pre-3.1",
            "context": None,
            "do_not_revert": False,
            "tags": [],
            "supersedes": None,
            "superseded_by": None,
            "outcome": None,
            # NOTE: no "origin" field — legacy 3.0.x shape
        }
        jsonl_store.append(paths.decisions_path(), legacy)

        # Reads via the merged view: legacy record surfaces, origin missing.
        merged = decisions_store._read_merged()
        assert len(merged) == 1
        assert "origin" not in merged[0] or merged[0].get("origin") is None

        # Now write a NEW decision via the dev path — the new one carries origin,
        # legacy doesn't. Both must coexist in subsequent reads.
        decisions_store.record(decision="New decision under 3.1.0")
        merged = decisions_store._read_merged()
        assert len(merged) == 2
        new_rec = next(r for r in merged if "New decision" in r["decision"])
        assert new_rec["origin"]["host_hash"]
        legacy_rec = next(r for r in merged if "Legacy decision" in r["decision"])
        assert legacy_rec.get("origin") is None  # untouched

    def test_search_surfaces_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """decisions_store.search() includes origin per candidate so
        check_conflict can surface provenance."""
        monkeypatch.setenv("CODEVIRA_IDE", "windsurf")
        decisions_store.record(
            decision="Migrate database to PostgreSQL", tags=["db", "migration"]
        )

        hits = decisions_store.search("PostgreSQL migration", limit=5)
        assert len(hits) >= 1
        assert hits[0]["origin"]["ide"] == "windsurf"


# ──────────────────────────────────────────────────────────────────────
# v3.1.x — secret sanitization at write
# ──────────────────────────────────────────────────────────────────────


class TestDecisionSecretSanitization:
    """decisions_store.record now scrubs api-key / Bearer / password /
    AWS AKIA / long hex / long base64 in both the decision text and the
    optional context field. A pasted curl example or stack trace must
    NOT land its secret in decisions.jsonl (committed)."""

    def test_decision_text_secret_redacted(self, project: Path) -> None:
        did = decisions_store.record(
            decision="see api_key=hunter2-deadbeefcafedeadbeef for endpoint",
            tags=["test"],
        )
        rec = decisions_store.get(did)
        assert rec is not None
        assert "hunter2-deadbeefcafedeadbeef" not in rec["decision"]
        assert "<redacted:api-key>" in rec["decision"]

    def test_context_secret_redacted(self, project: Path) -> None:
        did = decisions_store.record(
            decision="use bcrypt",
            context="Authorization: Bearer abc123XYZdef456GHIjkl789",
            tags=["auth"],
        )
        rec = decisions_store.get(did)
        assert rec.get("context") is not None
        assert "abc123XYZdef456GHIjkl789" not in rec["context"]
        assert "<redacted:bearer>" in rec["context"]

    def test_clean_text_passes_through_unchanged(self, project: Path) -> None:
        did = decisions_store.record(
            decision="Use snake_case for Python variables.",
            context="Matches PEP8.",
        )
        rec = decisions_store.get(did)
        assert rec["decision"] == "Use snake_case for Python variables."
        assert rec["context"] == "Matches PEP8."


# ──────────────────────────────────────────────────────────────────────
# v3.1.x — counter-decision discipline (P4 + M2)
# ──────────────────────────────────────────────────────────────────────


class TestCounterDecisionFields:
    """alternatives_considered + would_re_examine_if are NEW v3.1.x
    fields on decisions_store.record. Legacy v3.0.x decisions are
    tolerated (fields absent / null on read)."""

    def test_alternatives_and_re_examine_persisted(self, project: Path) -> None:
        did = decisions_store.record(
            decision="Use bcrypt for password hashing",
            tags=["auth"],
            alternatives_considered=[
                "argon2id (rejected: heavier on cheap mobile clients)",
                "scrypt (rejected: less well-vetted in our ecosystem)",
            ],
            would_re_examine_if=(
                "if argon2id native bindings ship in the stdlib OR "
                "if we move off mobile clients"
            ),
        )
        rec = decisions_store.get(did)
        assert len(rec["alternatives_considered"]) == 2
        assert "argon2id" in rec["alternatives_considered"][0]
        assert "would_re_examine_if" in rec
        assert "argon2id" in rec["would_re_examine_if"]

    def test_defaults_when_not_provided(self, project: Path) -> None:
        did = decisions_store.record(decision="trivial", tags=["x"])
        rec = decisions_store.get(did)
        # New fields exist on every fresh write (empty list / None).
        assert rec["alternatives_considered"] == []
        assert rec["would_re_examine_if"] is None

    def test_alternatives_are_sanitized(self, project: Path) -> None:
        """A 'rejected option' string can leak a secret just like the
        chosen decision can."""
        did = decisions_store.record(
            decision="use OAuth2",
            alternatives_considered=[
                "static api_key=hunter2-deadbeefcafedeadbeef in env"
            ],
            would_re_examine_if=("if Bearer abc123XYZdef456GHIjkl789 expires"),
        )
        rec = decisions_store.get(did)
        assert "hunter2-deadbeefcafedeadbeef" not in rec["alternatives_considered"][0]
        assert "<redacted:api-key>" in rec["alternatives_considered"][0]
        assert "abc123XYZ" not in rec["would_re_examine_if"]
        assert "<redacted:bearer>" in rec["would_re_examine_if"]

    def test_empty_strings_dropped_from_alternatives(self, project: Path) -> None:
        did = decisions_store.record(
            decision="x",
            alternatives_considered=["valid", "", "  ", "also valid"],
        )
        rec = decisions_store.get(did)
        assert rec["alternatives_considered"] == ["valid", "also valid"]

    def test_record_decision_tool_threads_new_fields(self, project: Path) -> None:
        from mcp_server.tools.learning import record_decision

        r = record_decision(
            decision="x",
            alternatives_considered=["alt1", "alt2"],
            would_re_examine_if="if condition X holds",
        )
        assert r["recorded"] is True
        rec = decisions_store.get(r["decision_id"])
        assert rec["alternatives_considered"] == ["alt1", "alt2"]
        assert rec["would_re_examine_if"] == "if condition X holds"


# ──────────────────────────────────────────────────────────────────────
# v3.2.0 — reaffirm + do_not_revert soft-expire
# ──────────────────────────────────────────────────────────────────────


class TestReaffirmAndSoftExpire:
    def test_reaffirm_appends_amendment_with_timestamp(self, project: Path) -> None:
        decision_id = decisions_store.record(
            decision="lock me",
            do_not_revert=True,
        )
        result = decisions_store.reaffirm(decision_id)
        assert result["success"] is True
        assert result["decision_id"] == decision_id
        assert isinstance(result["reaffirmed_at"], str)

        rec = decisions_store.get(decision_id)
        assert rec["reaffirmed_at"] == result["reaffirmed_at"]
        # do_not_revert flag is preserved (amendment overlay merges)
        assert rec["do_not_revert"] is True

    def test_reaffirm_missing_decision_returns_error(self, project: Path) -> None:
        result = decisions_store.reaffirm("D999999")
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_compute_dnr_soft_expire_not_protected_is_never_expired(
        self,
        project: Path,
    ) -> None:
        decision_id = decisions_store.record(decision="x", do_not_revert=False)
        rec = decisions_store.get(decision_id)
        result = decisions_store.compute_dnr_soft_expire(rec, max_age_days=1)
        assert result["soft_expired"] is False
        # age_days still computed for observability
        assert result["age_days"] is not None and result["age_days"] >= 0

    def test_compute_dnr_soft_expire_fresh_protected_not_expired(
        self,
        project: Path,
    ) -> None:
        decision_id = decisions_store.record(decision="x", do_not_revert=True)
        rec = decisions_store.get(decision_id)
        result = decisions_store.compute_dnr_soft_expire(rec, max_age_days=180)
        assert result["soft_expired"] is False
        assert result["age_days"] == 0
        assert result["max_age_days"] == 180

    def test_compute_dnr_soft_expire_old_protected_is_expired(
        self,
        project: Path,
    ) -> None:
        # Use a synthetic record with an old ts directly.
        synthetic = {
            "id": "D000099",
            "ts": "2020-01-01T00:00:00+00:00",
            "do_not_revert": True,
        }
        result = decisions_store.compute_dnr_soft_expire(
            synthetic,
            max_age_days=180,
        )
        assert result["soft_expired"] is True
        assert result["age_days"] > 180

    def test_compute_dnr_soft_expire_reaffirmed_resets_clock(
        self,
        project: Path,
    ) -> None:
        from datetime import datetime, timezone

        synthetic = {
            "id": "D000099",
            "ts": "2020-01-01T00:00:00+00:00",
            "reaffirmed_at": datetime.now(timezone.utc).isoformat(),
            "do_not_revert": True,
        }
        result = decisions_store.compute_dnr_soft_expire(
            synthetic,
            max_age_days=180,
        )
        assert result["soft_expired"] is False
        assert result["age_days"] == 0

    def test_compute_dnr_soft_expire_threshold_zero_disables(
        self,
        project: Path,
    ) -> None:
        synthetic = {
            "id": "D000099",
            "ts": "2020-01-01T00:00:00+00:00",
            "do_not_revert": True,
        }
        result = decisions_store.compute_dnr_soft_expire(
            synthetic,
            max_age_days=0,
        )
        assert result["soft_expired"] is False  # disabled
        # But age_days still reported for observability
        assert result["age_days"] > 0

    def test_compute_dnr_soft_expire_no_ts_returns_none_age(
        self,
        project: Path,
    ) -> None:
        synthetic = {"id": "D000099", "do_not_revert": True}
        result = decisions_store.compute_dnr_soft_expire(
            synthetic,
            max_age_days=180,
        )
        assert result["soft_expired"] is False
        assert result["age_days"] is None
        assert result["effective_ts"] is None

    def test_dnr_soft_expire_days_env_var_override(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEVIRA_DNR_SOFT_EXPIRE_DAYS", "30")
        assert decisions_store.dnr_soft_expire_days() == 30

    def test_dnr_soft_expire_days_env_unset_uses_default(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("CODEVIRA_DNR_SOFT_EXPIRE_DAYS", raising=False)
        # Default is 180 days per the module constant
        assert decisions_store.dnr_soft_expire_days() == 180

    def test_dnr_soft_expire_days_bogus_env_falls_back(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEVIRA_DNR_SOFT_EXPIRE_DAYS", "nonsense")
        assert decisions_store.dnr_soft_expire_days() == 180

    def test_dnr_soft_expire_days_negative_falls_back(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CODEVIRA_DNR_SOFT_EXPIRE_DAYS", "-5")
        assert decisions_store.dnr_soft_expire_days() == 180


class TestReaffirmDecisionTool:
    """v3.2.0 MCP tool surface."""

    def test_reaffirm_tool_calls_storage(self, project: Path) -> None:
        from mcp_server.tools.learning import reaffirm_decision

        decision_id = decisions_store.record(decision="x", do_not_revert=True)
        result = reaffirm_decision(decision_id)
        assert result["success"] is True
        assert result["decision_id"] == decision_id
        assert "reaffirmed_at" in result

        # Persists via the storage layer
        rec = decisions_store.get(decision_id)
        assert rec["reaffirmed_at"] == result["reaffirmed_at"]

    def test_reaffirm_tool_rejects_empty_id(self, project: Path) -> None:
        from mcp_server.tools.learning import reaffirm_decision

        result = reaffirm_decision("")
        assert result["success"] is False
        assert "non-empty string" in result["error"]

    def test_reaffirm_tool_rejects_non_string_id(self, project: Path) -> None:
        from mcp_server.tools.learning import reaffirm_decision

        result = reaffirm_decision(None)  # type: ignore[arg-type]
        assert result["success"] is False

    def test_reaffirm_tool_propagates_not_found(self, project: Path) -> None:
        from mcp_server.tools.learning import reaffirm_decision

        result = reaffirm_decision("D999999")
        assert result["success"] is False
        assert "not found" in result["error"]


class TestFtsWriteFailureIsRecoverable:
    """Phase 24, second half: a failed FTS5 index write must not leave a
    decision permanently unsearchable.

    ``record()`` appends to decisions.jsonl FIRST and only then updates the
    index, deliberately — line 403 there is explicit that the index update is
    best-effort and must "never fail the write" (P9). The decision is durable
    the moment the append returns, so raising afterwards would be worse than
    the bug: the caller would believe the record failed, retry, and duplicate
    it.

    The real defect is recovery. ``staleness_check`` compares mtimes with a
    1-second epsilon (fts5_index.py:350) for filesystems with second-precision
    timestamps. A decision appended within that second of the last rebuild,
    whose index write then fails, leaves an index that reports itself FRESH —
    so no rebuild fires and the row never appears. If nothing else is recorded
    for a while, that decision is invisible to search indefinitely.
    """

    def test_decision_is_searchable_after_a_failed_index_write(
        self, project, monkeypatch
    ):
        from mcp_server.storage import fts5_index

        # A first decision, indexed normally: this is what stamps a RECENT
        # source_mtime into the index meta and arms the 1-second epsilon.
        decisions_store.record(decision="adopt structured logging everywhere")
        assert decisions_store.search("structured logging", limit=5), (
            "precondition: a healthy index finds the first decision"
        )

        def _boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(fts5_index, "add_decision", _boom)
        decisions_store.record(decision="pin the tokenizer to a fixed revision")
        monkeypatch.undo()

        # No sleep: the whole test runs well inside the epsilon, which is
        # precisely the window the bug lives in.
        hits = decisions_store.search("tokenizer fixed revision", limit=5)
        assert any("tokenizer" in (h.get("decision") or "") for h in hits), (
            "a decision whose index write failed must still be reachable via "
            "search — the index has to know it is stale and rebuild"
        )

    def test_record_many_also_recovers(self, project, monkeypatch):
        from mcp_server.storage import fts5_index

        decisions_store.record(decision="adopt structured logging everywhere")
        assert decisions_store.search("structured logging", limit=5)

        def _boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(fts5_index, "add_decision", _boom)
        decisions_store.record_many([{"decision": "vendor the wasm toolchain"}])
        monkeypatch.undo()

        hits = decisions_store.search("vendor wasm toolchain", limit=5)
        assert any("wasm" in (h.get("decision") or "") for h in hits), (
            "record_many carries the same best-effort index write and needs "
            "the same recovery"
        )


class TestSoftExpireIsSurfaced:
    """v3.2.0 shipped ``compute_dnr_soft_expire`` and three docstrings saying
    the result was surfaced on search / list output. It was not — the helper
    had zero production consumers, so a long-locked decision looked exactly
    like a fresh one to every reader. 4.0.1 wires it in.

    Emitted only for ``do_not_revert`` decisions: ``soft_expired`` is
    definitionally False without a lock to expire, so emitting the pair on
    every row would be pure token cost on a surface that is explicitly
    summary-by-default.
    """

    _OLD = "2020-01-01T00:00:00+00:00"

    def _seed_old_locked(self, decision: str) -> str:
        from mcp_server.storage import jsonl_store
        from mcp_server.storage import paths as _paths

        rec = {
            "id": "D000900",
            "decision": decision,
            "do_not_revert": True,
            "ts": self._OLD,
            "session_id": "seed",
            "tags": [],
        }
        jsonl_store.append(_paths.decisions_path(), rec)
        return "D000900"

    def test_search_surfaces_soft_expire_on_a_stale_lock(self, project) -> None:
        self._seed_old_locked("never rewrite the wire protocol")
        hits = decisions_store.search("wire protocol", limit=5)
        assert hits, "precondition: the seeded decision is searchable"
        hit = hits[0]
        assert hit["dnr_soft_expired"] is True
        assert hit["dnr_age_days"] > 180

    def test_list_all_surfaces_soft_expire_on_a_stale_lock(self, project) -> None:
        self._seed_old_locked("never rewrite the wire protocol")
        rows = decisions_store.list_all(limit=5)["decisions"]
        row = next(r for r in rows if r["id"] == "D000900")
        assert row["dnr_soft_expired"] is True
        assert row["dnr_age_days"] > 180

    def test_list_all_full_shape_surfaces_it_too(self, project) -> None:
        self._seed_old_locked("never rewrite the wire protocol")
        rows = decisions_store.list_all(limit=5, full=True)["decisions"]
        row = next(r for r in rows if r["id"] == "D000900")
        assert row["dnr_soft_expired"] is True, (
            "full=True must not be a downgrade — it returns MORE, not less"
        )

    def test_a_fresh_lock_is_flagged_not_expired(self, project) -> None:
        decisions_store.record(decision="pin the wire protocol", do_not_revert=True)
        hit = decisions_store.search("wire protocol", limit=5)[0]
        assert hit["dnr_soft_expired"] is False
        assert hit["dnr_age_days"] == 0

    def test_unprotected_decisions_carry_no_soft_expire_keys(self, project) -> None:
        decisions_store.record(decision="pin the wire protocol", do_not_revert=False)
        hit = decisions_store.search("wire protocol", limit=5)[0]
        assert "dnr_soft_expired" not in hit, (
            "an unlocked decision has no lock to expire; the keys are omitted "
            "so the common path costs nothing"
        )
        assert "dnr_age_days" not in hit

    def test_threshold_zero_disables_the_flag(self, project, monkeypatch) -> None:
        monkeypatch.setenv("CODEVIRA_DNR_SOFT_EXPIRE_DAYS", "0")
        self._seed_old_locked("never rewrite the wire protocol")
        hit = decisions_store.search("wire protocol", limit=5)[0]
        assert hit["dnr_soft_expired"] is False, "0 means disabled, per the docs"
        assert hit["dnr_age_days"] > 180, "age stays observable even when disabled"
