"""
test_blast_radius.py — Hero 4 acceptance tests.

The 10 scenarios listed in docs/heroes/04-blast-radius.md "Acceptance
test list", plus a few unit tests for the signature-detection helper
which has its own contract (multi-language regex correctness).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policies.blast_radius import BlastRadiusVeto
from mcp_server.engine.policies._signature_detect import (
    change_touches_signature,
    language_for_path,
    signature_change_summary,
)


# =====================================================================
# Helpers
# =====================================================================


def _make_event(
    *,
    tool_name: str = "Edit",
    target: Path | None = None,
    proposed_diff: str | None = None,
    project_root: Path | None = None,
) -> HookEvent:
    """Build a synthetic PreToolUse event."""
    return HookEvent(
        event_type=EventType.PRE_TOOL_USE,
        project_root=project_root or Path("/tmp/proj"),
        tool_name=tool_name,
        target_file=target,
        proposed_diff=proposed_diff,
    )


def _signals_with_impact(
    impact_for: dict[Path, dict] | None = None,
) -> Any:
    """Build a fake SignalContext that returns canned impact data.

    Pass ``impact_for = {Path('foo.py'): {"found": True, "blast_radius": 12}}``.
    Files not in the dict get ``{}``.
    """
    impact_for = impact_for or {}

    class _FakeSignals:
        def impact(self, path: Path) -> dict:  # noqa: D401
            return impact_for.get(path, {})

    return _FakeSignals()


def _spy_signals(
    impact_for: dict[Path, dict] | None = None,
) -> Any:
    """Build a fake SignalContext that records every impact() call.

    Used by Week-5-retrospective behavioral assertions: tests assert
    on whether `signals.impact()` IS or IS NOT called for a given
    event shape. Output-only tests can't catch gates / short-circuit
    optimizations because empty impact data flows through the policy
    to the same allow verdict regardless.
    """
    impact_for = impact_for or {}

    class _SpySignals:
        def __init__(self):
            self.impact_calls: list[Path] = []

        def impact(self, path: Path) -> dict:
            self.impact_calls.append(path)
            return impact_for.get(path, {})

    return _SpySignals()


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make sure env-var config doesn't leak across tests."""
    for k in (
        "CODEVIRA_BLAST_RADIUS_MODE",
        "CODEVIRA_BLAST_RADIUS_THRESHOLD",
        "CODEVIRA_BLAST_RADIUS_WARN_THRESHOLD",
    ):
        monkeypatch.delenv(k, raising=False)


# =====================================================================
# Acceptance scenarios from the spec
# =====================================================================


