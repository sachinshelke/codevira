"""E2 (Phase 20) — read-only session-transcript ingest.

Pins the contract from D0000WQ:

* Per-tool parsers (Claude Code / Codex / Gemini) reduce native logs to a
  normalized SessionDigest; failures + user-corrections are flagged with NO
  LLM.
* Parsers are DEFENSIVE: an unknown / garbage format returns None, never
  raises; one IDE's broken log can't break the scan.
* Retained excerpts are SANITIZED (secrets scrubbed) and capped.
* The scan is READ-ONLY and feeds CANDIDATES only — nothing is auto-committed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_server.ingest import claude_code, codex, gemini, scan
from mcp_server.ingest import heuristics as H


# ─────────────────────────────────────────────────────────────────────
# Heuristics (no LLM)
# ─────────────────────────────────────────────────────────────────────


class TestHeuristics:
    @pytest.mark.parametrize(
        "text",
        [
            "no, that's wrong",
            "actually, revert that",
            "don't do that",
            "you broke the build",
            "that's not what I asked",
            "use bcrypt instead",
        ],
    )
    def test_corrections_detected(self, text: str) -> None:
        assert H.looks_like_correction(text)

    @pytest.mark.parametrize(
        "text",
        ["yes, looks good", "thanks, continue", "ship it", "great work"],
    )
    def test_non_corrections_pass(self, text: str) -> None:
        assert not H.looks_like_correction(text)

    def test_output_failure_markers(self) -> None:
        assert H.output_looks_failed("Traceback (most recent call last)")
        assert H.output_looks_failed({"exit_code": 1})
        assert H.output_looks_failed({"success": False})
        assert not H.output_looks_failed("done, 3 files changed")
        assert not H.output_looks_failed({"exit_code": 0})

    def test_excerpt_sanitizes_secrets(self) -> None:
        out = H.excerpt("error near AKIAIOSFODNN7EXAMPLE while connecting")
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "redacted" in out

    def test_excerpt_caps_length(self) -> None:
        out = H.excerpt("x " * 500)
        assert len(out) <= H.EXCERPT_CHARS


# ─────────────────────────────────────────────────────────────────────
# Per-tool parsers
# ─────────────────────────────────────────────────────────────────────


def _claude_log(path: Path) -> None:
    records = [
        {
            "type": "user",
            "timestamp": "2026-06-16T00:00:00Z",
            "message": {"content": "please edit foo"},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    },
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
                        "content": "Exit code 1: command failed near AKIAIOSFODNN7EXAMPLE",
                    },
                ]
            },
        },
        {"type": "user", "message": {"content": "no, that's wrong — revert it"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records))


def _codex_log(path: Path) -> None:
    records = [
        {
            "type": "session_meta",
            "timestamp": "2026-06-16T00:00:00Z",
            "payload": {"id": "cx-1"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "call_id": "c1",
                "arguments": "{}",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "c1",
                "output": "bash: error: command not found",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "actually that's incorrect"}
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(r) for r in records))


def _gemini_log(path: Path) -> None:
    chat = {
        "messages": [
            {
                "role": "model",
                "parts": [{"functionCall": {"name": "run_shell", "args": {}}}],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "run_shell",
                            "response": {"error": "permission denied"},
                        }
                    }
                ],
            },
            {"role": "user", "parts": [{"text": "no, don't do that"}]},
        ]
    }
    path.write_text(json.dumps(chat))


class TestClaudeCodeParser:
    def test_parses_failure_and_correction(self, tmp_path: Path) -> None:
        f = tmp_path / "sess.jsonl"
        _claude_log(f)
        d = claude_code.parse_file(f)
        assert d is not None and d.source == "claude_code"
        assert d.n_tool_calls == 1
        assert d.n_failures == 1 and d.failures[0].tool == "Bash"
        assert "AKIAIOSFODNN7EXAMPLE" not in d.failures[0].error_excerpt  # sanitized
        assert d.n_corrections == 1 and "revert" in d.corrections[0].excerpt
        assert d.is_interesting

    def test_garbage_file_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "junk.jsonl"
        f.write_text("not json at all\n{also not}\n")
        assert claude_code.parse_file(f) is None


class TestCodexParser:
    def test_parses_failure_and_correction(self, tmp_path: Path) -> None:
        f = tmp_path / "cx.jsonl"
        _codex_log(f)
        d = codex.parse_file(f)
        assert d is not None and d.source == "codex"
        assert d.n_tool_calls == 1
        assert d.n_failures == 1 and d.failures[0].tool == "shell"
        assert d.n_corrections == 1
        assert d.is_interesting

    def test_unrelated_jsonl_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "other.jsonl"
        f.write_text(json.dumps({"type": "totally-unknown", "x": 1}))
        assert codex.parse_file(f) is None


class TestGeminiParser:
    def test_parses_failure_and_correction(self, tmp_path: Path) -> None:
        f = tmp_path / "chat.json"
        _gemini_log(f)
        d = gemini.parse_file(f)
        assert d is not None and d.source == "gemini"
        assert d.n_tool_calls == 1
        assert d.n_failures == 1
        assert d.n_corrections == 1

    def test_non_chat_json_returns_none(self, tmp_path: Path) -> None:
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"theme": "dark", "model": "gemini"}))
        assert gemini.parse_file(f) is None


# ─────────────────────────────────────────────────────────────────────
# scan orchestrator
# ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def fixture_roots(tmp_path: Path):
    """Seed each IDE's root with one interesting session for project P."""
    project = tmp_path / "proj"
    project.mkdir()

    cc_root = tmp_path / "claude"
    (cc_root / str(project).replace("/", "-")).mkdir(parents=True)
    _claude_log(cc_root / str(project).replace("/", "-") / "s.jsonl")

    cx_root = tmp_path / "codex" / "2026" / "06"
    cx_root.mkdir(parents=True)
    _codex_log(cx_root / "c.jsonl")

    gm_root = tmp_path / "gemini" / "hash" / "chats"
    gm_root.mkdir(parents=True)
    _gemini_log(gm_root / "chat.json")

    roots = {
        "claude_code": tmp_path / "claude",
        "codex": tmp_path / "codex",
        "gemini": tmp_path / "gemini",
    }
    return project, roots, tmp_path


