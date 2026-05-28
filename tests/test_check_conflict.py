"""
test_check_conflict.py — pins the v3.0.0 round-3 conflict-detection
contract: symmetric Jaccard for duplicates + asymmetric overlap
coefficient for conflicts against do_not_revert decisions.

The asymmetric path was added after the AgentStore system test
(``scripts/system_test_agentstore.py::A9``) surfaced that pure Jaccard
missed a direct contradiction (33% symmetric overlap, well below the
0.60 duplicate threshold) when the new decision was terse and the
protected decision was longer. The new path catches that shape
without false-positive-ing re-affirmations of protected decisions
(those have high SYMMETRIC similarity and fall under "duplicate").
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_server.tools.check_conflict import (
    _CONFLICT_MIN_SHARED_TOKENS,
    _CONFLICT_OVERLAP_THRESHOLD,
    _DUP_THRESHOLD,
    _jaccard,
    _overlap_coefficient,
    _tokenize,
    check_conflict,
)


@pytest.fixture
def isolated_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Pin a fresh .codevira/ + ~/.codevira/ under tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    monkeypatch.setattr(paths_mod, "get_global_home", lambda: fake_home / ".codevira")

    from mcp_server.storage import paths as store_paths

    store_paths.ensure_dirs()
    return project


# =====================================================================
# Pure helpers — Jaccard + overlap coefficient + tokenize
# =====================================================================


class TestHelpers:
    def test_jaccard_symmetric(self) -> None:
        a = {"x", "y", "z"}
        b = {"y", "z", "w"}
        assert _jaccard(a, b) == _jaccard(b, a)
        assert _jaccard(a, b) == 0.5  # 2 shared / 4 union

    def test_jaccard_empty_inputs(self) -> None:
        assert _jaccard(set(), set()) == 1.0  # both empty is "identical"
        assert _jaccard({"x"}, set()) == 0.0
        assert _jaccard(set(), {"x"}) == 0.0

    def test_overlap_asymmetric_invariant(self) -> None:
        """Overlap coefficient IS symmetric (|A∩B| / min(|A|,|B|) doesn't
        depend on which is A vs B), but it's "asymmetric" in the sense
        that it's normalized by the smaller set, not the union."""
        a = {"x", "y", "z"}
        b = {"y", "z", "w", "v", "u"}
        # |A∩B|=2, |A|=3, |B|=5, min=3 → 2/3 = 0.667
        assert _overlap_coefficient(a, b) == pytest.approx(2 / 3, abs=1e-3)
        assert _overlap_coefficient(b, a) == pytest.approx(2 / 3, abs=1e-3)

    def test_overlap_vs_jaccard_diverge_on_size_imbalance(self) -> None:
        """The whole point: overlap >> jaccard when sets are imbalanced."""
        small = {"x", "y"}
        big = {"x", "y", "a", "b", "c", "d", "e", "f"}
        # Jaccard: 2/8 = 0.25
        # Overlap: 2/min(2,8) = 1.0
        assert _jaccard(small, big) == pytest.approx(0.25, abs=1e-3)
        assert _overlap_coefficient(small, big) == 1.0

    def test_tokenize_stripped_stopwords_and_short(self) -> None:
        # "is", "an", "the", "of", "to" are stopwords; len<3 dropped.
        toks = _tokenize("This is an example of a short and tiny test")
        # remaining: "example", "short", "tiny", "test"
        assert toks == {"example", "short", "tiny", "test"}


# =====================================================================
# check_conflict — empty / novel paths
# =====================================================================


class TestEmptyAndNovel:
    def test_empty_decision_text_errors(self, isolated_project: Path) -> None:
        r = check_conflict("")
        assert r["status"] == "error"
        assert r["conflicts"] == []
        assert r["duplicates"] == []

    def test_non_string_decision_text_errors(self, isolated_project: Path) -> None:
        r = check_conflict(None)  # type: ignore[arg-type]
        assert r["status"] == "error"

    def test_novel_when_no_decisions_yet(self, isolated_project: Path) -> None:
        r = check_conflict("a truly novel decision about authentication")
        assert r["status"] == "novel"
        assert r["conflicts"] == []
        assert r["duplicates"] == []

    def test_novel_when_no_keyword_overlap(self, isolated_project: Path) -> None:
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision="Use bcrypt for password hashing", file_path="auth.py"
        )
        r = check_conflict("Configure nginx reverse proxy on port 8080")
        assert r["status"] == "novel"