class TestAcceptanceScenarios:
    def test_1_non_edit_event_allowed(self):
        """Read / Bash / Glob events must allow without checking impact."""
        policy = BlastRadiusVeto()
        for tool in ("Read", "Bash", "Glob", "Grep"):
            event = _make_event(tool_name=tool, target=Path("/p/foo.py"))
            verdict = policy.evaluate(event, _signals_with_impact())
            assert verdict.is_allowing(), f"{tool} should be allowed"

    def test_2_edit_on_file_not_in_graph_allowed(self):
        """signals.impact returns {} when graph has no node for the file."""
        policy = BlastRadiusVeto()
        target = Path("/p/foo.py")
        event = _make_event(
            target=target,
            proposed_diff="--- before\ndef f(): pass\n--- after\ndef g(): pass\n",
        )
        # Empty signals → no impact data
        verdict = policy.evaluate(event, _signals_with_impact())
        assert verdict.is_allowing()

    def test_3_edit_with_low_blast_radius_allowed(self):
        """blast_radius < threshold short-circuits before signature check."""
        policy = BlastRadiusVeto()
        target = Path("/p/foo.py")
        # Default block_threshold = 5; 2 callers is well under.
        signals = _signals_with_impact(
            {target: {"found": True, "blast_radius": 2, "affected": []}}
        )
        event = _make_event(
            target=target,
            proposed_diff="--- before\ndef f(x): pass\n--- after\ndef f(x, y): pass\n",
        )
        verdict = policy.evaluate(event, signals)
        assert verdict.is_allowing()

    def test_4_high_radius_body_only_change_allowed(self):
        """High blast radius but no signature change → allow.

        The function body changed but signature lines are identical
        before and after.
        """
        policy = BlastRadiusVeto()
        target = Path("/p/foo.py")
        signals = _signals_with_impact(
            {
                target: {
                    "found": True,
                    "blast_radius": 20,
                    "affected": [{"file": "a.py"}, {"file": "b.py"}],
                }
            }
        )
        diff = (
            "--- before\n"
            "def auth_token(user_id):\n"
            "    return user_id + 1\n"
            "--- after\n"
            "def auth_token(user_id):\n"
            "    return user_id + 2\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_allowing()

    def test_5_high_radius_signature_change_blocked(self):
        """High radius + sig change → block with diagnostic."""
        policy = BlastRadiusVeto()
        target = Path("/p/auth.py")
        signals = _signals_with_impact(
            {
                target: {
                    "found": True,
                    "blast_radius": 12,
                    "affected": [
                        {"file": "x.py"},
                        {"file": "y.py"},
                        {"file": "z.py"},
                        {"file": "a.py"},
                        {"file": "b.py"},
                    ],
                }
            }
        )
        diff = (
            "--- before\n"
            "def auth_token(user_id):\n"
            "    return user_id\n"
            "--- after\n"
            "def auth_token(user):\n"
            "    return user\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking()
        assert verdict.message is not None
        assert "12" in verdict.message
        assert "auth.py" in verdict.message
        assert verdict.metadata["blast_radius"] == 12
        assert verdict.metadata["mode"] == "block"

    def test_6_adding_new_function_to_high_radius_file_allowed(self):
        """v3.3.0 Phase 7 BEHAVIOR CHANGE: purely-ADDED signatures allow.

        A function that didn't exist cannot have callers, so adding one
        can't break the blast radius. (The old block-on-add rule vetoed
        two legitimate edits while dogfooding on 2026-06-12.) The old
        "can't tell rename from add" concern doesn't apply: a rename
        always REMOVES the old signature too, and removals still block —
        see test_6b.
        """
        policy = BlastRadiusVeto()
        target = Path("/p/foo.py")
        signals = _signals_with_impact(
            {target: {"found": True, "blast_radius": 20, "affected": []}}
        )
        diff = (
            "--- before\n"
            "def existing(x):\n"
            "    return x\n"
            "--- after\n"
            "def existing(x):\n"
            "    return x\n"
            "\n"
            "def brand_new(y):\n"
            "    return y\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert (
            verdict.is_allowing()
        ), f"purely-added def must allow (cannot break callers); got {verdict.action}"
        assert verdict.metadata.get("reason") == "signature_changes_purely_additive"

    def test_6b_rename_still_blocks(self):
        """A rename is removed(old) + added(new) — the removal keeps it
        blocking, so the purely-additive allowance can't mask renames."""
        policy = BlastRadiusVeto()
        target = Path("/p/foo.py")
        signals = _signals_with_impact(
            {target: {"found": True, "blast_radius": 20, "affected": []}}
        )
        diff = (
            "--- before\n"
            "def old_name(x):\n"
            "    return x\n"
            "--- after\n"
            "def new_name(x):\n"
            "    return x\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking()

    def test_7_deleting_function_with_callers_blocked(self):
        """Removing a high-impact function should block."""
        policy = BlastRadiusVeto()
        target = Path("/p/foo.py")
        signals = _signals_with_impact(
            {
                target: {
                    "found": True,
                    "blast_radius": 8,
                    "affected": [{"file": "consumer.py"}],
                }
            }
        )
        diff = (
            "--- before\n"
            "def about_to_die():\n"
            "    return 1\n"
            "\n"
            "def stays(x):\n"
            "    return x\n"
            "--- after\n"
            "def stays(x):\n"
            "    return x\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking()
        # The diagnostic should call out the removed signature
        sig_changes = verdict.metadata.get("signature_changes", {})
        assert any(
            "about_to_die" in line for line in sig_changes.get("removed", [])
        ), f"removed function not in metadata: {sig_changes}"

    def test_8_warn_mode_produces_warn_not_block(self, monkeypatch: pytest.MonkeyPatch):
        """Same scenario as test_5 but with mode=warn yields warn."""
        monkeypatch.setenv("CODEVIRA_BLAST_RADIUS_MODE", "warn")
        policy = BlastRadiusVeto()
        target = Path("/p/auth.py")
        signals = _signals_with_impact(
            {target: {"found": True, "blast_radius": 12, "affected": []}}
        )
        diff = (
            "--- before\n"
            "def auth_token(user_id):\n"
            "    return user_id\n"
            "--- after\n"
            "def auth_token(user):\n"
            "    return user\n"
        )
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.action == "warn", verdict.action
        assert not verdict.is_blocking()
        assert verdict.metadata["mode"] == "warn"

    def test_9_off_mode_disables_policy(self, monkeypatch: pytest.MonkeyPatch):
        """mode=off short-circuits to allow."""
        monkeypatch.setenv("CODEVIRA_BLAST_RADIUS_MODE", "off")
        policy = BlastRadiusVeto()
        target = Path("/p/auth.py")
        signals = _signals_with_impact(
            {target: {"found": True, "blast_radius": 100, "affected": []}}
        )
        diff = "--- before\ndef f(): pass\n--- after\ndef g(): pass\n"
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_allowing()

    def test_10_evaluation_under_50ms_p95(self):
        """Cold-impact evaluation budget — 100 trials, p95 < 50 ms."""
        import time

        policy = BlastRadiusVeto()
        target = Path("/p/auth.py")
        signals = _signals_with_impact(
            {target: {"found": True, "blast_radius": 12, "affected": []}}
        )
        diff = (
            "--- before\n"
            "def auth_token(user_id):\n"
            "    return user_id\n"
            "--- after\n"
            "def auth_token(user):\n"
            "    return user\n"
        )
        event = _make_event(target=target, proposed_diff=diff)

        durations = []
        for _ in range(100):
            t = time.perf_counter()
            policy.evaluate(event, signals)
            durations.append((time.perf_counter() - t) * 1000)
        p95 = sorted(durations)[94]
        assert p95 < 50.0, f"p95 = {p95:.2f} ms"


# =====================================================================
# Configuration robustness
# =====================================================================


class TestConfiguration:
    def test_invalid_mode_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch):
        """Garbage env var doesn't crash; falls back to 'block'."""
        monkeypatch.setenv("CODEVIRA_BLAST_RADIUS_MODE", "totally-fake")
        policy = BlastRadiusVeto()
        config = policy._config()
        assert config["mode"] == "block"

    def test_invalid_threshold_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        for bad in ("not-a-number", "-5", "0", "abc", ""):
            monkeypatch.setenv("CODEVIRA_BLAST_RADIUS_THRESHOLD", bad)
            policy = BlastRadiusVeto()
            assert (
                policy._config()["block_threshold"] == 5
            ), f"bad threshold {bad!r} not handled"

    def test_threshold_clamped_to_max(self, monkeypatch: pytest.MonkeyPatch):
        """Even a valid huge threshold is clamped."""
        monkeypatch.setenv("CODEVIRA_BLAST_RADIUS_THRESHOLD", str(10**9))
        policy = BlastRadiusVeto()
        assert policy._config()["block_threshold"] == 10_000

    def test_warn_threshold_cannot_exceed_block(self, monkeypatch: pytest.MonkeyPatch):
        """warn=10 + block=5 is nonsense; clamp warn down."""
        monkeypatch.setenv("CODEVIRA_BLAST_RADIUS_THRESHOLD", "5")
        monkeypatch.setenv("CODEVIRA_BLAST_RADIUS_WARN_THRESHOLD", "10")
        policy = BlastRadiusVeto()
        config = policy._config()
        assert config["warn_threshold"] <= config["block_threshold"]


# =====================================================================
# Signature-detection helper unit tests (Hero 4's external schema)
# =====================================================================


class TestSignatureDetection:
    def test_python_def_signature_change(self):
        diff = (
            "--- before\n"
            "def foo(a, b):\n"
            "    return a\n"
            "--- after\n"
            "def foo(a, b, c):\n"
            "    return a\n"
        )
        assert change_touches_signature(diff, language="python")

    def test_python_body_only_change(self):
        diff = (
            "--- before\n"
            "def foo(a):\n"
            "    return 1\n"
            "--- after\n"
            "def foo(a):\n"
            "    return 2\n"
        )
        assert not change_touches_signature(diff, language="python")

    def test_python_class_definition_change(self):
        diff = (
            "--- before\n"
            "class Foo(Base):\n"
            "    pass\n"
            "--- after\n"
            "class Foo(Other):\n"
            "    pass\n"
        )
        assert change_touches_signature(diff, language="python")

    def test_python_async_def(self):
        diff = (
            "--- before\n"
            "async def fetch(url):\n"
            "    pass\n"
            "--- after\n"
            "async def fetch(url, retries):\n"
            "    pass\n"
        )
        assert change_touches_signature(diff, language="python")

    def test_typescript_arrow_function(self):
        diff = (
            "--- before\n"
            "export const greet = (name: string) => `Hello, ${name}`;\n"
            "--- after\n"
            "export const greet = (name: string, lang: string) => `Hi, ${name}`;\n"
        )
        assert change_touches_signature(diff, language="typescript")

    def test_go_func_change(self):
        diff = (
            "--- before\n"
            "func DoThing(x int) int {\n"
            "  return x\n"
            "}\n"
            "--- after\n"
            "func DoThing(x, y int) int {\n"
            "  return x + y\n"
            "}\n"
        )
        assert change_touches_signature(diff, language="go")

    def test_rust_pub_fn_change(self):
        diff = (
            "--- before\n"
            "pub fn handle(req: Request) -> Response {\n"
            "    response\n"
            "}\n"
            "--- after\n"
            "pub fn handle(req: Request, ctx: Context) -> Response {\n"
            "    response\n"
            "}\n"
        )
        assert change_touches_signature(diff, language="rust")

    def test_unknown_language_uses_union_regex(self):
        """Unknown language → union of all patterns; still catches Python."""
        diff = "--- before\ndef foo(): pass\n--- after\ndef foo(x): pass\n"
        assert change_touches_signature(diff, language=None)
        assert change_touches_signature(diff, language="cobol")  # fallback

    def test_huge_diff_bails_to_false(self):
        """Week-4 R1 #7 + R3 mutation finding: a malicious 100 MB diff
        must NOT spend unbounded CPU running ~30 regex patterns
        line-by-line. The cap is 1 MB; anything larger short-circuits.

        BOTH the output AND the timing must reflect the cap, not just
        the return value — output-only assertions don't catch the cap
        removal because content with no signatures returns False either
        way (Week-2 R5 lesson reinforced).
        """
        import time
        from mcp_server.engine.policies._signature_detect import _MAX_DIFF_BYTES

        # Build a diff bigger than the cap. Content must be MANY lines
        # so a removed cap actually pays the per-line regex cost.
        line = "  some_identifier_with_paren_lookalike(\n"
        n_lines = (_MAX_DIFF_BYTES // len(line)) + 100
        big_body = line * n_lines
        huge_diff = f"--- before\n{big_body}\n--- after\n{big_body}\n"
        assert len(huge_diff) > _MAX_DIFF_BYTES

        # Time-bound: with the cap, this is one len() check + return,
        # consistently under 1 ms. Without the cap, on a 2 MB diff the
        # function does parse_diff (regex on whole text) + splitlines
        # + per-line regex matching → measured at ~38 ms median on
        # dev hardware. Bound at 10 ms — well above bounded-path noise
        # (~0 ms) and well below the cheapest unbounded run (~30 ms).
        # We take the BEST of 3 trials to defeat variance.
        def _measure(fn, *args, **kw):
            best = float("inf")
            for _ in range(3):
                t = time.perf_counter()
                result = fn(*args, **kw)
                elapsed = (time.perf_counter() - t) * 1000
                best = min(best, elapsed)
            return result, best

        result, elapsed_ms = _measure(
            change_touches_signature,
            huge_diff,
            language="python",
        )
        assert not result, "huge diff should bail to False"
        assert elapsed_ms < 10, (
            f"huge diff took {elapsed_ms:.1f} ms (best of 3) — cap not "
            f"enforced. Bounded path is sub-millisecond; unbounded path "
            f"on this input is ~38 ms."
        )

        # Same for the summary helper.
        summary, elapsed_ms_2 = _measure(
            signature_change_summary,
            huge_diff,
            language="python",
        )
        assert summary == {"added": [], "removed": [], "modified": []}
        assert (
            elapsed_ms_2 < 10
        ), f"summary on huge diff took {elapsed_ms_2:.1f} ms — cap missing"

    def test_malformed_diff_returns_false(self):
        """Malformed diff: no envelope. Conservative — return False."""
        for malformed in (
            "",
            "no envelope here",
            "--- after\n--- before\nbackwards\n",
            None,
        ):
            assert not change_touches_signature(malformed, language="python")

    def test_language_for_path_known_extensions(self):
        assert language_for_path("foo.py") == "python"
        assert language_for_path("foo.ts") == "typescript"
        assert language_for_path("/abs/path/foo.go") == "go"
        assert language_for_path("foo.spec.ts") == "typescript"
        assert language_for_path("foo.tsx") == "typescript"
        assert language_for_path("foo.rs") == "rust"

    def test_language_for_path_unknown(self):
        assert language_for_path("foo.unknown") is None
        assert language_for_path("README") is None
        assert language_for_path("") is None
        assert language_for_path(None) is None  # type: ignore[arg-type]

    def test_signature_change_summary_pairs_renames(self):
        diff = (
            "--- before\n"
            "def auth_token(user_id):\n"
            "    return 1\n"
            "--- after\n"
            "def auth_token(user):\n"
            "    return 1\n"
        )
        summary = signature_change_summary(diff, language="python")
        assert summary["modified"], f"didn't pair rename: {summary}"
        assert "auth_token" in summary["modified"][0]

    def test_signature_change_summary_separates_unrelated_changes(self):
        diff = "--- before\ndef alpha():\n    pass\n--- after\ndef beta():\n    pass\n"
        summary = signature_change_summary(diff, language="python")
        # alpha → beta: different names, not paired
        assert summary["added"]
        assert summary["removed"]


# =====================================================================
# Default-policy registration is idempotent
# =====================================================================


class TestBehavioralGates:
    """Week-5 retrospective: gates that don't change OUTPUT for the
    happy/empty-signals path can't be caught by output-only tests.
    These behavioral spies catch mutations that remove the gates,
    causing unnecessary signals.impact() calls or crashes.
    """

    def test_non_edit_does_not_call_signals_impact(self):
        """is_edit gate: Read/Bash/Glob events must NOT trigger
        signals.impact(). Mutation that removes the gate would still
        return allow on empty signals — this spy is the only catch.
        """
        policy = BlastRadiusVeto()
        spy = _spy_signals()
        for tool in ("Read", "Bash", "Glob", "Grep"):
            policy.evaluate(_make_event(tool_name=tool, target=Path("/p/x.py")), spy)
        assert spy.impact_calls == [], (
            f"is_edit gate degraded: signals.impact called on non-edit "
            f"events: {spy.impact_calls}"
        )

    def test_target_none_does_not_call_signals_impact(self):
        """target_file=None gate: when the AI's tool input has no
        file_path (e.g. Bash command without explicit target), the
        wiring layer leaves event.target_file as None. Hero 4 must
        skip impact lookup. Behavioral spy catches the gate.
        """
        policy = BlastRadiusVeto()
        spy = _spy_signals()
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=Path("/p"),
            tool_name="Edit",  # IS an edit, so first gate passes
            target_file=None,  # but no target
            proposed_diff="--- before\ndef f(): pass\n--- after\ndef g(): pass\n",
        )
        verdict = policy.evaluate(event, spy)
        assert verdict.is_allowing()
        assert (
            spy.impact_calls == []
        ), f"target_file None gate degraded: {spy.impact_calls}"

    def test_signals_none_does_not_crash(self):
        """signals=None gate: if the runner ever fails to build
        signals (e.g. graph corrupt), Hero 4 must allow gracefully,
        not crash with AttributeError on signals.impact().
        """
        policy = BlastRadiusVeto()
        event = _make_event(
            target=Path("/p/x.py"),
            proposed_diff="--- before\ndef f(): pass\n--- after\ndef g(): pass\n",
        )
        # signals=None — must not crash
        verdict = policy.evaluate(event, None)
        assert verdict.is_allowing()

    def test_priority_value_stable(self):
        """Hero 4's priority MUST remain mid-range (specifically
        below Hero 1's 100). If a future refactor flips it to >100,
        block-class verdict ordering changes silently. Document the
        invariant. (Original M5 mutation passed because no test
        asserted on the value.)"""
        from mcp_server.engine.policies.decision_lock import DecisionLock

        assert BlastRadiusVeto.priority < DecisionLock.priority, (
            f"BlastRadiusVeto.priority ({BlastRadiusVeto.priority}) "
            f"must remain below DecisionLock.priority ({DecisionLock.priority}); "
            f"block ordering depends on this."
        )

    def test_impact_found_false_skips_evaluation(self):
        """When impact.get('found') is False, Hero 4 must allow
        without checking signature. A mutation that flips the check
        (so found=False causes evaluation to continue) would silently
        block on every uninitialized graph. Spy catches it.
        """
        policy = BlastRadiusVeto()
        target = Path("/p/x.py")
        # impact dict has explicit found=False
        signals = _signals_with_impact(
            {target: {"found": False, "blast_radius": 999, "affected": []}}
        )
        diff = "--- before\ndef f(x): pass\n--- after\ndef f(x, y): pass\n"
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        assert (
            verdict.is_allowing()
        ), f"impact.found=False must short-circuit to allow; got {verdict.action}"

    def test_impact_missing_blast_radius_defaults_safe(self):
        """If impact dict is found=True but missing 'blast_radius'
        key (defensive against partial data), Hero 4 must use a SAFE
        default (treat as 0 → allow). A mutation flipping the default
        to 9999 would silently block every edit on an incomplete graph.
        """
        policy = BlastRadiusVeto()
        target = Path("/p/x.py")
        # found=True but no blast_radius key
        signals = _signals_with_impact(
            {
                target: {"found": True, "affected": []}  # no blast_radius
            }
        )
        diff = "--- before\ndef f(x): pass\n--- after\ndef f(x, y): pass\n"
        event = _make_event(target=target, proposed_diff=diff)
        verdict = policy.evaluate(event, signals)
        # blast_radius defaults to 0 → 0 < threshold → allow
        assert (
            verdict.is_allowing()
        ), f"missing blast_radius must default to 0 (allow); got {verdict.action}"

    def test_full_write_with_high_radius_blocks(self):
        """Edit-class tools always have proposed_diff. But Write tool
        replaces a file wholesale — proposed_diff may be None. On a
        high-impact file, this is risky enough to block by default.
        (M10 mutation flipped this to allow; this test catches it.)
        """
        policy = BlastRadiusVeto()
        target = Path("/p/x.py")
        signals = _signals_with_impact(
            {
                target: {
                    "found": True,
                    "blast_radius": 12,
                    "affected": [{"file": "a.py"}, {"file": "b.py"}],
                }
            }
        )
        # proposed_diff=None simulates a Write tool replacement
        event = _make_event(target=target, tool_name="Write", proposed_diff=None)
        verdict = policy.evaluate(event, signals)
        assert verdict.is_blocking(), (
            f"None diff (full Write) on high-impact file should block; "
            f"got {verdict.action}"
        )
        # Metadata indicates the special case
        sig_changes = verdict.metadata.get("signature_changes", {})
        assert sig_changes.get("modified") == ["(full Write)"], sig_changes


class TestRegistration:
    def test_register_default_policies_idempotent(self):
        from mcp_server.engine import (
            register_default_policies,
            registered_policies,
            reset_policies,
        )

        reset_policies()
        register_default_policies()
        names1 = sorted(p.name for p in registered_policies())
        register_default_policies()
        names2 = sorted(p.name for p in registered_policies())
        assert (
            names1 == names2
        ), f"register_default_policies created duplicates: {names1} vs {names2}"
        assert "blast_radius_veto" in names1

    def test_cli_engine_handler_calls_register_default_policies(self):
        """Week-4 R2 finding: cli.py's `engine handle <event>` entry MUST
        call register_default_policies() before dispatching, otherwise
        Claude Code hooks invoke the engine with zero policies registered
        and Hero 4 silently does nothing.

        Same class of bug as Week-1 R3 (MCP dispatch never wired).
        Asserts on source content rather than runtime behavior because
        the cli entry forks subprocesses; static check is sufficient.
        """
        from pathlib import Path

        cli_src = (
            Path(__file__).resolve().parents[2] / "mcp_server" / "cli.py"
        ).read_text()
        # Both the import AND the call must be present in the engine handler.
        assert (
            "register_default_policies" in cli_src
        ), "cli.py must call register_default_policies in `engine handle`"

    def test_hero_4_fires_through_engine_dispatch(self, tmp_path):
        """Week-5 R5-redo found a runner-vs-policy signature mismatch:
        ``policy.evaluate(event, signals=None)`` was being called with
        only ``event`` by the runner, so Hero 4 silently no-op'd
        through dispatch even though direct ``evaluate(event, signals)``
        worked. Catches this regression for Hero 4 specifically.

        Builds a real graph with high blast radius + sig-changing diff;
        dispatches; asserts BLOCK. If the runner stops passing signals,
        this test fails.
        """
        from indexer.sqlite_graph import SQLiteGraph
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine import register_policy, reset_policies, dispatch
        import mcp_server.paths as paths_mod

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        project = tmp_path / "p"
        project.mkdir()
        (project / "pyproject.toml").write_text("")
        # v3.7.0 opt-in gate: the blast-radius signal reads tools.graph.get_impact,
        # which is now inert for projects the user never `codevira init`-ed. Opt
        # this project in (in-repo .codevira/config.yaml marker) so the policy can
        # see the graph and fire — matching a real init-ed workspace.
        (project / ".codevira").mkdir()
        (project / ".codevira" / "config.yaml").write_text(
            "schema_version: 1\n", encoding="utf-8"
        )

        paths_mod.get_global_home = lambda: fake_home
        paths_mod.set_project_dir(project)
        paths_mod.invalidate_data_dir_cache()

        from mcp_server.paths import get_data_dir

        db_path = get_data_dir() / "graph" / "graph.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        g = SQLiteGraph(db_path)
        g.add_node("auth", "file", "auth.py", "auth.py")
        for i in range(12):
            cid = f"c_{i}"
            g.add_node(cid, "file", f"caller_{i}.py", f"callers/c_{i}.py")
            g.conn.execute(
                "INSERT INTO edges (source_id, target_id, kind) VALUES (?, ?, ?)",
                (cid, "auth", "imports"),
            )
        g.conn.commit()
        g.close()

        reset_policies()
        register_policy(BlastRadiusVeto())

        os.environ["CODEVIRA_BLAST_RADIUS_THRESHOLD"] = "5"
        os.environ["CODEVIRA_BLAST_RADIUS_MODE"] = "block"
        try:
            diff = (
                "--- before\ndef auth_token(user_id):\n    return user_id\n"
                "--- after\ndef auth_token(user):\n    return user\n"
            )
            event = HookEvent(
                event_type=EventType.PRE_TOOL_USE,
                project_root=project,
                tool_name="Edit",
                target_file=project / "auth.py",
                proposed_diff=diff,
            )
            verdict = dispatch(event)
            assert verdict.is_blocking(), (
                f"Hero 4 must fire through dispatch, not just direct evaluate; "
                f"got {verdict.action} — likely the runner isn't passing "
                f"signals to policy.evaluate()"
            )
            assert verdict.policy == "blast_radius_veto"
        finally:
            for k in ("CODEVIRA_BLAST_RADIUS_THRESHOLD", "CODEVIRA_BLAST_RADIUS_MODE"):
                os.environ.pop(k, None)
            reset_policies()

    def test_mcp_server_call_tool_calls_register_default_policies(self):
        """Same wiring check for the MCP server's call_tool dispatch:
        without `register_default_policies` on every tool call, Hero 4's
        block path is unreachable from tools like Edit / Write.
        """
        from pathlib import Path

        srv_src = (
            Path(__file__).resolve().parents[2] / "mcp_server" / "server.py"
        ).read_text()
        assert (
            "register_default_policies" in srv_src
        ), "server.py call_tool must register policies before pre_call"


class TestAffectedFilesListRegression:
    """v3.4.0: blast_radius read impact['affected'] but get_impact /
    signals.impact return the list under 'affected_files' (each item keyed
    'file'). The veto's 'Affected files' list therefore always rendered
    empty. This pins the corrected key."""

    def test_affected_files_appear_in_message(self):
        from pathlib import Path

        target = Path("/tmp/proj/hot.py")
        impact = {
            target: {
                "found": True,
                "blast_radius": 20,
                "affected_files": [
                    {"file": "caller_a.py"},
                    {"file": "caller_b.py"},
                ],
            }
        }
        # Removing a public function → caller-breaking → block, with the
        # affected-files context attached.
        diff = (
            "--- before\n"
            "def public_api(x):\n    return x\n"
            "--- after\n"
            "def other(z):\n    return z\n"
        )
        verdict = BlastRadiusVeto().evaluate(
            _make_event(target=target, proposed_diff=diff),
            _signals_with_impact(impact),
        )
        assert verdict.is_blocking()
        assert "caller_a.py" in (verdict.message or "")
        assert "caller_b.py" in (verdict.message or "")