class TestScan:
    def test_scans_all_sources(self, fixture_roots) -> None:
        project, roots, _ = fixture_roots
        digests = scan.scan_sessions(project, roots=roots, since_days=3650)
        sources = {d.source for d in digests}
        assert sources == {"claude_code", "codex", "gemini"}
        assert all(d.is_interesting for d in digests)

    def test_source_filter(self, fixture_roots) -> None:
        project, roots, _ = fixture_roots
        digests = scan.scan_sessions(
            project, roots=roots, sources=["claude_code"], since_days=3650
        )
        assert {d.source for d in digests} == {"claude_code"}

    def test_since_days_excludes_old(self, fixture_roots) -> None:
        project, roots, _ = fixture_roots
        # now far in the future + tiny window → everything is "too old".
        digests = scan.scan_sessions(project, roots=roots, since_days=1, now=9.9e9)
        assert digests == []

    def test_one_broken_source_does_not_break_scan(
        self, fixture_roots, tmp_path
    ) -> None:
        project, roots, _ = fixture_roots
        # Point codex at a dir full of garbage; claude/gemini still return.
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "x.jsonl").write_text("\x00\x01 not json")
        roots["codex"] = bad
        digests = scan.scan_sessions(project, roots=roots, since_days=3650)
        assert {d.source for d in digests} == {"claude_code", "gemini"}

    def test_to_reflection_signals_shape(self, fixture_roots) -> None:
        project, roots, _ = fixture_roots
        sigs = scan.to_reflection_signals(
            scan.scan_sessions(project, roots=roots, since_days=3650)
        )
        assert sigs and all(
            {"source", "failures", "corrections"} <= set(s) for s in sigs
        )


# ─────────────────────────────────────────────────────────────────────
# Read-only / candidate-only invariants
# ─────────────────────────────────────────────────────────────────────


class TestReadOnlyInvariants:
    def _snapshot(self, root: Path) -> dict:
        return {
            str(p): (p.stat().st_mtime_ns, p.stat().st_size)
            for p in root.rglob("*")
            if p.is_file()
        }

    def test_scan_never_mutates_logs(self, fixture_roots) -> None:
        project, roots, tmp_path = fixture_roots
        before = self._snapshot(tmp_path)
        scan.scan_sessions(project, roots=roots, since_days=3650)
        after = self._snapshot(tmp_path)
        assert before == after, "transcript scan must not create/modify any file"

    def test_build_source_context_folds_signals_without_writing(
        self, tmp_path, monkeypatch
    ) -> None:
        """include_transcripts surfaces signals but commits nothing."""
        from mcp_server.storage import reflections_store
        from mcp_server.ingest.models import SessionDigest, ToolEvent

        canned = [
            SessionDigest(
                source="claude_code",
                session_id="s1",
                path="/x",
                started_at=None,
                n_tool_calls=3,
                n_failures=1,
                n_corrections=0,
                failures=(ToolEvent(tool="Bash", error_excerpt="boom", seq=1),),
                corrections=(),
            )
        ]
        # Lazy import target inside build_source_context.
        monkeypatch.setattr(
            "mcp_server.ingest.scan_sessions", lambda *a, **k: canned, raising=False
        )
        monkeypatch.setattr("mcp_server.paths.get_project_root", lambda: tmp_path)
        ctx = reflections_store.build_source_context(
            period_days=7, include_transcripts=True, project_root=tmp_path
        )
        assert ctx["transcript_signals"], "signals should be folded in"
        assert ctx["transcript_signals"][0]["failures"][0]["tool"] == "Bash"
        # Candidate-only: no reflections file was written by merely building ctx.
        assert not (tmp_path / ".codevira" / "reflections.jsonl").exists()
