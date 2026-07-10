"""
Phase 25-rest — cross-engineer id-collision surfaces.

Covers decisions_store.repair_ids, the `codevira merge-driver` union+repair,
install_merge_driver, and the read_merged base-id-collision warning.
"""

from __future__ import annotations

import json
import logging
import subprocess

from mcp_server.storage import decisions_store, jsonl_store
from mcp_server.storage import paths as store_paths
from mcp_server.cli_repair import cmd_merge_driver, install_merge_driver


def _collide(host, decision, ts):
    return {
        "id": "D000120",
        "decision": decision,
        "ts": ts,
        "origin": {"host_hash": host},
    }


class TestRepairIdsStore:
    def test_report_only_does_not_change_file(self):
        p = store_paths.decisions_path()
        jsonl_store.append(p, _collide("aaa", "alice", "2026-01-01T10:00:00"))
        jsonl_store.append(p, _collide("bbb", "bob", "2026-01-01T10:05:00"))
        before = p.read_text()

        res = decisions_store.repair_ids(apply=False)
        assert res["collisions"] == 1
        assert res["changed"] is True
        assert res["applied"] is False
        assert p.read_text() == before, "report-only must not rewrite the store"

    def test_apply_repairs_and_both_survive(self):
        p = store_paths.decisions_path()
        jsonl_store.append(p, _collide("aaa", "alice decision", "2026-01-01T10:00:00"))
        jsonl_store.append(p, _collide("bbb", "bob decision", "2026-01-01T10:05:00"))

        res = decisions_store.repair_ids(apply=True)
        assert res["applied"] is True

        # No base-id collision remains, and BOTH decisions survive.
        raw = jsonl_store.read_all(p)
        from mcp_server.storage import id_repair

        assert id_repair.find_collisions(raw) == {}
        texts = {r["decision"] for r in raw}
        assert {"alice decision", "bob decision"} <= texts

    def test_clean_store_is_noop(self):
        p = store_paths.decisions_path()
        jsonl_store.append(p, _collide("aaa", "solo", "2026-01-01T10:00:00"))
        res = decisions_store.repair_ids(apply=True)
        assert res["changed"] is False
        assert res["applied"] is False

    def test_apply_preserves_malformed_lines(self):
        """Foolproof: repairing collisions must NEVER silently drop
        unparseable lines (a git-conflict marker, a truncated write). Same
        no-data-loss contract as jsonl_store.compact."""
        p = store_paths.decisions_path()
        jsonl_store.append(p, _collide("aaa", "alice", "2026-01-01T10:00:00"))
        jsonl_store.append(p, _collide("bbb", "bob", "2026-01-01T10:05:00"))
        with open(p, "a", encoding="utf-8") as f:
            f.write("<<<<<<< HEAD this is not json\n")

        res = decisions_store.repair_ids(apply=True)
        assert res["applied"] is True
        text = p.read_text()
        assert (
            "<<<<<<< HEAD this is not json" in text
        ), "malformed line was silently dropped — data loss"
        # And the collision was still repaired.
        raw = jsonl_store.read_all(p)
        from mcp_server.storage import id_repair

        assert id_repair.find_collisions(raw) == {}


class TestMergeDriver:
    def test_union_dedup_and_repair(self, tmp_path):
        # "ours" and "theirs" each have a D000120, plus a shared identical line.
        shared = {"id": "D000100", "decision": "shared", "ts": "2026-01-01T09:00:00"}
        ours = tmp_path / "ours.jsonl"
        theirs = tmp_path / "theirs.jsonl"
        ours.write_text(
            json.dumps(shared)
            + "\n"
            + json.dumps(_collide("aaa", "alice", "2026-01-01T10:00:00"))
            + "\n"
        )
        theirs.write_text(
            json.dumps(shared)
            + "\n"
            + json.dumps(_collide("bbb", "bob", "2026-01-01T10:05:00"))
            + "\n"
        )

        rc = cmd_merge_driver(str(tmp_path / "base"), str(ours), str(theirs))
        assert rc == 0

        merged = jsonl_store.read_all(ours)
        from mcp_server.storage import id_repair

        # Shared line deduped (appears once), collision resolved, all survive.
        assert sum(1 for r in merged if r.get("id") == "D000100") == 1
        assert id_repair.find_collisions(merged) == {}
        texts = {r["decision"] for r in merged}
        assert {"shared", "alice", "bob"} <= texts

    def test_merge_driver_is_deterministic(self, tmp_path):
        a = tmp_path / "a.jsonl"
        b = tmp_path / "b.jsonl"
        content_a = json.dumps(_collide("aaa", "alice", "2026-01-01T10:00:00")) + "\n"
        content_b = json.dumps(_collide("bbb", "bob", "2026-01-01T10:05:00")) + "\n"
        # Run the merge both ways (ours/theirs swapped) — result must converge.
        a.write_text(content_a)
        b.write_text(content_b)
        cmd_merge_driver("x", str(a), str(b))
        forward = sorted(json.dumps(r, sort_keys=True) for r in jsonl_store.read_all(a))

        a.write_text(content_b)
        b.write_text(content_a)
        cmd_merge_driver("x", str(a), str(b))
        backward = sorted(
            json.dumps(r, sort_keys=True) for r in jsonl_store.read_all(a)
        )
        assert forward == backward


