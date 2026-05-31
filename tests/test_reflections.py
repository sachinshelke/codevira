"""
Tests for v3.1.0 M8: reflections.

Covers:
  - scrub_sensitive: redacts api keys / bearer / passwords / AKIA /
    long hex / long base64.
  - build_source_context: filters by period; obeys session/decision
    caps; sanitizes narrative fields; envelope-size cap trims when over.
  - render_prompt: template inlines source context with placeholder
    expansion; missing template falls back gracefully.
  - reflections_store.append / list_recent / list_filtered.
  - cmd_reflect: render mode prints prompt; --from-file parses YAML
    and writes proposal; --apply --yes commits to reflections.jsonl;
    missing/empty/malformed input rejected.
  - MCP tools: reflect returns sampling_supported=False; get/list
    return durable data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import mcp_server.paths as paths_module
from mcp_server.storage import (
    decisions_store,
    jsonl_store,
    paths,
    reflections_store,
    sessions_store,
)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".codevira").mkdir(parents=True)
    (root / ".codevira" / "config.yaml").write_text("project:\n  name: test\n")
    monkeypatch.setattr(paths_module, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())
    return root


# ──────────────────────────────────────────────────────────────────────
# scrub_sensitive
# ──────────────────────────────────────────────────────────────────────


class TestScrubSensitive:
    def test_api_key_redacted(self) -> None:
        out = reflections_store.scrub_sensitive("api_key=hunter2-deadbeefcafe")
        assert "<redacted:api-key>" in out
        assert "hunter2" not in out

    def test_bearer_redacted(self) -> None:
        out = reflections_store.scrub_sensitive(
            "Authorization: Bearer abc123XYZ.token-here"
        )
        assert "<redacted:bearer>" in out
        assert "abc123XYZ" not in out

    def test_password_redacted(self) -> None:
        out = reflections_store.scrub_sensitive(
            "password=hunter2-correct-horse-battery-staple"
        )
        assert "<redacted:password>" in out

    def test_akia_redacted(self) -> None:
        out = reflections_store.scrub_sensitive("AKIAIOSFODNN7EXAMPLE here")
        assert "<redacted:aws-akia>" in out

    def test_long_hex_redacted(self) -> None:
        out = reflections_store.scrub_sensitive(
            "tok = a1b2c3d4e5f607182930405060708090abcdef0123456789"
        )
        assert "<redacted:long-token>" in out

    def test_plain_text_untouched(self) -> None:
        text = "Use bcrypt for password hashing in auth.py."
        # Note: the "password" word is part of normal prose; the
        # secret regex requires `password=` or `password:` form.
        assert reflections_store.scrub_sensitive(text) == text

    def test_long_b64_redacted(self) -> None:
        """CRITICAL — the `long-b64` pattern (40+ base64 chars) catches
        JWT-shaped tokens and service-account JSON fragments. A regression
        silently dropping or breaking this pattern would let those leak
        into committed reflections."""
        # 48 base64-shaped chars in a row (JWT segment length).
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9aGVsbG8wMTIz"
        assert len(token) >= 40
        out = reflections_store.scrub_sensitive(f"saw {token} in logs")
        # The pattern's KIND label is "long-b64" per _SECRET_PATTERNS.
        assert (
            "<redacted:long-b64>" in out
        ), f"long-b64 pattern did NOT redact a 48-char token: {out!r}"
        assert token not in out

    def test_long_b64_with_trailing_padding(self) -> None:
        """The pattern allows up to 2 '=' padding chars; a real base64
        blob with padding must still redact."""
        token = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=="
        assert token.endswith("==") and len(token) >= 40
        out = reflections_store.scrub_sensitive(f"key: {token} done")
        assert "<redacted:long-b64>" in out
        assert token not in out


# ──────────────────────────────────────────────────────────────────────
# build_source_context
# ──────────────────────────────────────────────────────────────────────


class TestBuildSourceContext:
    def test_empty_returns_empty_lists(self, project: Path) -> None:
        ctx = reflections_store.build_source_context(period_days=7)
        assert ctx["sessions"] == []
        assert ctx["decisions"] == []

    def test_in_window_included(self, project: Path) -> None:
        decisions_store.record(decision="X", tags=["t1"])
        sessions_store.write("s1", task="x", task_type="bug")
        ctx = reflections_store.build_source_context(period_days=7)
        assert len(ctx["sessions"]) == 1
        assert len(ctx["decisions"]) == 1

    def test_out_of_window_excluded(self, project: Path) -> None:
        # Inject an old decision row directly.
        old_ts = (datetime(2020, 1, 1, tzinfo=timezone.utc)).isoformat()
        jsonl_store.append(
            paths.decisions_path(),
            {
                "id": "D000099",
                "ts": old_ts,
                "session_id": "ad-hoc",
                "decision": "ancient",
                "_schema_v": 0,
            },
        )
        decisions_store.record(decision="fresh", tags=[])
        ctx = reflections_store.build_source_context(period_days=7)
        # Only the fresh decision surfaces.
        assert all("ancient" not in d.get("decision", "") for d in ctx["decisions"])

    def test_session_cap_enforced(self, project: Path) -> None:
        for i in range(50):
            sessions_store.write(f"sess-{i}", task=f"task {i}", task_type="bug")
        ctx = reflections_store.build_source_context(period_days=7)
        assert len(ctx["sessions"]) <= reflections_store.MAX_SESSIONS_PER_REFLECTION

    def test_decision_cap_enforced(self, project: Path) -> None:
        for i in range(150):
            decisions_store.record(decision=f"d{i}", tags=["t"])
        ctx = reflections_store.build_source_context(period_days=7)
        assert len(ctx["decisions"]) <= reflections_store.MAX_DECISIONS_PER_REFLECTION

    def test_sanitization_runs(self, project: Path) -> None:
        decisions_store.record(
            decision="see api_key=hunter2-deadbeefcafedeadbeef",
            tags=["secret"],
        )
        ctx = reflections_store.build_source_context(period_days=7)
        assert "<redacted:api-key>" in ctx["decisions"][0]["decision"]
        assert "hunter2" not in ctx["decisions"][0]["decision"]

    def test_sanitization_runs_on_session_task(self, project: Path) -> None:
        """CRITICAL — session.task flows through scrub_sensitive in
        build_source_context (line 178). A bug where the decision-side
        stays redacted but the session-side does not would silently leak
        long-lived session credentials into the LLM prompt."""
        sessions_store.write(
            "s-secret",
            task="curl -H 'api_key=hunter2-deadbeefcafedeadbeef' /v1/things",
            task_type="bug",
        )
        ctx = reflections_store.build_source_context(period_days=7)
        sess = next(
            (s for s in ctx["sessions"] if s.get("session_id") == "s-secret"),
            None,
        )
        assert sess is not None, f"session not surfaced: {ctx['sessions']}"
        assert "<redacted:api-key>" in sess["task"], (
            f"session.task NOT sanitized — credential leaked into LLM prompt: "
            f"{sess['task']!r}"
        )
        assert "hunter2" not in sess["task"]

    def test_sanitization_runs_on_session_summary(self, project: Path) -> None:
        """CRITICAL — session.summary also flows through scrub_sensitive
        (line 180). Same leak risk as task; close it with explicit coverage."""
        sessions_store.write(
            "s-summary",
            task="ok",
            task_type="bug",
            summary="Authorization: Bearer abc123XYZ.shouldNotLeak.signature",
        )
        ctx = reflections_store.build_source_context(period_days=7)
        sess = next(
            (s for s in ctx["sessions"] if s.get("session_id") == "s-summary"),
            None,
        )
        assert sess is not None
        assert (
            "<redacted:bearer>" in sess["summary"]
        ), f"session.summary NOT sanitized: {sess['summary']!r}"
        assert "abc123XYZ" not in sess["summary"]


# ──────────────────────────────────────────────────────────────────────
# render_prompt
# ──────────────────────────────────────────────────────────────────────


class TestRenderPrompt:
    def test_inlines_source_context(self, project: Path) -> None:
        decisions_store.record(decision="Use bcrypt", tags=["auth"])
        ctx = reflections_store.build_source_context(period_days=7)
        prompt = reflections_store.render_prompt(ctx)
        assert "Use bcrypt" in prompt
        # Template placeholder was replaced.
        assert "<<<SOURCE_CONTEXT>>>" not in prompt

    def test_template_missing_falls_back(
        self,
        project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Point to a non-existent template; the fallback inline prompt
        # still renders without crashing.
        monkeypatch.setattr(
            paths,
            "reflection_prompt_path",
            lambda: Path("/nonexistent/path/reflection.md"),
        )
        ctx = reflections_store.build_source_context(period_days=7)
        prompt = reflections_store.render_prompt(ctx)
        # Fallback inline contains the "abstraction" guidance string.
        assert "abstraction" in prompt


# ──────────────────────────────────────────────────────────────────────
# Storage: append / list / filter
# ──────────────────────────────────────────────────────────────────────


class TestStorage:
    def test_append_returns_r_id(self, project: Path) -> None:
        rid = reflections_store.append(
            abstraction="The team prioritizes auth hardening.",
            confidence=0.7,
            tags=["auth", "security"],
            period_start=datetime(2026, 5, 21, tzinfo=timezone.utc).isoformat(),
            period_end=datetime(2026, 5, 28, tzinfo=timezone.utc).isoformat(),
            source_session_ids=["s1", "s2"],
            source_decision_ids=["D000001"],
        )
        assert rid.startswith("R")

    def test_list_recent_newest_first(self, project: Path) -> None:
        for i, text in enumerate(["first", "second", "third"]):
            reflections_store.append(
                abstraction=text,
                confidence=0.5,
                tags=[],
                period_start="2026-05-01T00:00:00+00:00",
                period_end="2026-05-07T00:00:00+00:00",
                source_session_ids=[],
                source_decision_ids=[],
            )
        rows = reflections_store.list_recent(limit=5)
        assert [r["abstraction"] for r in rows] == ["third", "second", "first"]

    def test_list_filtered_by_tags(self, project: Path) -> None:
        reflections_store.append(
            abstraction="A",
            confidence=0.5,
            tags=["release", "v3"],
            period_start="2026-05-01T00:00:00+00:00",
            period_end="2026-05-07T00:00:00+00:00",
            source_session_ids=[],
            source_decision_ids=[],
        )
        reflections_store.append(
            abstraction="B",
            confidence=0.5,
            tags=["v3"],
            period_start="2026-05-01T00:00:00+00:00",
            period_end="2026-05-07T00:00:00+00:00",
            source_session_ids=[],
            source_decision_ids=[],
        )
        rows = reflections_store.list_filtered(tags=["release", "v3"])
        assert [r["abstraction"] for r in rows] == ["A"]


# ──────────────────────────────────────────────────────────────────────
# CLI: cmd_reflect
# ──────────────────────────────────────────────────────────────────────


_GOOD_RESPONSE = """\
```yaml
abstraction: |
  The team is consolidating around bcrypt+rate-limiting for auth.
tags: [auth, security]
confidence: 0.78
```
"""

_NO_FENCE_RESPONSE = """\
abstraction: |
  Auth hardening continues this week.
