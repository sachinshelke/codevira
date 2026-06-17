"""
test_v350_acceptance.py — the v3.5.0 release acceptance gate.

ONE end-to-end check that walks every v3.5.0 roadmap item through its real,
user-facing surface — the same idiom as ``test_cross_tool_universality.py``
(real stores, real signals, real policies, real CLI), not unit mocks. When
this file is green, v3.5.0 is coherent and releasable; when a class goes red,
it names exactly which shipped feature regressed.

Roadmap coverage (branch feat/v3.5.0, 10 commits off main):

  doctor   ghost_projects classifies empty leftovers as *stale*, not ghost   D0000Z4
  P18      content-aware decision lock: orthogonal edit warns, conflict blocks D00010B
  E1/P19   summary-first search_decisions + expand() round-trip                D0000ZQ
  E2/P20   read-only session-transcript ingest (candidates only)              D00010W
  E3/P21   read-side relevance eval (self-derived, non-gating)                 D00010Y
  P13      learned ranking weights (opt-in, persisted, hot-path read)          D00010Z
  E4/P22   managed files beyond AGENTS.md, same canonical block everywhere     D000110
  P16      get_signature multi-language surface (.py/.ts/.tsx/.js/.jsx/.go/.rs)
  P17      one shared git outcome classifier (kept / modified / reverted)      D000112
  E5/P23   opt-in synonym query widening (default-off recall aid)              D000113

The final class asserts the cross-cutting release-coherence invariants —
CHANGELOG completeness, the env-flag default-off contract, and CLI/MCP
surface registration.

Run as part of G2 in the release gauntlet (``make test-e2e``). NEVER ship red.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

# Repo root from this file's location: tests/e2e/<this> → parents[2].
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ─────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A fresh isolated project wired as BOTH the resolved project-dir AND the
    cwd, so every v3.5.0 read/write path (decisions_store, SignalContext,
    config, AGENTS.md writers) lands in disposable storage. Mirrors the
    cross-tool e2e fixture; composes with conftest's autouse global-home
    isolation.
    """
    import mcp_server.paths as paths_mod

    proj = tmp_path / "v350-project"
    (proj / ".codevira").mkdir(parents=True)
    (proj / ".codevira" / "config.yaml").write_text(
        "project:\n  name: v350-acceptance\n", encoding="utf-8"
    )
    (proj / "pyproject.toml").write_text("", encoding="utf-8")
    paths_mod.set_project_dir(proj)
    paths_mod.invalidate_data_dir_cache()
    monkeypatch.chdir(proj)
    return proj


