"""
Tests for indexer/outcome_tracker.py — Git-based outcome analysis.

Mocks ALL subprocess calls (git) and path helpers to keep tests
hermetic and fast.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_db(populated_db):
    """Unpack the populated_db fixture and return (project, data_dir, db)."""
    return populated_db


# ---------------------------------------------------------------------------
# analyze_session_outcomes
# ---------------------------------------------------------------------------

class TestAnalyzeSessionOutcomes:
    """Tests for analyze_session_outcomes()."""

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    @patch("indexer.outcome_tracker.subprocess.check_output")
    def test_specific_session_id(self, mock_git, mock_root, mock_data_dir, populated_db):
        """Providing a session_id should analyze only that session."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        # Session s2 has a decision for src/db.py with no outcome yet.
        # Simulate: file exists, no subsequent commits -> kept
        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "db.py").write_text("# db")

        mock_git.return_value = b""  # no commits after session

        from indexer.outcome_tracker import analyze_session_outcomes
        analyze_session_outcomes(session_id="s2")

        outcomes = db.get_outcomes_for_file("src/db.py")
        assert any(o["outcome_type"] == "kept" for o in outcomes)

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    @patch("indexer.outcome_tracker.subprocess.check_output")
    def test_none_session_processes_all_without_outcomes(
        self, mock_git, mock_root, mock_data_dir, populated_db
    ):
        """session_id=None should find sessions with decisions but no outcomes."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        # s2 has decision for src/db.py but no outcome yet
        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "db.py").write_text("# db")

        mock_git.return_value = b""  # kept

        from indexer.outcome_tracker import analyze_session_outcomes
        analyze_session_outcomes(session_id=None)

        outcomes = db.get_outcomes_for_file("src/db.py")
        assert len(outcomes) >= 1

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    @patch("indexer.outcome_tracker.subprocess.check_output")
    def test_session_with_no_decisions(self, mock_git, mock_root, mock_data_dir, populated_db):
        """A session with no decisions should produce nothing to analyze."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        # Log a session with no decisions
        db.log_session("s_empty", "Empty session", "99", [])

        from indexer.outcome_tracker import analyze_session_outcomes
        analyze_session_outcomes(session_id="s_empty")

        # No outcomes should be recorded for this session
        cur = db.conn.execute(
            "SELECT COUNT(*) as c FROM outcomes WHERE session_id = ?", ("s_empty",)
        )
        assert cur.fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# _determine_file_outcome (tested indirectly through analyze_session_outcomes)
# ---------------------------------------------------------------------------