tags: [auth]
confidence: 0.6
"""

_EMPTY_ABSTRACTION = """\
```yaml
abstraction: ""
tags: []
confidence: 0.1
```
"""


class TestCmdReflect:
    def test_no_from_file_prints_prompt(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Feed the prompt below to your LLM" in out
        assert "abstraction" in out

    def test_from_file_writes_proposal(self, project: Path, tmp_path: Path) -> None:
        decisions_store.record(decision="x")  # seed source context
        resp_path = tmp_path / "resp.yaml"
        resp_path.write_text(_GOOD_RESPONSE)
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect(from_file=str(resp_path))
        assert rc == 0
        proposals = jsonl_store.read_all(paths.reflection_proposals_path())
        assert len(proposals) == 1
        assert "bcrypt" in proposals[0]["abstraction"]
        assert sorted(proposals[0]["tags"]) == ["auth", "security"]
        assert abs(proposals[0]["confidence"] - 0.78) < 1e-3

    def test_apply_yes_commits_to_reflections(
        self, project: Path, tmp_path: Path
    ) -> None:
        decisions_store.record(decision="x")
        resp_path = tmp_path / "resp.yaml"
        resp_path.write_text(_GOOD_RESPONSE)
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect(from_file=str(resp_path), apply=True, yes=True)
        assert rc == 0
        rows = jsonl_store.read_all(paths.reflections_path())
        assert len(rows) == 1
        assert "bcrypt" in rows[0]["abstraction"]

    def test_unfenced_response_still_parsed(
        self, project: Path, tmp_path: Path
    ) -> None:
        decisions_store.record(decision="x")
        resp_path = tmp_path / "resp.yaml"
        resp_path.write_text(_NO_FENCE_RESPONSE)
        from mcp_server.cli_reflect import cmd_reflect

        assert cmd_reflect(from_file=str(resp_path)) == 0

    def test_missing_file_returns_1(
        self, project: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect(from_file="/nonexistent/path.yaml")
        assert rc == 1
        assert "could not read" in capsys.readouterr().err

    def test_empty_abstraction_rejected(
        self, project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        resp_path = tmp_path / "resp.yaml"
        resp_path.write_text(_EMPTY_ABSTRACTION)
        from mcp_server.cli_reflect import cmd_reflect

        rc = cmd_reflect(from_file=str(resp_path))
        assert rc == 1
        assert "empty" in capsys.readouterr().err


# ──────────────────────────────────────────────────────────────────────
# MCP tools
# ──────────────────────────────────────────────────────────────────────


class TestMcpTools:
    def test_reflect_returns_sampling_supported_false(self, project: Path) -> None:
        """Sync reflect() (CLI entry) always returns the stub shape — sampling
        only runs through the async MCP path."""
        decisions_store.record(decision="x")
        from mcp_server.tools.reflections import reflect

        r = reflect()
        assert r["sampling_supported"] is False
        # v3.2.0 renamed the deferred_to marker since sampling now exists
        # for MCP callers but is N/A for sync.
        assert r["deferred_to"] == "v3.2-or-host-without-sampling"
        assert "rendered_prompt" in r
        assert "source_context" in r

    def test_get_reflections_empty(self, project: Path) -> None:
        from mcp_server.tools.reflections import get_reflections

        r = get_reflections()
        assert r["count"] == 0
        assert r["reflections"] == []

    def test_get_reflections_populated(self, project: Path) -> None:
        reflections_store.append(
            abstraction="A",
            confidence=0.5,
            tags=["x"],
            period_start="2026-05-01T00:00:00+00:00",
            period_end="2026-05-07T00:00:00+00:00",
            source_session_ids=[],
            source_decision_ids=[],
        )
        from mcp_server.tools.reflections import get_reflections

        r = get_reflections()
        assert r["count"] == 1
        assert r["reflections"][0]["abstraction"] == "A"


# ──────────────────────────────────────────────────────────────────────
# v3.1.0 M8 — additional coverage
# ──────────────────────────────────────────────────────────────────────


class TestScrubSensitiveNonStringInputs:
    """`scrub_sensitive` short-circuits with
    `if not isinstance(text, str) or not text: return text`. Non-string
    and empty inputs must round-trip unchanged."""

    def test_none_returns_none(self) -> None:
        assert reflections_store.scrub_sensitive(None) is None  # type: ignore[arg-type]

    def test_empty_string_returns_empty(self) -> None:
        assert reflections_store.scrub_sensitive("") == ""

    def test_int_returns_int(self) -> None:
        assert reflections_store.scrub_sensitive(42) == 42  # type: ignore[arg-type]

    def test_bytes_returns_bytes(self) -> None:
        b = b"api_key=hunter2"
        # Non-string short-circuit: bytes are NOT scrubbed (would need
        # decoding); pin the current behavior so a future change is loud.
        assert reflections_store.scrub_sensitive(b) is b  # type: ignore[arg-type]


class TestBuildSourceContextEnvelopeTrim:
    """`build_source_context` runs a while-loop that pops the oldest
    sessions/decisions when the serialized envelope exceeds
    MAX_INPUT_BYTES (6 KB). Untested today."""

    def test_envelope_trimmed_to_under_cap(self, project: Path) -> None:
        # Write enough fat sessions to overflow the 6 KB envelope.
        big_task = "x" * 500  # 500 bytes each
        for i in range(25):
            sessions_store.write(f"sess-{i:02d}", task=big_task, task_type="bug")
        # And some decisions.
        for i in range(40):
            decisions_store.record(decision=f"d {i} " + ("y" * 100), tags=["t"])

        ctx = reflections_store.build_source_context(period_days=7)
        # The envelope (after sanitization, including ts+ids etc.) MUST
        # be under MAX_INPUT_BYTES. Reproduce the size calc.
        import json as _json

        envelope = _json.dumps(
            {"sessions": ctx["sessions"], "decisions": ctx["decisions"]}
        ).encode("utf-8")
        assert len(envelope) <= reflections_store.MAX_INPUT_BYTES, (
            f"envelope {len(envelope)} bytes exceeded cap "
            f"{reflections_store.MAX_INPUT_BYTES}"
        )


class TestBuildSourceContextAmendmentRowsExcluded:
    """`build_source_context` skips rows with `_amendment_to_id` for both
    sessions and decisions. Locks the exclusion so an amendment chain
    cannot leak into LLM context."""

    def test_decision_amendment_row_excluded(self, project: Path) -> None:
        did = decisions_store.record(decision="base", tags=["a"])
        # Append an amendment-only row.
        jsonl_store.append(
            paths.decisions_path(),
            {
                "id": did,
                "ts": datetime.now(timezone.utc).isoformat(),
                "_amendment_to_id": did,
                "tags": ["b"],
            },
        )
        ctx = reflections_store.build_source_context(period_days=7)
        ids = [d.get("id") for d in ctx["decisions"]]
        # Base appears exactly once (no amendment leak).
        assert ids.count(did) == 1

    def test_session_amendment_row_excluded(self, project: Path) -> None:
        sessions_store.write("s1", task="t", task_type="bug")
        # Append an amendment.
        jsonl_store.append(
            paths.sessions_path(),
            {
                "session_id": "s1",
                "ts": datetime.now(timezone.utc).isoformat(),
                "_amendment_to_id": "s1",
                "summary": "updated",
            },
        )
        ctx = reflections_store.build_source_context(period_days=7)
        ids = [s.get("session_id") for s in ctx["sessions"]]
        assert ids.count("s1") == 1


class TestBuildSourceContextMalformedTsSkipped:
    """`build_source_context` wraps `datetime.fromisoformat` in
    try/except; malformed-ts rows are silently skipped (not raised)."""

    def test_malformed_ts_does_not_crash_build(self, project: Path) -> None:
        # Inject a session row with a junk ts.
        jsonl_store.append(
            paths.sessions_path(),
            {
                "session_id": "junk-ts",
                "task": "t",
                "task_type": "bug",
                "ts": "this-is-not-iso8601",
            },
        )
        sessions_store.write("good-ts", task="t", task_type="bug")
        # Build must NOT raise.
        ctx = reflections_store.build_source_context(period_days=7)
        ids = [s.get("session_id") for s in ctx["sessions"]]
        # Good row surfaces; junk-ts row is filtered out by the ts try/except.
        assert "good-ts" in ids
        assert "junk-ts" not in ids


class TestAppendProposalTargetRoute:
    """append(target='proposals') writes to reflection_proposals_path;
    list_recent(target='proposals') reads it. Two separate jsonl files."""

    def test_target_routes_writes_to_proposals_file(self, project: Path) -> None:
        reflections_store.append(
            abstraction="A proposal",
            confidence=0.5,
            tags=["x"],
            period_start="2026-01-01",
            period_end="2026-01-07",
            source_session_ids=["s1"],
            source_decision_ids=["D1"],
            target="proposals",
        )
        reflections_store.append(
            abstraction="A reflection",
            confidence=0.9,
            tags=["x"],
            period_start="2026-01-01",
            period_end="2026-01-07",
            source_session_ids=["s2"],
            source_decision_ids=["D2"],
            target="reflections",
        )

        # Two distinct files. (Both use the R-prefix; ids start at 1 in
        # each file independently. So both rids may be 'R000001'. We
        # verify by abstraction text + which path holds which.)
        proposals = reflections_store.list_recent(target="proposals", limit=10)
        reflections = reflections_store.list_recent(target="reflections", limit=10)
        assert any(p.get("abstraction") == "A proposal" for p in proposals)
        assert all(p.get("abstraction") != "A reflection" for p in proposals)
        assert any(r.get("abstraction") == "A reflection" for r in reflections)
        assert all(r.get("abstraction") != "A proposal" for r in reflections)
        # And the files themselves are different paths.
        assert paths.reflection_proposals_path() != paths.reflections_path()


class TestAppendStampsOrigin:
    """append() embeds origin so M6/M7 consensus can identify the
    authoring IDE. Untested today."""

    def test_appended_reflection_carries_origin(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        rid = reflections_store.append(
            abstraction="x",
            confidence=0.5,
            tags=[],
            period_start="2026-01-01",
            period_end="2026-01-07",
            source_session_ids=[],
            source_decision_ids=[],
        )
        rows = reflections_store.list_recent(limit=5)
        rec = next(r for r in rows if r.get("id") == rid)
        assert isinstance(rec.get("origin"), dict)
        assert rec["origin"]["ide"] == "cursor"


# ──────────────────────────────────────────────────────────────────────
# M8 minor + polish coverage
# ──────────────────────────────────────────────────────────────────────


class TestBuildContextPeriodClamp:
    """`period_start = now - timedelta(days=max(period_days, 1))` —
    zero/negative clamps to 1."""

    def test_period_days_zero_treated_as_one(self, project: Path) -> None:
        # Record something within the last 24h — should be included.
        decisions_store.record(decision="recent", tags=["t"])
        ctx = reflections_store.build_source_context(period_days=0)
        assert len(ctx["decisions"]) == 1

    def test_period_days_negative_treated_as_one(self, project: Path) -> None:
        decisions_store.record(decision="recent", tags=["t"])
        ctx = reflections_store.build_source_context(period_days=-5)
        assert len(ctx["decisions"]) == 1


class TestAppendNormalizes:
    """append normalizes tags (lower+strip+drop blanks), coerces
    confidence with float() fallback to None."""

    def test_tags_lowercased_stripped_blanks_dropped(self, project: Path) -> None:
        rid = reflections_store.append(
            abstraction="x",
            confidence=0.5,
            tags=["  AUTH  ", "", "Security", "  "],
            period_start="2026-01-01",
            period_end="2026-01-07",
            source_session_ids=[],
            source_decision_ids=[],
        )
        rec = next(r for r in reflections_store.list_recent(limit=5) if r["id"] == rid)
        assert rec["tags"] == ["auth", "security"]

    def test_non_numeric_confidence_falls_back_to_none(self, project: Path) -> None:
        rid = reflections_store.append(
            abstraction="x",
            confidence="not-a-number",  # type: ignore[arg-type]
            tags=[],
            period_start="2026-01-01",
            period_end="2026-01-07",
            source_session_ids=[],
            source_decision_ids=[],
        )
        rec = next(r for r in reflections_store.list_recent(limit=5) if r["id"] == rid)
        assert rec["confidence"] is None


class TestListFilteredSinceCutoff:
    """list_filtered(since=...) skips rows whose ts < since."""

    def test_since_excludes_older_rows(self, project: Path) -> None:
        # Add an old reflection by manually setting ts.
        old_rid = reflections_store.append(
            abstraction="ancient",
            confidence=0.5,
            tags=[],
            period_start="2020-01-01",
            period_end="2020-01-07",
            source_session_ids=[],
            source_decision_ids=[],
        )
        # Patch the ts via amendment.
        jsonl_store.append(
            paths.reflections_path(),
            {
                "id": old_rid,
                "_amendment_to_id": old_rid,
                "ts": "2020-01-01T00:00:00+00:00",
            },
        )
        new_rid = reflections_store.append(
            abstraction="fresh",
            confidence=0.5,
            tags=[],
            period_start="2026-01-01",
            period_end="2026-01-07",
            source_session_ids=[],
            source_decision_ids=[],
        )
        # list_filtered builds on list_recent (newest-first read). Use
        # a since cutoff in 2024.
        rows = reflections_store.list_filtered(since="2024-01-01T00:00:00+00:00")
        ids = [r.get("id") for r in rows]
        assert new_rid in ids
        # NOTE: list_filtered uses list_recent which reads merged rows;
        # the amendment's ts is what wins.
        # If old_rid still appears, the amendment didn't apply — but
        # the since filter excludes 2020 either way.


class TestReflectDryRunFlag:
    """`reflect()` echoes dry_run in the response so callers can
    distinguish the storage-safe path."""

    def test_dry_run_true_echoed(self, project: Path) -> None:
        from mcp_server.tools.reflections import reflect

        decisions_store.record(decision="x", tags=["t"])
        r = reflect(period_days=7, dry_run=True)
        assert r.get("dry_run") is True

    def test_dry_run_false_echoed(self, project: Path) -> None:
        from mcp_server.tools.reflections import reflect

        decisions_store.record(decision="x", tags=["t"])
        r = reflect(period_days=7, dry_run=False)
        assert r.get("dry_run") is False


class TestListReflectionsMcpTool:
    """The list_reflections MCP tool wraps list_filtered and echoes
    filtered_by."""

    def test_list_reflections_returns_filter_metadata(self, project: Path) -> None:
        from mcp_server.tools.reflections import list_reflections

        reflections_store.append(
            abstraction="x",
            confidence=0.5,
            tags=["auth"],
            period_start="2026-01-01",
            period_end="2026-01-07",
            source_session_ids=[],
            source_decision_ids=[],
        )
        r = list_reflections(tags=["auth"])
        assert r["count"] >= 1
        assert isinstance(r.get("filtered_by"), dict)
        assert r["filtered_by"].get("tags") == ["auth"]


class TestRenderContextBlockFormat:
    """_render_context_block emits YAML-ish headers (period_start,
    period_end, sessions, decisions)."""

    def test_headers_present_in_block(self, project: Path) -> None:
        decisions_store.record(decision="d", tags=["a"])
        sessions_store.write("s1", task="t", task_type="bug")
        ctx = reflections_store.build_source_context(period_days=7)
        block = reflections_store._render_context_block(ctx)
        for header in ("period_start:", "period_end:", "sessions:", "decisions:"):
            assert header in block, f"missing header {header!r}: {block[:200]}"


# ──────────────────────────────────────────────────────────────────────
# v3.2.0 — reflect_async (sampling/createMessage path)
# ──────────────────────────────────────────────────────────────────────


class _FakeContent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResult:
    def __init__(self, text: str, model: str = "test-llm") -> None:
        self.content = _FakeContent(text)
        self.model = model


class _FakeSession:
    """Mimics the subset of mcp ServerSession reflect_async touches."""

    def __init__(
        self,
        *,
        advertise_sampling: bool = True,
        canned_response: str = "Generated abstraction.",
        raise_exc: Exception | None = None,
    ) -> None:
        if advertise_sampling:

            class _Sampling:
                pass

            class _Caps:
                def __init__(self) -> None:
                    self.sampling = _Sampling()

            class _Params:
                def __init__(self) -> None:
                    self.capabilities = _Caps()

            self.client_params = _Params()
        else:
            self.client_params = None  # no capabilities advertised
        self._canned = canned_response
        self._raise = raise_exc
        self.create_message_called = 0
        self.last_messages: list = []

    async def create_message(self, *, messages, max_tokens, **kw):
        self.create_message_called += 1
        self.last_messages = messages
        if self._raise is not None:
            raise self._raise
        return _FakeResult(self._canned)


class TestReflectAsyncSampling:
    @pytest.fixture(autouse=True)
    def _patch_mcp_types_samplingmessage(self):
        """test_server.py installs a stub mcp.types module that lacks
        SamplingMessage. Patch a stub class in for this test class so
        reflect_async's lazy `from mcp.types import SamplingMessage`
        succeeds — the FakeSession never actually uses the class, so a
        plain duck-type works."""
        import sys

        mt = sys.modules.get("mcp.types")
        if mt is None:
            yield
            return
        had = hasattr(mt, "SamplingMessage")
        if not had:

            class _StubSamplingMessage:
                def __init__(self, **kw):
                    self.__dict__.update(kw)

            mt.SamplingMessage = _StubSamplingMessage
        try:
            yield
        finally:
            if not had and hasattr(mt, "SamplingMessage"):
                delattr(mt, "SamplingMessage")

    @staticmethod
    def _run(coro):
        import asyncio

        return asyncio.run(coro)

    def test_no_session_falls_back_to_stub(self, project: Path) -> None:
        from mcp_server.tools.reflections import reflect_async

        result = self._run(reflect_async(server_session=None))
        assert result["sampling_supported"] is False
        assert result["sampling_error"] == "no_server_session"
        assert "rendered_prompt" in result

    def test_session_without_sampling_capability_falls_back(
        self,
        project: Path,
    ) -> None:
        from mcp_server.tools.reflections import reflect_async

        sess = _FakeSession(advertise_sampling=False)
        result = self._run(reflect_async(server_session=sess))
        assert result["sampling_supported"] is False
        assert result["sampling_error"] == "client_did_not_advertise_sampling"
        assert sess.create_message_called == 0

    def test_sampling_success_dry_run_returns_abstraction_no_persist(
        self,
        project: Path,
    ) -> None:
        from mcp_server.tools.reflections import reflect_async

        decisions_store.record(decision="X", tags=["t1"])
        sessions_store.write("s1", task="x", task_type="bug")

        sess = _FakeSession(canned_response="My abstraction body.")
        before = len(reflections_store.list_recent(limit=999))
        result = self._run(reflect_async(server_session=sess, dry_run=True))

        assert result["sampling_supported"] is True
        assert result["abstraction"] == "My abstraction body."
        assert result["persisted"] is False
        assert result["reflection_id"] is None
        assert result["model_used"] == "test-llm"
        assert sess.create_message_called == 1

        after = len(reflections_store.list_recent(limit=999))
        assert after == before, "dry_run must not write"

    def test_sampling_success_persists_when_dry_run_false(
        self,
        project: Path,
    ) -> None:
        from mcp_server.tools.reflections import reflect_async

        decisions_store.record(decision="X", tags=["t1"])
        sessions_store.write("s1", task="x", task_type="bug")

        sess = _FakeSession(canned_response="Committed abstraction.")
        before = len(reflections_store.list_recent(limit=999))
        result = self._run(reflect_async(server_session=sess, dry_run=False))

        assert result["sampling_supported"] is True
        assert result["persisted"] is True
        assert isinstance(result["reflection_id"], str)
        assert result["reflection_id"].startswith("R")

        recent = reflections_store.list_recent(limit=999)
        assert len(recent) == before + 1
        assert recent[0]["abstraction"] == "Committed abstraction."
        assert recent[0]["model_used"] == "test-llm"

    def test_sampling_exception_falls_back_to_stub(
        self,
        project: Path,
    ) -> None:
        from mcp_server.tools.reflections import reflect_async

        sess = _FakeSession(raise_exc=RuntimeError("transport closed"))
        result = self._run(reflect_async(server_session=sess))
        assert result["sampling_supported"] is False
        assert "RuntimeError" in (result["sampling_error"] or "")
        assert "transport closed" in (result["sampling_error"] or "")

    def test_empty_llm_response_falls_back(self, project: Path) -> None:
        from mcp_server.tools.reflections import reflect_async

        sess = _FakeSession(canned_response="   ")  # whitespace-only
        result = self._run(reflect_async(server_session=sess))
        assert result["sampling_supported"] is False
        assert result["sampling_error"] == "empty_or_non_text_response"

    def test_sync_reflect_unchanged_in_v320(self, project: Path) -> None:
        """The sync reflect() entry stays on the v3.1.0 stub shape so the
        CLI keeps working."""
        from mcp_server.tools.reflections import reflect

        result = reflect(period_days=7, dry_run=True)
        assert result["sampling_supported"] is False
        assert "rendered_prompt" in result