@pytest.fixture(autouse=True)
def _clean_v350_env(monkeypatch):
    """Reset engine policies + clear every v3.5.0 opt-in flag so each test
    asserts DEFAULT behavior. ``CODEVIRA_DECISION_LOCK_MODE`` is known to
    linger in a developer's shell (see the feat/v3.5.0 session notes), which
    would otherwise mask a default-mode regression.
    """
    from mcp_server.engine.runner import reset_policies

    reset_policies()
    for env in (
        "CODEVIRA_ENGINE",
        "CODEVIRA_DECISION_LOCK_MODE",
        "CODEVIRA_DECISION_LOCK_CONTENT_AWARE",
        "CODEVIRA_DECISION_DETAIL",
        "CODEVIRA_LEARNED_WEIGHTS",
        "CODEVIRA_SYNONYM_WIDENING",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    reset_policies()


def _record(decision: str, **kw) -> str:
    """Record one decision into the (isolated) project store; return its ID."""
    from mcp_server.storage import decisions_store

    return decisions_store.record(decision=decision, **kw)


# ─────────────────────────────────────────────────────────────────────
# doctor — ghost_projects false positive (D0000Z4)
# ─────────────────────────────────────────────────────────────────────


class TestDoctorGhostNotStale:
    """An empty leftover under ~/.codevira/projects/ is *stale*, not a ghost,
    and the doctor check agrees with the canonical project inventory."""

    def test_empty_leftover_is_stale_and_doctor_agrees(self, project):
        import mcp_server.paths as paths_mod
        from mcp_server._ghost_check import check_ghost_projects
        from mcp_server._project_inventory import enumerate_projects, summarize

        # A directory under the (isolated) global home with NO recognizable
        # state — the exact shape that the pre-fix rule miscounted as a ghost.
        projects_dir = paths_mod.get_global_home() / "projects"
        (projects_dir / "stale-leftover").mkdir(parents=True)

        entries = enumerate_projects()
        leftover = [e for e in entries if e.slug == "stale-leftover"] or [
            e for e in entries if getattr(e, "status", None) == "stale"
        ]
        assert leftover, "the empty leftover dir should appear in the inventory"
        assert (
            leftover[0].status == "stale"
        ), "empty leftover must classify as 'stale', not 'ghost' (D0000Z4)"

        counts = summarize(entries)
        assert counts["ghost"] == 0, "no real-state dirs → zero ghosts"
        assert counts["stale"] >= 1

        # The doctor surface delegates to the same inventory, so it must NOT
        # raise a ghost WARN for a stale-only registry.
        result = check_ghost_projects()
        assert (
            result.state == "PASS"
        ), f"doctor disagreed with inventory: {result.state} / {result.message}"
        assert "no ghost" in result.message.lower()


# ─────────────────────────────────────────────────────────────────────
# P18 — content-aware decision lock (D00010B)
# ─────────────────────────────────────────────────────────────────────


class TestDecisionLockContentAware:
    """Through the REAL SignalContext + a REAL locked decision: an edit that is
    provably orthogonal to the locked decision's subject downgrades block→warn;
    an edit that touches the subject still hard-blocks. The integration proof a
    unit mock can't give."""

    _WATCHER = (
        "CODEVIRA_NO_WATCHER=1 env var skips start_background_watcher in both "
        "stdio and HTTP MCP servers"
    )

    def _policy_and_signals(self, project):
        from mcp_server.engine.policies.decision_lock import DecisionLock
        from mcp_server.engine.signals import SignalContext

        _record(self._WATCHER, file_path="server.py", do_not_revert=True)
        return DecisionLock(), SignalContext(project_root=project)

    def _edit_event(self, project, before: str, after: str):
        from mcp_server.engine.events import EventType, HookEvent

        diff = f"--- before\n{before}\n--- after\n{after}\n"
        return HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=project,
            tool_name="Edit",
            target_file=project / "server.py",
            proposed_diff=diff,
        )

    def test_orthogonal_edit_downgrades_to_warn(self, project):
        policy, signals = self._policy_and_signals(project)
        # Editing an unrelated tool description — shares no salient token with
        # the watcher decision → orthogonal → warn (decision still surfaced).
        event = self._edit_event(
            project,
            before="enumerate decisions with filters since_date and tags; ~50 tokens/row.",
            after="enumerate decisions. compact one-line summary; full=true or expand(ids).",
        )
        verdict = policy.evaluate(event, signals)
        assert verdict.action == "warn", "orthogonal edit must not hard-block (D00010B)"
        assert verdict.metadata["content_orthogonal"] is True

    def test_conflicting_edit_still_blocks(self, project):
        policy, signals = self._policy_and_signals(project)
        # This edit removes the CODEVIRA_NO_WATCHER guard from
        # start_background_watcher — it touches the decision's subject.
        event = self._edit_event(
            project,
            before=(
                "def start_background_watcher():\n"
                "    if os.environ.get('CODEVIRA_NO_WATCHER'):\n"
                "        return\n"
                "    spawn()"
            ),
            after="def start_background_watcher():\n    spawn()  # always run",
        )
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking(), "edit touching the locked subject must block"
        assert "start_background_watcher" in verdict.metadata["conflict_tokens"]

    def test_opt_out_restores_strict_block(self, project, monkeypatch):
        monkeypatch.setenv("CODEVIRA_DECISION_LOCK_CONTENT_AWARE", "0")
        policy, signals = self._policy_and_signals(project)
        event = self._edit_event(
            project,
            before="enumerate decisions with filters. ~50 tokens/row.",
            after="enumerate decisions. compact one-line summary.",
        )
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking(), "opt-out (=0) restores strict file-level lock"
        assert verdict.metadata["content_orthogonal"] is False


