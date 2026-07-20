"""
test_retirement_and_amendments.py — read-layer defects: retiring a decision did
not retire it, and amendments rewrote creation time.

**Retirement was cosmetic.** ``mark_decision_outdated`` correctly hid a decision
from ``search`` / ``list_all``, but the three index regenerators (manifest,
digest, FTS5) and the AGENTS.md writer never checked ``is_outdated``. So a
retired decision was still injected into every prompt by ``relevance_inject``
and still PUBLISHED to AGENTS.md — the file Cursor, Copilot, Codex and Windsurf
read. Retiring has to retire everywhere.

**Indexes read raw lines.** manifest/digest/FTS5 used ``read_all`` instead of
``read_merged``, so amendment records were treated as decisions: unprotecting a
decision left it in ``do_not_revert_ids`` forever, and the digest emitted a row
built from the amendment alone — text-less — which `relevance_inject` (later row
wins per id) injected as ``🔒 **D000001**`` with no content.

**Amendments overwrote `ts`.** The overlay copied the amendment's timestamp over
the base's creation time, so tagging an old decision made it look new — and
``compute_dnr_soft_expire`` reads ``max(reaffirmed_at, ts)``, so any unrelated
edit silently reset the 180-day do_not_revert staleness clock.
"""

from __future__ import annotations

import pytest

from mcp_server.storage import decisions_store, jsonl_store, manifest, paths


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


class TestRetiringActuallyRetires:
    def test_outdated_decision_leaves_the_manifest(self, store):
        """It stayed in tags/files, so relevance_inject kept surfacing it."""
        rec_id = decisions_store.record(
            decision="old approach", tags=["infra"], file_path="a.py"
        )
        decisions_store.mark_outdated(rec_id, reason="superseded by reality")
        decisions_store.rebuild_indexes()

        m = manifest.load(paths.manifest_path())
        assert rec_id not in str(m.get("tags", {})), "retired decision still tagged"
        assert rec_id not in str(m.get("files", {})), "retired decision still filed"

    def test_outdated_decision_leaves_the_digest(self, store):
        """The digest is what relevance_inject injects into every prompt."""
        rec_id = decisions_store.record(decision="old approach", tags=["infra"])
        decisions_store.mark_outdated(rec_id, reason="no longer true")
        decisions_store.rebuild_indexes()

        rows = jsonl_store.read_all(paths.digest_path())
        assert all(
            r.get("id") != rec_id for r in rows
        ), "retired decision is still injected into every prompt"

    def test_outdated_decision_leaves_agents_md(self, store):
        """AGENTS.md is read by Cursor / Copilot / Codex / Windsurf.

        force=True because retiring a do_not_revert decision requires it.
        """
        from mcp_server.storage import agents_md_generator

        rec_id = decisions_store.record(
            decision="RETIRED-MARKER-TEXT must not be published", do_not_revert=True
        )
        decisions_store.mark_outdated(rec_id, reason="retired", force=True)
        agents_md_generator.regenerate()

        text = (store / "AGENTS.md").read_text()
        assert (
            "RETIRED-MARKER-TEXT" not in text
        ), "a retired decision is still published to other AI tools"

    def test_active_decision_is_still_published(self, store):
        from mcp_server.storage import agents_md_generator

        decisions_store.record(decision="ACTIVE-MARKER-TEXT stays", do_not_revert=True)
        agents_md_generator.regenerate()
        assert "ACTIVE-MARKER-TEXT" in (store / "AGENTS.md").read_text()