class TestFileOutcomeClassification:
    """Tests for outcome classification logic: kept / modified / reverted."""

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    @patch("indexer.outcome_tracker.subprocess.check_output")
    def test_file_kept_no_subsequent_commits(
        self, mock_git, mock_root, mock_data_dir, populated_db
    ):
        """No commits after session date -> outcome 'kept'."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        # Create file on disk and add a session/decision for it
        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "kept_file.py").write_text("# kept")
        db.log_session("s_kept", "Kept test", "10", [
            {"file_path": "src/kept_file.py", "decision": "Add file", "context": "test"},
        ])

        # git log returns empty -> no commits touching this file after session
        mock_git.return_value = b""

        from indexer.outcome_tracker import analyze_session_outcomes
        analyze_session_outcomes(session_id="s_kept")

        outcomes = db.get_outcomes_for_file("src/kept_file.py")
        assert len(outcomes) == 1
        assert outcomes[0]["outcome_type"] == "kept"

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    @patch("indexer.outcome_tracker.subprocess.check_output")
    def test_file_modified_subsequent_commits(
        self, mock_git, mock_root, mock_data_dir, populated_db
    ):
        """Subsequent commits without revert keywords -> outcome 'modified'."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "mod_file.py").write_text("# modified")
        db.log_session("s_mod", "Modified test", "11", [
            {"file_path": "src/mod_file.py", "decision": "Add file", "context": "test"},
        ])

        def git_side_effect(cmd, **kwargs):
            cmd_str = " ".join(cmd)
            if "log" in cmd_str:
                return b"abc1234 fix: refactor handler\ndef5678 feat: add validation"
            if "diff" in cmd_str:
                return b" src/mod_file.py | 5 ++---"
            return b""

        mock_git.side_effect = git_side_effect

        from indexer.outcome_tracker import analyze_session_outcomes
        analyze_session_outcomes(session_id="s_mod")

        outcomes = db.get_outcomes_for_file("src/mod_file.py")
        assert len(outcomes) == 1
        assert outcomes[0]["outcome_type"] == "modified"

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    @patch("indexer.outcome_tracker.subprocess.check_output")
    def test_file_reverted_commit_message(
        self, mock_git, mock_root, mock_data_dir, populated_db
    ):
        """Commit message containing 'revert' -> outcome 'reverted'."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "rev_file.py").write_text("# reverted")
        db.log_session("s_rev", "Reverted test", "12", [
            {"file_path": "src/rev_file.py", "decision": "Add file", "context": "test"},
        ])

        mock_git.return_value = b"aaa1111 revert: undo bad change"

        from indexer.outcome_tracker import analyze_session_outcomes
        analyze_session_outcomes(session_id="s_rev")

        outcomes = db.get_outcomes_for_file("src/rev_file.py")
        assert len(outcomes) == 1
        assert outcomes[0]["outcome_type"] == "reverted"

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    @patch("indexer.outcome_tracker.subprocess.check_output")
    def test_file_reverted_rollback_keyword(
        self, mock_git, mock_root, mock_data_dir, populated_db
    ):
        """Commit message containing 'rollback' -> outcome 'reverted'."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "rb_file.py").write_text("# rollback")
        db.log_session("s_rb", "Rollback test", "13", [
            {"file_path": "src/rb_file.py", "decision": "Add file", "context": "test"},
        ])

        mock_git.return_value = b"bbb2222 rollback api changes"

        from indexer.outcome_tracker import analyze_session_outcomes
        analyze_session_outcomes(session_id="s_rb")

        outcomes = db.get_outcomes_for_file("src/rb_file.py")
        assert len(outcomes) == 1
        assert outcomes[0]["outcome_type"] == "reverted"

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    def test_file_no_longer_exists(self, mock_root, mock_data_dir, populated_db):
        """File removed from disk -> outcome 'reverted'."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        # Decision references a file that does NOT exist on disk
        db.log_session("s_gone", "Gone test", "14", [
            {"file_path": "src/gone_file.py", "decision": "Add file", "context": "test"},
        ])

        from indexer.outcome_tracker import analyze_session_outcomes
        analyze_session_outcomes(session_id="s_gone")

        outcomes = db.get_outcomes_for_file("src/gone_file.py")
        assert len(outcomes) == 1
        assert outcomes[0]["outcome_type"] == "reverted"


# ---------------------------------------------------------------------------
# Git error handling
# ---------------------------------------------------------------------------

class TestGitErrorHandling:
    """Tests for graceful handling of git failures."""

    def test_git_cmd_returns_none_on_called_process_error(self):
        """_git_cmd should return None when git command fails."""
        with patch("indexer.outcome_tracker._project_root", return_value=Path("/tmp/fake")):
            with patch(
                "indexer.outcome_tracker.subprocess.check_output",
                side_effect=subprocess.CalledProcessError(1, "git"),
            ):
                from indexer.outcome_tracker import _git_cmd
                result = _git_cmd("log", "--oneline")
                assert result is None

    def test_git_cmd_returns_none_on_file_not_found(self):
        """_git_cmd should return None when git is not installed."""
        with patch("indexer.outcome_tracker._project_root", return_value=Path("/tmp/fake")):
            with patch(
                "indexer.outcome_tracker.subprocess.check_output",
                side_effect=FileNotFoundError("git not found"),
            ):
                from indexer.outcome_tracker import _git_cmd
                result = _git_cmd("status")
                assert result is None

    @patch("indexer.outcome_tracker.get_data_dir")
    @patch("indexer.outcome_tracker._project_root")
    @patch("indexer.outcome_tracker.subprocess.check_output")
    def test_git_failure_during_analysis_is_graceful(
        self, mock_git, mock_root, mock_data_dir, populated_db
    ):
        """Git failures during analysis should not crash; files get 'kept' when git returns None."""
        project, data_dir, db = _get_db(populated_db)
        mock_data_dir.return_value = data_dir
        mock_root.return_value = project

        (project / "src").mkdir(parents=True, exist_ok=True)
        (project / "src" / "err_file.py").write_text("# error")
        db.log_session("s_err", "Error test", "15", [
            {"file_path": "src/err_file.py", "decision": "Add file", "context": "test"},
        ])

        mock_git.side_effect = subprocess.CalledProcessError(128, "git")

        from indexer.outcome_tracker import analyze_session_outcomes
        # Should not raise
        analyze_session_outcomes(session_id="s_err")

        # When git fails, _git_cmd returns None, which means "no subsequent commits" -> kept
        outcomes = db.get_outcomes_for_file("src/err_file.py")
        assert len(outcomes) == 1
        assert outcomes[0]["outcome_type"] == "kept"


# ---------------------------------------------------------------------------
# P0-D (rc.5 audit, 2026-05-13): file mention extraction for file-less decisions
# ---------------------------------------------------------------------------

class TestExtractFileMentions:
    """``_extract_file_mentions`` rescues outcome classification for decisions
    recorded via ``record_decision`` without an explicit ``file_path=`` arg."""

    def test_path_with_directory(self):
        from indexer.outcome_tracker import _extract_file_mentions
        text = "Refactored mcp_server/cli.py to add the new subcommand."
        assert _extract_file_mentions(text) == ["mcp_server/cli.py"]

    def test_path_with_line_suffix_strips_to_path(self):
        from indexer.outcome_tracker import _extract_file_mentions
        text = "Bug 19: indexer/index_codebase.py:873 — wrong dict key."
        assert _extract_file_mentions(text) == ["indexer/index_codebase.py"]

    def test_multiple_mentions_returned_in_order(self):
        from indexer.outcome_tracker import _extract_file_mentions
        text = "Touched src/foo.py and tests/test_foo.py — also docs/foo.md"
        result = _extract_file_mentions(text)
        assert result == ["src/foo.py", "tests/test_foo.py", "docs/foo.md"]

    def test_skips_urls(self):
        from indexer.outcome_tracker import _extract_file_mentions
        text = "See https://example.com/foo.json for the schema."
        # URL substring "/foo.json" should NOT be extracted as a project file.
        assert _extract_file_mentions(text) == []

    def test_skips_absolute_paths(self):
        from indexer.outcome_tracker import _extract_file_mentions
        text = "The crash log lives at /var/log/system.log on macOS."
        # Absolute paths can't be validated against project tree.
        assert _extract_file_mentions(text) == []

    def test_no_mentions_returns_empty(self):
        from indexer.outcome_tracker import _extract_file_mentions
        assert _extract_file_mentions("Discussed the design tradeoffs.") == []

    def test_empty_text_returns_empty(self):
        from indexer.outcome_tracker import _extract_file_mentions
        assert _extract_file_mentions("") == []
        assert _extract_file_mentions(None) == []  # type: ignore[arg-type]

    def test_dedupes_repeated_mentions(self):
        from indexer.outcome_tracker import _extract_file_mentions
        text = "src/foo.py was changed. Then src/foo.py was reverted."
        assert _extract_file_mentions(text) == ["src/foo.py"]


# ---------------------------------------------------------------------------
# get_file_outcome_summary
# ---------------------------------------------------------------------------

