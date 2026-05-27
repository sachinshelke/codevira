"""Unit tests for mcp_server.storage.manifest.

Covers:
- load: missing file → empty manifest with correct schema_version
- save: write-tmp + rename atomicity
- regenerate: full rebuild from decisions.jsonl
- incremental_add: O(1) update; idempotent on duplicate
- Tag normalization (lowercase, strip whitespace)
- do_not_revert_ids list maintenance
- Superseded decisions excluded from active counts
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.storage import jsonl_store, manifest


class TestLoadAndSave:
    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        m = manifest.load(tmp_path / "nonexistent.yaml")
        assert m["schema_version"] == 1
        assert m["total_decisions"] == 0
        assert m["tags"] == {}

    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        m = manifest._empty_manifest()
        m["tags"] = {"security": ["D000001"]}
        m["files"] = {"auth.py": ["D000001"]}
        manifest.save(path, m)

        loaded = manifest.load(path)
        assert loaded["tags"] == {"security": ["D000001"]}
        assert loaded["files"] == {"auth.py": ["D000001"]}

    def test_save_atomic_no_leftover_tmp(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        manifest.save(path, manifest._empty_manifest())
        leftover = path.with_suffix(path.suffix + ".tmp")
        assert not leftover.exists()

    def test_load_malformed_yaml_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        path.write_text("{this is: [not] - valid: yaml: [[[")
        m = manifest.load(path)
        assert m["schema_version"] == 1
        assert m["total_decisions"] == 0


class TestRegenerate:
    @pytest.fixture
    def decisions_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "decisions.jsonl"
        jsonl_store.append_many(
            path,
            [
                {
                    "id": "D000001",
                    "decision": "Use bcrypt",
                    "tags": ["Security", "auth"],  # capital S to test normalization
                    "file_path": "auth.py",
                    "do_not_revert": True,
                },
                {
                    "id": "D000002",
                    "decision": "named exports",
                    "tags": ["typescript", "style"],
                    "file_path": "core.ts",
                },
                {
                    "id": "D000003",
                    "decision": "old approach",
                    "tags": ["security"],
                    "file_path": "auth.py",
                    "is_superseded": True,
                },
            ],
        )
        return path

    def test_regenerate_counts(self, decisions_path: Path, tmp_path: Path) -> None:
        m = manifest.regenerate(decisions_path, tmp_path / "manifest.yaml")
        assert m["total_decisions"] == 3
        assert m["active_decisions"] == 2  # D000003 superseded

    def test_tag_normalization_lowercase(
        self, decisions_path: Path, tmp_path: Path
    ) -> None:
        m = manifest.regenerate(decisions_path, tmp_path / "manifest.yaml")
        # "Security" should become "security" (and merge with the existing
        # "security" tag from D000003... but D000003 is superseded so
        # excluded from active maps).
        assert "security" in m["tags"]
        assert "Security" not in m["tags"]

    def test_superseded_excluded_from_maps(
        self, decisions_path: Path, tmp_path: Path
    ) -> None:
        m = manifest.regenerate(decisions_path, tmp_path / "manifest.yaml")
        # D000003 is superseded; should not appear in tags/files maps.
        for tag_ids in m["tags"].values():
            assert "D000003" not in tag_ids
        for file_ids in m["files"].values():
            assert "D000003" not in file_ids

    def test_do_not_revert_ids_collected(
        self, decisions_path: Path, tmp_path: Path
    ) -> None:
        m = manifest.regenerate(decisions_path, tmp_path / "manifest.yaml")
        assert m["do_not_revert_ids"] == ["D000001"]

    def test_value_lists_sorted_deterministic(
        self, decisions_path: Path, tmp_path: Path
    ) -> None:
        # Regenerate twice; ordered fields should be identical (cache-friendly).
        # generated_at timestamps will differ each call so we compare the
        # ordered-content fields rather than raw bytes.
        path = tmp_path / "manifest.yaml"
        manifest.regenerate(decisions_path, path)
        m1 = manifest.load(path)
        manifest.regenerate(decisions_path, path)
        m2 = manifest.load(path)
        # Content (excluding timestamp) is identical.
        for key in (
            "tags",
            "files",
            "do_not_revert_ids",
            "total_decisions",
            "active_decisions",
        ):
            assert m1[key] == m2[key]


class TestIncrementalAdd:
    def test_add_to_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        manifest.incremental_add(
            path,
            {
                "id": "D000001",
                "tags": ["security"],
                "file_path": "auth.py",
                "do_not_revert": True,
            },
        )
        m = manifest.load(path)
        assert m["tags"] == {"security": ["D000001"]}
        assert m["files"] == {"auth.py": ["D000001"]}
        assert m["do_not_revert_ids"] == ["D000001"]
        assert m["total_decisions"] == 1
        assert m["active_decisions"] == 1

    def test_add_idempotent_on_duplicate_id(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        rec = {"id": "D000001", "tags": ["security"], "file_path": "auth.py"}
        manifest.incremental_add(path, rec)
        manifest.incremental_add(path, rec)  # duplicate
        m = manifest.load(path)
        assert m["total_decisions"] == 1
        assert m["tags"]["security"] == ["D000001"]  # not [D1, D1]

    def test_add_normalizes_tag_case(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        manifest.incremental_add(
            path,
            {
                "id": "D000001",
                "tags": ["Security", " AUTH "],
            },
        )
        m = manifest.load(path)
        assert "security" in m["tags"]
        assert "auth" in m["tags"]
        assert "Security" not in m["tags"]

    def test_add_merges_with_existing_tags(self, tmp_path: Path) -> None:
        path = tmp_path / "manifest.yaml"
        manifest.incremental_add(path, {"id": "D000001", "tags": ["security"]})
        manifest.incremental_add(path, {"id": "D000002", "tags": ["security"]})
        m = manifest.load(path)
        assert sorted(m["tags"]["security"]) == ["D000001", "D000002"]