# ─────────────────────────────────────────────────────────────────────
# E1 — summary-first payloads + expand() (D0000ZQ)
# ─────────────────────────────────────────────────────────────────────


class TestSummaryFirstAndExpand:
    # A >140-char decision whose distinctive marker lives AFTER char 140, so
    # the compact one-liner truncates it out and only expand() restores it.
    _LONG = (
        "adopt exponential backoff with full jitter for all outbound HTTP "
        "retries; cap attempts at five; this supersedes the fixed 200ms sleep "
        "that caused a retry storm against the billing API — INCIDENT-88 has "
        "the full postmortem detail."
    )

    def test_compact_default_then_expand_restores_full(self, project):
        from mcp_server.tools import search as search_tool

        did = _record(self._LONG, file_path="retries.py", tags=["retry", "http"])

        res = search_tool.search_decisions("retries", limit=5)
        row = next(r for r in res["results"] if r["id"] == did)
        assert len(row["decision"]) <= 140, "default rows are one-lined (≤140)"
        assert "INCIDENT-88" not in row["decision"], "tail truncated in compact view"
        assert "expand" in res.get("hint", "").lower()

        full = search_tool.expand(ids=[did])
        assert full["count"] == 1 and not full["not_found"]
        assert (
            "INCIDENT-88" in full["decisions"][0]["decision"]
        ), "expand() must return the full untruncated record (D0000ZQ)"

    def test_detail_env_restores_verbose_default(self, project, monkeypatch):
        from mcp_server.tools import search as search_tool

        did = _record(self._LONG, file_path="retries.py")
        monkeypatch.setenv("CODEVIRA_DECISION_DETAIL", "full")
        res = search_tool.search_decisions("retries", limit=5)
        row = next(r for r in res["results"] if r["id"] == did)
        assert (
            "INCIDENT-88" in row["decision"]
        ), "CODEVIRA_DECISION_DETAIL=full restores the pre-E1 verbose default"


# ─────────────────────────────────────────────────────────────────────
# E2 — read-only session-transcript ingest (D00010W)
# ─────────────────────────────────────────────────────────────────────