class TestIndexesFoldAmendments:
    def test_unprotecting_clears_do_not_revert_ids(self, store):
        """Reading raw lines left the id in do_not_revert_ids forever."""
        rec_id = decisions_store.record(decision="lock me", do_not_revert=True)
        decisions_store.set_flag(rec_id, do_not_revert=False)
        decisions_store.rebuild_indexes()

        m = manifest.load(paths.manifest_path())
        assert rec_id not in (
            m.get("do_not_revert_ids") or []
        ), "unprotecting a decision never took effect in the manifest"

    def test_amended_decision_keeps_its_text_in_the_digest(self, store):
        """The amendment row won per-id and had no text, so the decision was
        injected as `🔒 **D...**` with nothing after it."""
        rec_id = decisions_store.record(decision="keep this text visible")
        decisions_store.set_flag(rec_id, do_not_revert=True)
        decisions_store.rebuild_indexes()

        rows = [
            r
            for r in jsonl_store.read_all(paths.digest_path())
            if r.get("id") == rec_id
        ]
        assert rows, "decision vanished from the digest"
        assert rows[-1].get("summary"), "digest row has no text — injects as noise"

    def test_amendments_are_not_counted_as_decisions(self, store):
        rec_id = decisions_store.record(decision="one real decision")
        decisions_store.set_flag(rec_id, do_not_revert=True)
        decisions_store.rebuild_indexes()

        m = manifest.load(paths.manifest_path())
        assert (
            m.get("total_decisions") == 1
        ), f"amendment counted as a decision: {m.get('total_decisions')}"


class TestAmendmentsPreserveCreationTime:
    def test_ts_survives_an_amendment(self, store):
        """Tagging an old decision must not make it look created today."""
        path = paths.decisions_path()
        path.write_text(
            '{"id":"D1","decision":"old","ts":"2024-01-01T00:00:00+00:00"}\n'
        )
        jsonl_store.append(
            path,
            {
                "id": "D1",
                "_amendment_to_id": "D1",
                "ts": "2026-07-20T00:00:00+00:00",
                "do_not_revert": True,
            },
        )

        merged = {d["id"]: d for d in jsonl_store.read_merged(path)}
        assert merged["D1"]["ts"].startswith("2024-01-01"), (
            "amendment overwrote creation time — the decision jumps to the top "
            "of recent-decisions and resets the do_not_revert staleness clock"
        )
        assert merged["D1"]["do_not_revert"] is True, "amendment was not applied"

    def test_amendment_specific_fields_still_apply(self, store):
        """Only `ts` is protected; everything else the amendment carries wins."""
        path = paths.decisions_path()
        path.write_text('{"id":"D1","decision":"x","ts":"2024-01-01T00:00:00+00:00"}\n')
        jsonl_store.append(
            path,
            {
                "id": "D1",
                "_amendment_to_id": "D1",
                "reaffirmed_at": "2026-07-20T00:00:00+00:00",
            },
        )

        merged = {d["id"]: d for d in jsonl_store.read_merged(path)}
        assert merged["D1"]["reaffirmed_at"].startswith("2026-07-20")


class TestTsHealingEdgeCase:
    def test_amendment_heals_a_missing_base_ts(self, store):
        """A base record with NO ts must not merge to ts=None: the amendment's
        timestamp heals it (else it sorts to the bottom, is dropped by since=,
        and never soft-expires)."""
        path = paths.decisions_path()
        path.write_text('{"id":"D1","decision":"no ts here"}\n')  # base, no ts
        jsonl_store.append(
            path,
            {
                "id": "D1",
                "_amendment_to_id": "D1",
                "ts": "2026-07-20T00:00:00+00:00",
                "do_not_revert": True,
            },
        )

        merged = {d["id"]: d for d in jsonl_store.read_merged(path)}
        assert (
            merged["D1"].get("ts") == "2026-07-20T00:00:00+00:00"
        ), "missing base ts was not healed by the amendment"

    def test_present_base_ts_still_wins(self, store):
        """The common case is unchanged: a real creation ts survives."""
        path = paths.decisions_path()
        path.write_text('{"id":"D1","decision":"x","ts":"2024-01-01T00:00:00+00:00"}\n')
        jsonl_store.append(
            path,
            {"id": "D1", "_amendment_to_id": "D1", "ts": "2026-07-20T00:00:00+00:00"},
        )
        merged = {d["id"]: d for d in jsonl_store.read_merged(path)}
        assert merged["D1"]["ts"].startswith("2024-01-01")
