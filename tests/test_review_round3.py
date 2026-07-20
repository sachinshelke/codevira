"""
test_review_round3.py — the remaining correctness items from the read-layer
adversarial review.

1. supersede forked a chain. Two stale views (two IDEs, or a retried call) each
   superseded the same decision -> two contradictory ACTIVE decisions and the
   old record's superseded_by overwritten to the last writer.
2. An orphan amendment (amendment line before its base, from a git union-merge)
   was destroyed: the base overwrote it wholesale in read_merged.
3. get_session_context omitted the decision `id`, so an agent could not expand()
   what the brief showed it.
4. id_repair's loser ids (lowercase sha1 hex) were parsed as base36 by the
   next-id computation, ending the readable D0000NN scheme after one merge.
"""

from __future__ import annotations

import pytest

from mcp_server.storage import decisions_store, jsonl_store, paths


@pytest.fixture
def store(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".codevira").mkdir(parents=True)
    (proj / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(proj))
    import mcp_server.paths as paths_mod

    paths_mod.set_project_dir(proj)
    paths_mod.reset_pinned_root()
    paths_mod.invalidate_data_dir_cache()
    yield proj
    paths_mod._project_dir_override = None
    paths_mod.reset_pinned_root()
    paths_mod.invalidate_data_dir_cache()


class TestSupersedeNoFork:
    def test_cannot_supersede_an_already_superseded_decision(self, store):
        d1 = decisions_store.record(decision="use npm")
        r1 = decisions_store.supersede(d1, "use pnpm", "faster")
        assert r1["success"]

        # A second, stale supersede of the SAME original must be refused.
        r2 = decisions_store.supersede(d1, "use yarn", "team pref")
        assert r2["success"] is False
        assert r2.get("superseded_by") == r1["new_id"]

    def test_no_two_active_decisions_after_double_supersede(self, store):
        d1 = decisions_store.record(decision="use npm")
        decisions_store.supersede(d1, "use pnpm", "faster")
        decisions_store.supersede(d1, "use yarn", "stale view")  # refused

        active = [
            d
            for d in decisions_store.list_all(full=True)["decisions"]
            if not d.get("is_superseded") and not d.get("superseded_by")
        ]
        texts = {d["decision"] for d in active}
        assert (
            "use yarn" not in texts
        ), "a forked (contradictory) active decision exists"
        assert "use pnpm" in texts

    def test_superseding_the_head_still_works(self, store):
        """You can keep evolving the chain by superseding its current head."""
        d1 = decisions_store.record(decision="v1")
        r1 = decisions_store.supersede(d1, "v2", "iterate")
        r2 = decisions_store.supersede(r1["new_id"], "v3", "iterate again")
        assert r2["success"] is True


class TestOrphanAmendmentSurvives:
    def test_amendment_before_base_is_not_destroyed(self, store):
        """Reproduce a reordered file (git union-merge): amendment line first."""
        path = paths.decisions_path()
        path.write_text(
            # amendment FIRST, base SECOND
            '{"id":"D1","_amendment_to_id":"D1","do_not_revert":true,'
            '"ts":"2026-02-01T00:00:00+00:00"}\n'
            '{"id":"D1","decision":"lock me","ts":"2026-01-01T00:00:00+00:00"}\n'
        )
        merged = {d["id"]: d for d in jsonl_store.read_merged(path)}

        assert merged["D1"]["decision"] == "lock me", "base content lost"
        assert merged["D1"]["do_not_revert"] is True, "amendment overlay was destroyed"
        assert merged["D1"]["ts"].startswith("2026-01-01"), "base creation ts lost"

    def test_two_real_bases_still_flagged_as_collision(self, store, caplog):
        """The orphan-amendment path must not swallow a genuine base-id
        collision (two BASE records sharing an id)."""
        import logging

        path = paths.decisions_path()
        path.write_text(
            '{"id":"D1","decision":"first"}\n{"id":"D1","decision":"second"}\n'
        )
        with caplog.at_level(logging.WARNING):
            jsonl_store.read_merged(path)
        assert any("collision" in r.message for r in caplog.records)


class TestSessionContextExposesId:
    def test_recent_decisions_include_id(self, store):
        from mcp_server.tools import learning

        rid = decisions_store.record(decision="something worth expanding")
        ctx = learning.get_session_context()

        recents = ctx.get("recent_decisions") or []
        assert recents, "no recent decisions surfaced"
        assert any(
            r.get("id") == rid for r in recents
        ), "the brief shows a decision the agent cannot expand() — no id"


class TestLoserIdDoesNotEndTheScheme:
    def test_next_id_ignores_a_lowercase_loser_id(self, store):
        """A content-derived loser id (lowercase hex) must not inflate the
        sequence — the next real id stays D0000NN, not a 12-char blob."""
        path = paths.decisions_path()
        # D000001 (sequential) + a loser id from id_repair (lowercase sha1 hex).
        path.write_text(
            '{"id":"D000001","decision":"a"}\n{"id":"Dab12cd34ef56","decision":"b"}\n'
        )
        nxt = jsonl_store._compute_next_id_locked(
            path, prefix="D", width=6, id_field="id"
        )
        assert nxt == "D000002", f"loser id ended the readable scheme: next={nxt}"

    def test_real_sequential_max_still_respected(self, store):
        path = paths.decisions_path()
        path.write_text(
            '{"id":"D000001","decision":"a"}\n{"id":"D00000Z","decision":"b"}\n'
        )
        nxt = jsonl_store._compute_next_id_locked(
            path, prefix="D", width=6, id_field="id"
        )
        # 'Z' base36 = 35, so next is 36 -> '10' -> padded 'D000010'.
        assert nxt == "D000010", nxt


class TestLoserIdHealsExistingDamage:
    """The final review's residual: a store poisoned under released v3.7.0 holds
    an UPPERCASE 12-char blob (the old bug re-encoded max_n+1 uppercase), which a
    lowercase-only guard would not heal. A length guard heals both."""

    def test_uppercase_blob_from_prefix_poisoning_is_ignored(self, store):
        path = paths.decisions_path()
        # D000001 + an uppercase blob a pre-fix store would have minted.
        path.write_text(
            '{"id":"D000001","decision":"a"}\n{"id":"DAB12CD34EF57","decision":"b"}\n'
        )
        nxt = jsonl_store._compute_next_id_locked(
            path, prefix="D", width=6, id_field="id"
        )
        assert nxt == "D000002", f"uppercase blob not healed: next={nxt}"

    def test_width_length_sequential_ids_still_counted(self, store):
        """A normal 6-char sequential id must still set the max."""
        path = paths.decisions_path()
        path.write_text('{"id":"D000042","decision":"a"}\n')
        nxt = jsonl_store._compute_next_id_locked(
            path, prefix="D", width=6, id_field="id"
        )
        assert nxt == "D000043", nxt