# =====================================================================
# Symmetric path — duplicates (Jaccard ≥ DUP_THRESHOLD)
# =====================================================================


class TestSymmetricDuplicates:
    def test_high_jaccard_against_unprotected_is_duplicate(
        self, isolated_project: Path
    ) -> None:
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision="Use bcrypt for password hashing",
            file_path="auth.py",
            do_not_revert=False,
        )
        # Note: the tokenizer doesn't stem, so we need actual token-set
        # overlap (not just same concept) to hit DUP_THRESHOLD. The
        # decision text below shares all 3 content tokens
        # {bcrypt, password, hashing} verbatim, so Jaccard ≥ 0.60.
        r = check_conflict("Always use bcrypt for password hashing")
        assert (
            r["status"] == "duplicate"
        ), f"expected duplicate, got {r['status']}; duplicates={r['duplicates']}"
        assert len(r["duplicates"]) >= 1
        assert r["duplicates"][0]["match_shape"] == "duplicate"

    def test_high_jaccard_against_protected_is_conflict(
        self, isolated_project: Path
    ) -> None:
        """Re-record of a protected decision DOES flag as conflict
        (status), but with match_shape='duplicate' so the agent can
        tell it's a re-affirmation rather than a contradiction."""
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision="Use bcrypt for password hashing",
            file_path="auth.py",
            do_not_revert=True,
        )
        r = check_conflict("Use bcrypt for password hashing")
        assert r["status"] == "conflict"
        assert len(r["conflicts"]) == 1
        assert r["conflicts"][0]["match_shape"] == "duplicate"
        assert r["conflicts"][0]["do_not_revert"] is True


# =====================================================================
# Asymmetric path — the v3.0.0 round-3 addition
# =====================================================================


class TestAsymmetricConflict:
    def test_agentstore_scenario_now_fires(self, isolated_project: Path) -> None:
        """The exact pair that surfaced A9 in the AgentStore system test:
        a terse new decision shares 3 of its 4 content tokens with a
        longer protected decision. Pre-fix: status='novel' (miss).
        Post-fix: status='conflict' via the asymmetric overlap path.
        """
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision=(
                "AgentStore uses pnpm workspaces — DO NOT switch package manager"
            ),
            file_path="pnpm-workspace.yaml",
            do_not_revert=True,
        )
        r = check_conflict("AgentStore should switch from pnpm to npm")
        assert r["status"] == "conflict", (
            f"asymmetric conflict missed — got status={r['status']}, "
            f"conflicts={r['conflicts']}"
        )
        assert len(r["conflicts"]) == 1
        c = r["conflicts"][0]
        assert c["match_shape"] == "asymmetric-conflict"
        assert c["do_not_revert"] is True
        # Sanity: the overlap was the firing signal, jaccard was below
        # the duplicate threshold.
        assert c["overlap_coefficient"] >= _CONFLICT_OVERLAP_THRESHOLD
        assert c["jaccard"] < _DUP_THRESHOLD
        assert c["shared_tokens"] >= _CONFLICT_MIN_SHARED_TOKENS

    def test_asymmetric_path_does_not_fire_against_unprotected(
        self, isolated_project: Path
    ) -> None:
        """The asymmetric path is conflict-only — non-protected
        decisions don't trigger it. Without protection, the same pair
        falls through to duplicate-detection (symmetric Jaccard) which
        misses by design."""
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision=(
                "AgentStore uses pnpm workspaces — DO NOT switch package manager"
            ),
            file_path="pnpm-workspace.yaml",
            do_not_revert=False,  # ← not protected
        )
        r = check_conflict("AgentStore should switch from pnpm to npm")
        assert r["status"] == "novel"

    def test_asymmetric_floor_filters_one_token_noise(
        self, isolated_project: Path
    ) -> None:
        """A 1-token overlap (e.g., both mention 'pnpm') should NOT
        flag as conflict even though overlap_coefficient = 1.0 — the
        shared-token floor (CONFLICT_MIN_SHARED_TOKENS=3) prevents it.
        """
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision=("We standardize on pnpm across all packages and CI pipelines"),
            file_path="pnpm-workspace.yaml",
            do_not_revert=True,
        )
        # New decision only shares 'pnpm' — single token; overlap = 1.0
        # for a 1-token query, but shared_tokens=1 < 3 → no conflict.
        r = check_conflict("Add pnpm")
        # Could be 'novel' or 'duplicate' depending on rounding, but
        # MUST NOT be 'conflict'.
        assert (
            r["status"] != "conflict"
        ), f"1-token noise flagged as conflict: {r['conflicts']}"

    def test_reaffirmation_of_protected_is_duplicate_not_asymmetric(
        self, isolated_project: Path
    ) -> None:
        """Re-recording the same protected decision verbatim should hit
        the duplicate path (Jaccard=1.0), NOT the asymmetric path —
        because the asymmetric path explicitly requires Jaccard <
        DUP_THRESHOLD (the re-affirmation filter)."""
        from mcp_server.storage import decisions_store

        decisions_store.record(
            decision="API responses must include X-Request-ID header",
            file_path="middleware.py",
            do_not_revert=True,
        )
        r = check_conflict("API responses must include X-Request-ID header")
        assert r["status"] == "conflict"  # still flagged (protected)
        # but as a duplicate-shape (re-affirmation), not asymmetric
        assert r["conflicts"][0]["match_shape"] == "duplicate"