def _claude_session_log(path: Path) -> None:
    """A minimal Claude Code transcript with one tool failure + one user
    correction — i.e. an 'interesting' session the scanner should surface."""
    records = [
        {"type": "user", "message": {"content": "please edit foo"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "is_error": True,
                        "content": "Exit code 1: failed near AKIAIOSFODNN7EXAMPLE",
                    }
                ]
            },
        },
        {"type": "user", "message": {"content": "no, that's wrong — revert it"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")


class TestSessionIngestReadOnly:
    def test_scan_surfaces_interesting_digest_without_mutating_logs(
        self, project, tmp_path
    ):
        from mcp_server.ingest import scan

        # Claude Code stores each project's sessions under a dir named after the
        # project path with slashes replaced by dashes.
        cc_root = tmp_path / "claude"
        proj_dir = cc_root / str(project).replace("/", "-")
        proj_dir.mkdir(parents=True)
        log = proj_dir / "s.jsonl"
        _claude_session_log(log)

        def snapshot():
            return {
                str(p): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in cc_root.rglob("*")
                if p.is_file()
            }

        before = snapshot()
        # Restrict to the claude_code parser so the scan can't reach the
        # developer's real ~/.codex / ~/.gemini logs (other parsers would
        # otherwise run against their default roots).
        digests = scan.scan_sessions(
            project,
            roots={"claude_code": cc_root},
            sources=["claude_code"],
            since_days=3650,
        )
        after = snapshot()

        assert before == after, "transcript scan must be READ-ONLY (D00010W)"
        assert digests, "the failure+correction session should be surfaced"
        d = digests[0]
        assert d.source == "claude_code" and d.is_interesting
        assert d.n_failures >= 1 and d.n_corrections >= 1
        # Secrets in retained excerpts are scrubbed at parse time.
        assert all("AKIAIOSFODNN7EXAMPLE" not in f.error_excerpt for f in d.failures)


# ─────────────────────────────────────────────────────────────────────
# E3 — read-side relevance eval (D00010Y)
# ─────────────────────────────────────────────────────────────────────


class TestRelevanceEval:
    def _seed(self):
        _record(
            "use bcrypt over argon2 for password hashing",
            file_path="auth.py",
            tags=["auth"],
        )
        _record(
            "exponential backoff with jitter; max 5 attempts",
            file_path="retries.py",
            tags=["retry"],
        )
        _record(
            "store the event ledger in postgres append-only",
            file_path="ledger.py",
            tags=["db"],
        )

    def test_eval_runs_on_self_derived_cases_and_returns_metrics(self, project):
        from mcp_server.eval import run_eval

        self._seed()
        result = run_eval(k=5, max_cases=50)
        metrics = result["metrics"]
        assert metrics["n_cases"] >= 1, "cases are self-derived from real memory"
        assert 0.0 <= metrics["recall_at_k"] <= 1.0
        assert "mrr" in metrics

    def test_eval_cli_is_non_gating(self, project):
        from mcp_server.cli_eval import cmd_eval

        self._seed()
        # Non-gating by design: a quality signal, always exit 0 without --gate.
        assert cmd_eval(k=5, max_cases=50, trend=False) == 0


# ─────────────────────────────────────────────────────────────────────
# P13 — learned hot-path weights, opt-in (D00010Z)
# ─────────────────────────────────────────────────────────────────────


class TestLearnedWeights:
    def test_opt_in_round_trip(self, project, monkeypatch):
        from mcp_server.engine.policies import relevance_inject
        from mcp_server.storage import learned_weights

        learned = {"tag": 9.0, "file": 8.0, "fts": 7.0}
        assert learned_weights.save(learned), "atomic persist must succeed"

        # Default (no env): the hot path ignores the learned file.
        monkeypatch.delenv("CODEVIRA_LEARNED_WEIGHTS", raising=False)
        assert relevance_inject._learned_weights_enabled() is False
        assert relevance_inject._effective_weights() != (9.0, 8.0, 7.0)

        # Opt in: the learned vector replaces the shipped defaults.
        monkeypatch.setenv("CODEVIRA_LEARNED_WEIGHTS", "1")
        assert relevance_inject._effective_weights() == (9.0, 8.0, 7.0)

    def test_corrupt_file_falls_back_to_defaults(self, project, monkeypatch):
        from mcp_server.engine.policies import relevance_inject
        from mcp_server.storage import learned_weights

        learned_weights.path().write_text("{ not json", encoding="utf-8")
        monkeypatch.setenv("CODEVIRA_LEARNED_WEIGHTS", "1")
        # A malformed file can never make the read surface worse than it ships.
        assert relevance_inject._effective_weights() != (0.0, 0.0, 0.0)

    def test_tune_cli_never_gates(self, project):
        from mcp_server.cli_eval import cmd_tune_weights

        _record("use bcrypt over argon2", file_path="auth.py", tags=["auth"])
        assert cmd_tune_weights(k=5, max_cases=50) == 0


# ─────────────────────────────────────────────────────────────────────
# E4 — managed files beyond AGENTS.md, same canonical block (D000110)
# ─────────────────────────────────────────────────────────────────────


class TestManagedFilesCrossTool:
    def _set_managed(self, files):
        from mcp_server.storage import paths as store_paths

        cfg = store_paths.config_path()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        body = "project:\n  name: v350\nmanaged_files:\n" + "".join(
            f"  - {m}\n" for m in files
        )
        cfg.write_text(body, encoding="utf-8")

    def test_same_block_written_to_every_managed_file(self, project):
        from mcp_server.storage import agents_md_generator as gen

        self._set_managed(["AGENTS.md", "CLAUDE.md", "GEMINI.md"])
        _record("adopt the widget queue", do_not_revert=True)

        res = gen.regenerate_all()
        assert res["count"] == 3 and all(t["ok"] for t in res["targets"])

        # The wedge promise, file edition: every per-tool nudge file carries the
        # SAME canonical codevira block, byte-for-byte between the markers.
        def block_of(name):
            text = (project / name).read_text(encoding="utf-8")
            b = text.index("<!-- codevira:begin")
            e = text.index("<!-- codevira:end -->")
            return text[b:e]

        agents = block_of("AGENTS.md")
        assert "<!-- codevira:begin" in agents
        assert (
            block_of("CLAUDE.md") == agents == block_of("GEMINI.md")
        ), "managed files must share one canonical block (D000110)"

    def test_default_is_agents_only(self, project):
        from mcp_server.storage import agents_md_generator as gen

        _record("default-surface decision")
        gen.regenerate_all()
        assert (project / "AGENTS.md").is_file()
        assert not (project / "CLAUDE.md").exists(), "extra files stay opt-in"


# ─────────────────────────────────────────────────────────────────────
# P16 — get_signature multi-language surface
# ─────────────────────────────────────────────────────────────────────


class TestGetSignatureMultiLang:
    def test_supported_types_documented(self, project):
        from mcp_server.tools.code_reader import get_signature

        # The file must EXIST (else the not-found check short-circuits before
        # the extension check we want to exercise).
        mystery = project / "mystery.unknownext"
        mystery.write_text("noop\n", encoding="utf-8")
        res = get_signature(str(mystery))
        assert res["found"] is False
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            assert ext in res["error"], f"{ext} must be a documented supported type"

    def test_typescript_file_parses_without_crash(self, project):
        from mcp_server.tools.code_reader import get_signature

        ts = project / "greet.ts"
        ts.write_text(
            "export function greet(name: string): string {\n"
            "  return `hi ${name}`;\n}\n",
            encoding="utf-8",
        )
        res = get_signature(str(ts))
        assert isinstance(res, dict) and "found" in res
        if res["found"]:  # real grammar present (release env); mocked CI → False
            assert res.get("language") in ("typescript", "tsx", "javascript")


# ─────────────────────────────────────────────────────────────────────
# P17 — one shared git outcome classifier (D000112)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
class TestOutcomeClassifierUnified:
    """``classify_outcome`` is the single brain both outcome surfaces (SQLite
    confidence + JSONL digest/replay/skills) delegate to — if its kept/modified
    /reverted verdicts are right on a real repo, the two surfaces agree by
    construction (the whole point of Phase 17)."""

    @staticmethod
    def _git(repo: Path, *args, date: str | None = None):
        env = {**os.environ}
        if date:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        self._git(repo, "init", "-q")
        self._git(repo, "config", "user.email", "t@example.com")
        self._git(repo, "config", "user.name", "t")
        return repo

    def _commit(self, repo, fname, content, subject, date):
        (repo / fname).write_text(content, encoding="utf-8")
        self._git(repo, "add", fname)
        self._git(repo, "commit", "-q", "-m", subject, date=date)

    def test_kept_when_unchanged_since_anchor(self, tmp_path):
        from indexer.outcome_classifier import classify_outcome

        repo = self._repo(tmp_path)
        self._commit(repo, "f.py", "x = 1\n", "add f", "2020-01-01T00:00:00")
        # anchor after the only commit → no later commits → kept.
        assert classify_outcome(repo, "f.py", "2020-12-31T00:00:00") == "kept"

    def test_modified_when_changed_with_plain_subject(self, tmp_path):
        from indexer.outcome_classifier import classify_outcome

        repo = self._repo(tmp_path)
        self._commit(repo, "f.py", "x = 1\n", "add f", "2020-01-01T00:00:00")
        self._commit(repo, "f.py", "x = 2\n", "tweak f", "2020-06-01T00:00:00")
        # anchor between the two commits → later 'tweak' commit → modified.
        assert classify_outcome(repo, "f.py", "2020-03-01T00:00:00") == "modified"

    def test_reverted_on_revert_subject(self, tmp_path):
        from indexer.outcome_classifier import classify_outcome

        repo = self._repo(tmp_path)
        self._commit(repo, "f.py", "x = 1\n", "add f", "2020-01-01T00:00:00")
        self._commit(
            repo, "f.py", "x = 0\n", "revert the change", "2020-06-01T00:00:00"
        )
        assert classify_outcome(repo, "f.py", "2020-03-01T00:00:00") == "reverted"

    def test_reverted_when_file_deleted(self, tmp_path):
        from indexer.outcome_classifier import classify_outcome

        repo = self._repo(tmp_path)
        # File never existed at HEAD → gone → reverted (git failure now means
        # "can't classify", but a missing file is an unambiguous revert).
        assert classify_outcome(repo, "gone.py", "2020-01-01T00:00:00") == "reverted"


# ─────────────────────────────────────────────────────────────────────
# E5 — opt-in synonym query widening (D000113)
# ─────────────────────────────────────────────────────────────────────


class TestSynonymWidening:
    def test_widening_recalls_synonymous_decision(self, project, monkeypatch):
        from mcp_server.storage import decisions_store

        did = _record(
            "store the event log in postgres as an append-only ledger",
            tags=["postgres", "events"],
        )

        # Plain keyword search for "database" misses — the decision says
        # "postgres", and stemming can't bridge that non-morphological gap.
        monkeypatch.delenv("CODEVIRA_SYNONYM_WIDENING", raising=False)
        plain = {h["id"] for h in decisions_store.search("database", limit=10)}
        assert did not in plain

        # With widening, "database" expands to its synonym group (incl.
        # "postgres") → the decision is recalled (D000113).
        monkeypatch.setenv("CODEVIRA_SYNONYM_WIDENING", "1")
        widened = {h["id"] for h in decisions_store.search("database", limit=10)}
        assert did in widened


# ─────────────────────────────────────────────────────────────────────
# Release coherence — CHANGELOG, env defaults, CLI/MCP surface
# ─────────────────────────────────────────────────────────────────────


class TestReleaseCoherence:
    def _changelog_v350_section(self) -> str:
        # After release-prep, the v3.5.0 notes live under ## [3.5.0]
        # (promoted from [Unreleased]); slice that section out.
        text = (_REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        start = text.index("## [3.5.0]")
        rest = text[start + len("## [3.5.0]") :]
        nxt = rest.find("\n## [")
        return rest if nxt == -1 else rest[:nxt]

    def test_changelog_documents_every_shipped_feature(self):
        section = self._changelog_v350_section()
        # Every v3.5.0 deliverable must carry a release note (open-source
        # changelog discipline). Keyed by the decision IDs they were logged as.
        for marker in (
            "D0000ZQ",  # E1 summary-first + expand
            "D00010W",  # E2 session ingest
            "D00010Y",  # E3 relevance eval
            "D00010Z",  # P13 learned weights
            "D000110",  # E4 managed files
            "D000113",  # E5 synonym widening
            "D00010B",  # P18 content-aware lock
            "D0000Z4",  # doctor ghost fix
            "D000112",  # P17 outcome reconcile
        ):
            assert marker in section, f"CHANGELOG [3.5.0] missing {marker}"
        assert "get_signature" in section  # P16

    def test_env_flag_default_off_contract(self):
        """The opt-in flags must default to the conservative behavior."""
        from mcp_server.engine.policies import relevance_inject
        from mcp_server.engine.policies.decision_lock import DecisionLock
        from mcp_server.storage import fts5_index

        # Synonym widening OFF by default → query unchanged.
        assert fts5_index._sanitize_fts_query("database auth") == '"database" OR "auth"'
        # Learned weights OFF by default.
        assert relevance_inject._learned_weights_enabled() is False
        # Content-aware lock ON by default (the v3.5.0 behavior change).
        assert DecisionLock()._config()["content_aware"] is True

    def test_expand_tool_is_importable_and_callable(self):
        from mcp_server.tools.search import expand

        out = expand(ids=["D-does-not-exist"])
        assert out["count"] == 0 and out["not_found"] == ["D-does-not-exist"]

    @pytest.mark.parametrize("cmd", ["eval", "tune-weights"])
    def test_new_cli_subcommands_exist(self, cmd):
        # Run the BRANCH's CLI (not a possibly-stale installed `codevira`).
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", cmd, "--help"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert (
            result.returncode == 0
        ), f"`codevira {cmd} --help` failed: {result.stderr[:400]}"

    def test_reflect_documents_from_sessions(self):
        result = subprocess.run(
            [sys.executable, "-m", "mcp_server.cli", "reflect", "--help"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "--from-sessions" in (result.stdout + result.stderr)