class TestInstallMergeDriver:
    def test_installs_gitattributes_and_config(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "init"], check=True, capture_output=True
        )

        res = install_merge_driver(repo)
        assert res["configured"] is True
        ga = (repo / ".gitattributes").read_text()
        assert ".codevira/decisions.jsonl merge=codevira-jsonl" in ga

        got = subprocess.run(
            ["git", "-C", str(repo), "config", "--get", "merge.codevira-jsonl.driver"],
            capture_output=True,
            text=True,
        )
        assert "codevira merge-driver" in got.stdout

    def test_idempotent_no_duplicate_entry(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "init"], check=True, capture_output=True
        )
        install_merge_driver(repo)
        install_merge_driver(repo)
        ga = (repo / ".gitattributes").read_text()
        assert ga.count("merge=codevira-jsonl") == 1

    def test_no_git_repo_is_graceful(self, tmp_path):
        res = install_merge_driver(tmp_path)
        assert res["configured"] is False
        assert res["gitattributes"] is None

    def test_stages_gitattributes_for_sharing(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "init"], check=True, capture_output=True
        )
        install_merge_driver(repo)
        staged = subprocess.run(
            ["git", "-C", str(repo), "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
        )
        assert (
            ".gitattributes" in staged.stdout
        ), ".gitattributes must be staged so teammates inherit the mapping"

    def test_gap_check_warns_on_fresh_clone(self, tmp_path):
        """H4: .gitattributes references the driver but this clone has no driver
        config (the fresh-clone gap) -> warn."""
        from mcp_server.cli_repair import merge_driver_gap

        repo = tmp_path / "clone"
        repo.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "init"], check=True, capture_output=True
        )
        (repo / ".gitattributes").write_text(
            ".codevira/decisions.jsonl merge=codevira-jsonl\n"
        )
        # No driver config yet -> gap.
        assert merge_driver_gap(repo) is not None
        # After install, no gap.
        install_merge_driver(repo)
        assert merge_driver_gap(repo) is None

    def test_gap_check_none_without_gitattributes(self, tmp_path):
        from mcp_server.cli_repair import merge_driver_gap

        repo = tmp_path / "plain"
        repo.mkdir()
        subprocess.run(
            ["git", "-C", str(repo), "init"], check=True, capture_output=True
        )
        assert merge_driver_gap(repo) is None


class TestSemanticDuplicates:
    def test_finds_near_duplicate_pairs(self, monkeypatch):
        # Disable supersede-on-write so both near-dup decisions coexist and can
        # be surfaced by the Tier-1 reporter.
        monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "0")
        from mcp_server.tools import learning

        learning.record_decision(decision="adopt bcrypt hashing for passwords")
        learning.record_decision(
            decision="adopt bcrypt hashing for passwords everywhere", force=True
        )
        learning.record_decision(decision="use postgres for the invoice ledger")

        pairs = decisions_store.find_semantic_duplicates()
        assert len(pairs) >= 1
        assert all(p["similarity"] >= 0.75 for p in pairs)

    def test_no_pairs_when_all_distinct(self, monkeypatch):
        monkeypatch.setenv("CODEVIRA_SUPERSEDE_ON_RECORD", "0")
        from mcp_server.tools import learning

        learning.record_decision(decision="use postgres for the invoice ledger")
        learning.record_decision(decision="render receipts with a monospace font")
        assert decisions_store.find_semantic_duplicates() == []


def test_read_merged_collision_warns_exactly_once(tmp_path, caplog):
    """M9: 60 same-id base records must produce ONE warning per read, not 59
    (which would drown real warnings on every get_session_context/search)."""
    p = tmp_path / "decisions.jsonl"
    lines = [
        json.dumps(
            {
                "id": "D000120",
                "decision": f"d{i}",
                "ts": f"2026-01-01T10:{i % 60:02d}:00",
                "origin": {"host_hash": f"h{i}"},
            }
        )
        for i in range(60)
    ]
    p.write_text("\n".join(lines) + "\n")
    with caplog.at_level(logging.WARNING):
        jsonl_store.read_merged(p)
    warns = [r for r in caplog.records if "base-id collision" in r.message]
    assert len(warns) == 1, f"expected exactly one collision warning, got {len(warns)}"


def test_read_merged_warns_on_base_id_collision(tmp_path, caplog):
    p = tmp_path / "decisions.jsonl"
    p.write_text(
        json.dumps(_collide("aaa", "alice", "2026-01-01T10:00:00"))
        + "\n"
        + json.dumps(_collide("bbb", "bob", "2026-01-01T10:05:00"))
        + "\n"
    )
    with caplog.at_level(logging.WARNING):
        jsonl_store.read_merged(p)
    assert any("base-id collision" in r.message for r in caplog.records)
