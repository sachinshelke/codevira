"""Unit tests for mcp_server.storage.digest.

Covers:
- make_summary: word-boundary trim at ~80 chars
- weight_for_outcome: kept/modified/reverted/archived/None
- digest_record: shape contract
- regenerate: full rebuild produces correct digest, atomic via tmp+rename
- exclude_superseded behavior
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.storage import digest, jsonl_store


class TestMakeSummary:
    def test_short_text_unchanged(self) -> None:
        assert digest.make_summary("Use bcrypt") == "Use bcrypt"

    def test_empty_returns_empty(self) -> None:
        assert digest.make_summary("") == ""

    def test_long_text_truncates_at_word_boundary(self) -> None:
        text = (
            "Use bcrypt over argon2 for password hashing because team familiarity "
            "and audit history outweigh argon2's memory-hard advantage in our context"
        )
        summary = digest.make_summary(text)
        assert len(summary) <= 82  # 80 + ellipsis
        assert summary.endswith("…")
        # Should not split mid-word (last non-ellipsis char shouldn't be a partial)
        before = summary.rstrip("…").rstrip()
        assert before.split()[-1] in text  # last word is intact

    def test_strips_newlines(self) -> None:
        summary = digest.make_summary("line one\nline two\rline three")
        assert "\n" not in summary
        assert "\r" not in summary

    def test_exactly_80_chars(self) -> None:
        text = "x" * 80
        # Should not truncate at exactly the boundary.
        assert digest.make_summary(text) == text


class TestWeightForOutcome:
    def test_kept(self) -> None:
        assert digest.weight_for_outcome("kept") == 1.0

    def test_modified(self) -> None:
        assert digest.weight_for_outcome("modified") == 0.6

    def test_reverted(self) -> None:
        assert digest.weight_for_outcome("reverted") == 0.2

    def test_archived(self) -> None:
        assert digest.weight_for_outcome("archived") == 0.0

    def test_none_neutral(self) -> None:
        assert digest.weight_for_outcome(None) == 0.5

    def test_unknown_falls_back_to_neutral(self) -> None:
        assert digest.weight_for_outcome("bogus") == 0.5


class TestDigestRecord:
    def test_shape_contract(self) -> None:
        rec = digest.digest_record(
            {
                "id": "D000001",
                "decision": "Use bcrypt for password hashing",
                "tags": ["security", "auth"],
                "file_path": "auth.py",
                "do_not_revert": True,
                "outcome": "kept",
                # Fields that should be dropped:
                "context": "lengthy rationale",
                "ts": "2026-05-19T12:00:00Z",
            }
        )
        assert set(rec.keys()) == {
            "id",
            "summary",
            "tags",
            "file",
            "do_not_revert",
            "weight",
        }
        assert rec["id"] == "D000001"
        assert rec["summary"] == "Use bcrypt for password hashing"
        assert rec["tags"] == ["security", "auth"]
        assert rec["file"] == "auth.py"
        assert rec["do_not_revert"] is True
        assert rec["weight"] == 1.0

    def test_missing_optional_fields_default_sanely(self) -> None:
        rec = digest.digest_record({"id": "D000001", "decision": "x"})
        assert rec["tags"] == []
        assert rec["file"] is None
        assert rec["do_not_revert"] is False
        assert rec["weight"] == 0.5  # no outcome


class TestRegenerate:
    @pytest.fixture
    def populated_decisions(self, tmp_path: Path) -> Path:
        path = tmp_path / "decisions.jsonl"
        jsonl_store.append_many(
            path,
            [
                {
                    "id": "D000001",
                    "decision": "Use bcrypt",
                    "tags": ["security"],
                    "file_path": "auth.py",
                    "do_not_revert": True,
                    "outcome": "kept",
                },
                {
                    "id": "D000002",
                    "decision": "Use argon2 instead",
                    "tags": ["security"],
                    "file_path": "auth.py",
                    "do_not_revert": True,
                    "is_superseded": True,
                },
                {
                    "id": "D000003",
                    "decision": "Prefer named exports",
                    "tags": ["typescript"],
                    "file_path": "core.ts",
                    "outcome": "modified",
                },
            ],
        )
        return path

    def test_regenerate_count(self, populated_decisions: Path, tmp_path: Path) -> None:
        digest_path = tmp_path / "digest.jsonl"
        count = digest.regenerate(populated_decisions, digest_path)
        # 3 decisions; 1 superseded → 2 in digest by default.
        assert count == 2

    def test_regenerate_includes_superseded_when_asked(
        self, populated_decisions: Path, tmp_path: Path
    ) -> None:
        digest_path = tmp_path / "digest.jsonl"
        count = digest.regenerate(
            populated_decisions, digest_path, exclude_superseded=False
        )
        assert count == 3

    def test_regenerate_shape(self, populated_decisions: Path, tmp_path: Path) -> None:
        digest_path = tmp_path / "digest.jsonl"
        digest.regenerate(populated_decisions, digest_path)
        recs = jsonl_store.read_all(digest_path)
        for rec in recs:
            assert set(rec.keys()) == {
                "id",
                "summary",
                "tags",
                "file",
                "do_not_revert",
                "weight",
            }

    def test_regenerate_atomic_via_tmp(
        self, populated_decisions: Path, tmp_path: Path
    ) -> None:
        """No .tmp file left over after regenerate."""
        digest_path = tmp_path / "digest.jsonl"
        digest.regenerate(populated_decisions, digest_path)
        tmp_leftover = digest_path.with_suffix(digest_path.suffix + ".tmp")
        assert not tmp_leftover.exists()

    def test_regenerate_idempotent(
        self, populated_decisions: Path, tmp_path: Path
    ) -> None:
        digest_path = tmp_path / "digest.jsonl"
        digest.regenerate(populated_decisions, digest_path)
        first = digest_path.read_bytes()
        digest.regenerate(populated_decisions, digest_path)
        second = digest_path.read_bytes()
        assert first == second

    def test_regenerate_empty_decisions(self, tmp_path: Path) -> None:
        decisions_path = tmp_path / "decisions.jsonl"  # doesn't exist
        digest_path = tmp_path / "digest.jsonl"
        count = digest.regenerate(decisions_path, digest_path)
        assert count == 0
        # Empty digest file should be created.
        assert digest_path.exists()
        assert jsonl_store.read_all(digest_path) == []