# =====================================================================
# Response shape — agent-readable contract
# =====================================================================


class TestResponseShape:
    def test_thresholds_dict_present(self, isolated_project: Path) -> None:
        r = check_conflict("anything novel")
        assert "thresholds" in r
        assert r["thresholds"]["duplicate_jaccard"] == _DUP_THRESHOLD
        assert r["thresholds"]["conflict_overlap"] == _CONFLICT_OVERLAP_THRESHOLD
        assert (
            r["thresholds"]["conflict_min_shared_tokens"] == _CONFLICT_MIN_SHARED_TOKENS
        )

    def test_back_compat_threshold_used_still_present(
        self, isolated_project: Path
    ) -> None:
        r = check_conflict("anything novel")
        assert r["threshold_used"] == _DUP_THRESHOLD  # v2.x callers


class TestM1OriginSurface:
    """v3.1.0 M1: each conflict/duplicate entry carries the candidate's
    ``origin`` so agents can answer "this contradicts a decision Cursor
    wrote 3 days ago"."""

    def test_duplicate_entry_includes_origin(
        self,
        isolated_project: Path,
        monkeypatch: "pytest.MonkeyPatch",  # type: ignore[name-defined]
    ) -> None:
        from mcp_server.storage import decisions_store

        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        decisions_store.record(
            decision="Use bcrypt for password hashing",
            file_path="auth.py",
        )
        r = check_conflict("Use bcrypt for password hashing")
        assert r["status"] in ("duplicate", "conflict")
        entries = r.get("duplicates") + r.get("conflicts")
        assert entries, r
        origin_field = entries[0].get("origin")
        assert origin_field is not None, entries[0]
        assert origin_field["ide"] == "cursor"

    def test_origin_none_for_legacy_record(self, isolated_project: Path) -> None:
        """Legacy v3.0.x decisions written without origin still surface
        — the field is None (NOT a crash, NOT a placeholder)."""
        from mcp_server.storage import jsonl_store, paths

        # Hand-craft a legacy record without origin.
        legacy = {
            "id": "D000001",
            "ts": "2026-05-01T00:00:00Z",
            "session_id": "ad-hoc",
            "file_path": "x.py",
            "decision": "Use bcrypt for password hashing",
            "context": None,
            "do_not_revert": False,
            "tags": [],
            "supersedes": None,
            "superseded_by": None,
            "outcome": None,
        }
        jsonl_store.append(paths.decisions_path(), legacy)

        r = check_conflict("Use bcrypt for password hashing")
        entries = r.get("duplicates") + r.get("conflicts")
        assert entries, r
        # Origin field present in dict, value is None (no crash).
        assert entries[0]["origin"] is None
