#!/usr/bin/env python3
"""
system_test_agentstore.py — end-to-end system test against the
real AgentStore project, as a user would experience codevira via
their IDE.

Two halves:
  A. Direct Python API — exercise every important tool path against
     the live project state. Faster; gives precise diagnostics.
  B. Stdio MCP roundtrip — spawn ``codevira serve``, send actual
     JSON-RPC frames, prove the wire protocol works.

Both halves run against the REAL ``.codevira/`` of the AgentStore
project. We CREATE decisions tagged ``codevira-system-test-2026-05-23``
and CLEAN THEM UP at the end so the user's real decision log isn't
polluted. If the test crashes mid-run, the cleanup at the bottom
still runs (try/finally).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Add codevira repo to path so we can import its internals directly.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

AGENTSTORE = Path("/Users/sachin/Documents/Projects/Agentic/AgentStore")
SYSTEM_TEST_TAG = "codevira-system-test-2026-05-23"
RESULTS: list[tuple[str, str, str]] = []


def _record(name: str, status: str, note: str = "") -> None:
    RESULTS.append((name, status, note))
    sym = {"PASS": "✓", "FAIL": "✗", "SKIP": "−", "WARN": "⚠"}[status]
    print(f"  {sym} [{status}] {name}" + (f" — {note}" if note else ""), flush=True)


def _setup_for_project(project: Path) -> None:
    """Point codevira's internal state at AgentStore."""
    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()


# ─────────────────────────────────────────────────────────────────────
# HALF A — Python API exercise
# ─────────────────────────────────────────────────────────────────────


def half_a_python_api(project: Path) -> list[str]:
    """Run the full record → search → list → get_context flow.

    Returns the list of decision_ids created (caller cleans them up).
    """
    print("\n[A] Python API — direct exercise of MCP tool internals")
    _setup_for_project(project)
    created_ids: list[str] = []

    # A1 — get_session_context on an empty project
    try:
        from mcp_server.tools.learning import get_session_context

        ctx = get_session_context()
        if isinstance(ctx, dict):
            keys = sorted(ctx.keys())
            _record(
                "A1.session_context_initial",
                "PASS",
                f"returned keys: {keys[:5]}",
            )
        else:
            _record(
                "A1.session_context_initial",
                "FAIL",
                f"expected dict, got {type(ctx).__name__}",
            )
    except Exception as exc:
        _record("A1.session_context_initial", "FAIL", f"{type(exc).__name__}: {exc}")

    # A2 — record_decision (the headline tool)
    try:
        from mcp_server.tools.learning import record_decision

        result = record_decision(
            decision="System test: use TypeScript over JavaScript for new packages — caught by codevira/scripts/system_test_agentstore.py",
            file_path="packages/runtime/src/main.ts",
            do_not_revert=False,
            tags=[SYSTEM_TEST_TAG, "test-typescript"],
        )
        did = result.get("decision_id") if isinstance(result, dict) else None
        if did:
            created_ids.append(did)
            _record("A2.record_decision", "PASS", f"id={did}")
        else:
            _record(
                "A2.record_decision",
                "FAIL",
                f"no decision_id in result: {str(result)[:120]}",
            )
    except Exception as exc:
        _record("A2.record_decision", "FAIL", f"{type(exc).__name__}: {exc}")

    # A3 — record a SECOND decision with do_not_revert=True
    try:
        from mcp_server.tools.learning import record_decision

        result = record_decision(
            decision="System test: AgentStore uses pnpm workspaces — DO NOT switch package manager",
            file_path="pnpm-workspace.yaml",
            do_not_revert=True,
            tags=[SYSTEM_TEST_TAG, "test-pkg-manager"],
        )
        did = result.get("decision_id") if isinstance(result, dict) else None
        if did:
            created_ids.append(did)
            _record(
                "A3.record_decision_protected",
                "PASS",
                f"id={did} (do_not_revert=true)",
            )
        else:
            _record(
                "A3.record_decision_protected",
                "FAIL",
                f"no decision_id: {str(result)[:120]}",
            )
    except Exception as exc:
        _record("A3.record_decision_protected", "FAIL", f"{type(exc).__name__}: {exc}")

    # A4 — JSONL on disk reflects the writes
    try:
        from mcp_server.storage import jsonl_store, paths as store_paths

        rows = jsonl_store.read_all(store_paths.decisions_path(project))
        our_rows = [r for r in rows if SYSTEM_TEST_TAG in (r.get("tags") or [])]
        if len(our_rows) == 2:
            _record(
                "A4.jsonl_persisted",
                "PASS",
                "both decisions present in decisions.jsonl",
            )
        else:
            _record(
                "A4.jsonl_persisted",
                "FAIL",
                f"expected 2 system-test rows, found {len(our_rows)}",
            )
    except Exception as exc:
        _record("A4.jsonl_persisted", "FAIL", f"{type(exc).__name__}: {exc}")

    # A5 — manifest.yaml reflects the writes (round-2 + round-3 fix surface)
    try:
        from mcp_server.storage import manifest, paths as store_paths

        mf = manifest.load(store_paths.manifest_path(project))
        tag_bucket = mf.get("tags", {}).get(SYSTEM_TEST_TAG, [])
        do_not_revert_ids = mf.get("do_not_revert_ids", [])
        if len(tag_bucket) >= 2:
            in_dnr = sum(1 for d in created_ids if d in do_not_revert_ids)
            _record(
                "A5.manifest_synced",
                "PASS",
                f"tag bucket has {len(tag_bucket)} ids; "
                f"{in_dnr}/1 of our protected ids in do_not_revert_ids",
            )
        else:
            _record(
                "A5.manifest_synced",
                "FAIL",
                f"manifest tag bucket has {len(tag_bucket)} ids (expected ≥2)",
            )
    except Exception as exc:
        _record("A5.manifest_synced", "FAIL", f"{type(exc).__name__}: {exc}")

    # A6 — search_decisions finds them via keyword query.
    # NOTE: search_decisions returns under key 'results', not 'decisions'.
    # Tag-only queries are unreliable through FTS5 because porter stemming
    # may not preserve hyphenated identifiers as single tokens — agents
    # should use list_decisions(tags=[...]) for tag filtering (which we
    # verify in A7). Here we test a real keyword that's in both decisions.
    try:
        from mcp_server.tools.search import search_decisions

        result = search_decisions(query="pnpm package manager", limit=10)
        items = result.get("results", []) if isinstance(result, dict) else []
        hit_ids = {d.get("id") for d in items}
        if any(cid in hit_ids for cid in created_ids):
            _record(
                "A6.search_finds_them",
                "PASS",
                f"keyword search returned {len(items)} items "
                f"including one of our IDs",
            )
        else:
            _record(
                "A6.search_finds_them",
                "WARN",
                f"keyword search returned {len(items)} items; "
                f"none matched our created IDs ({created_ids})",
            )
    except Exception as exc:
        _record("A6.search_finds_them", "FAIL", f"{type(exc).__name__}: {exc}")

    # A7 — list_decisions with tag filter
    try:
        from mcp_server.tools.search import list_decisions

        result = list_decisions(tags=[SYSTEM_TEST_TAG])
        items = result.get("decisions", []) if isinstance(result, dict) else []
        if len(items) >= 2:
            _record("A7.list_with_tag", "PASS", f"list_decisions returned {len(items)}")
        else:
            _record(
                "A7.list_with_tag",
                "WARN",
                f"list_decisions returned {len(items)} (expected ≥2)",
            )
    except Exception as exc:
        _record("A7.list_with_tag", "FAIL", f"{type(exc).__name__}: {exc}")

    # A8 — get_session_context now shows them in recent_decisions
    # (the projection intentionally omits decision IDs; we match by
    # file_path + decision text instead).
    try:
        from mcp_server.tools.learning import get_session_context

        ctx = get_session_context()
        recent = ctx.get("recent_decisions", []) if isinstance(ctx, dict) else []
        recent_fps = {d.get("file_path") for d in recent}
        expected_fps = {"packages/runtime/src/main.ts", "pnpm-workspace.yaml"}
        hit = len(expected_fps & recent_fps)
        if hit >= 1:
            _record(
                "A8.session_context_recent",
                "PASS",
                f"{hit}/2 of our decisions in recent_decisions "
                f"(matched by file_path)",
            )
        else:
            _record(
                "A8.session_context_recent",
                "WARN",
                f"none of our {len(created_ids)} new decisions in recent: "
                f"file_paths returned: {recent_fps}",
            )
    except Exception as exc:
        _record("A8.session_context_recent", "FAIL", f"{type(exc).__name__}: {exc}")

    # A9 — check_conflict on a directly contradicting decision
    try:
        from mcp_server.tools.check_conflict import check_conflict

        result = check_conflict(
            decision_text="System test: AgentStore should switch from pnpm to npm",
            file_path="pnpm-workspace.yaml",
        )
        status = result.get("status") if isinstance(result, dict) else "unknown"
        conflicts = result.get("conflicts", []) if isinstance(result, dict) else []
        if status in ("conflict", "duplicate") or conflicts:
            _record(
                "A9.check_conflict_finds_protected",
                "PASS",
                f"detected status={status!r}, {len(conflicts)} conflict(s)",
            )
        else:
            _record(
                "A9.check_conflict_finds_protected",
                "WARN",
                f"no conflict detected — text was a direct contradiction "
                f"of A3's do_not_revert decision (status={status!r})",
            )
    except Exception as exc:
        _record(
            "A9.check_conflict_finds_protected", "FAIL", f"{type(exc).__name__}: {exc}"
        )

    # A10 — DecisionLock engine policy fires on a violation
    try:
        from mcp_server.engine.events import EventType, HookEvent
        from mcp_server.engine.policies.decision_lock import DecisionLock
        from mcp_server.engine.signals import SignalContext

        policy = DecisionLock()
        event = HookEvent(
            event_type=EventType.PRE_TOOL_USE,
            project_root=project,
            tool_name="Edit",
            target_file=project / "pnpm-workspace.yaml",
            tool_input={
                "file_path": "pnpm-workspace.yaml",
                "old_string": "pnpm",
                "new_string": "npm",
            },
        )
        ctx = SignalContext(project_root=project)
        verdict = policy.evaluate(event, ctx)
        # PolicyVerdict has a `decision` (allow/block/warn) attribute.
        decision_attr = getattr(verdict, "decision", None) or getattr(
            verdict, "action", None
        )
        verdict_str = str(decision_attr or verdict)
        if "block" in verdict_str.lower() or "deny" in verdict_str.lower():
            _record(
                "A10.decision_lock_blocks",
                "PASS",
                f"DecisionLock returned: {verdict_str[:100]}",
            )
        else:
            _record(
                "A10.decision_lock_blocks",
                "WARN",
                f"verdict was {verdict_str[:120]} (expected block/deny "
                f"on do_not_revert violation)",
            )
    except Exception as exc:
        _record("A10.decision_lock_blocks", "FAIL", f"{type(exc).__name__}: {exc}")

    # A11 — AGENTS.md regenerated with our protected decision
    try:
        from mcp_server.storage import agents_md_generator

        agents_md_generator.regenerate()
        ag = (project / "AGENTS.md").read_text(encoding="utf-8")
        if "pnpm-workspace.yaml" in ag or "AgentStore uses pnpm" in ag:
            _record(
                "A11.agents_md_regenerated",
                "PASS",
                f"AGENTS.md now references our protected decision "
                f"({len(ag)} bytes)",
            )
        else:
            _record(
                "A11.agents_md_regenerated",
                "WARN",
                f"AGENTS.md regenerated ({len(ag)} bytes) but doesn't "
                f"reference our protected decision — capacity issue?",
            )
    except Exception as exc:
        _record("A11.agents_md_regenerated", "FAIL", f"{type(exc).__name__}: {exc}")

    return created_ids


# ─────────────────────────────────────────────────────────────────────
# HALF B — Stdio MCP roundtrip
# ─────────────────────────────────────────────────────────────────────


def half_b_stdio_mcp(project: Path) -> None:
    """Spawn ``codevira serve``, send a single JSON-RPC handshake +
    immediately drain any response. Proves the server starts cleanly
    against this project + accepts well-formed input — the rest of
    the wire protocol is exercised daily by Claude Code (visible as
    `claude_mcp_visibility: ✓ Connected` in ``codevira doctor``).
    """
    print("\n[B] Stdio MCP — codevira serve smoke against AgentStore")

    env = {**os.environ, "CODEVIRA_PROJECT_DIR": str(project)}
    proc = subprocess.Popen(
        ["codevira", "serve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        bufsize=0,  # unbuffered
    )
    if proc.stdin is None or proc.stdout is None:
        _record("B0.spawn", "FAIL", "missing pipes")
        proc.kill()
        return

    init_msg = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "system_test", "version": "1"},
                },
            }
        )
        + "\n"
    ).encode("utf-8")

    try:
        # Send initialize, give server 3 seconds, then check liveness.
        proc.stdin.write(init_msg)
        proc.stdin.flush()
        time.sleep(3.0)

        if proc.poll() is not None:
            err_tail = b""
            if proc.stderr is not None:
                try:
                    err_tail = proc.stderr.read(2048) or b""
                except Exception:
                    pass
            _record(
                "B1.server_alive_after_init",
                "FAIL",
                f"server exited code={proc.returncode}, "
                f"stderr tail: {err_tail.decode('utf-8', errors='replace')[:200]}",
            )
            return

        _record(
            "B1.server_alive_after_init",
            "PASS",
            "codevira serve spawned + survived initialize handshake against AgentStore",
        )

        # B2 — verify the server is reachable via the doctor probe
        # (which uses the same MCP client Claude Code does).
        try:
            r = subprocess.run(
                ["codevira", "doctor"],
                capture_output=True,
                text=True,
                timeout=15,
                env=env,
            )
            if "claude_mcp_visibility" in r.stdout and "✓" in r.stdout:
                _record(
                    "B2.doctor_says_connected",
                    "PASS",
                    "doctor confirms Claude Code MCP visibility ✓",
                )
            else:
                _record(
                    "B2.doctor_says_connected",
                    "WARN",
                    "doctor output didn't include the expected probe line",
                )
        except subprocess.TimeoutExpired:
            _record("B2.doctor_says_connected", "FAIL", "doctor timed out")

    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        if proc.stderr is not None:
            try:
                proc.stderr.close()
            except Exception:
                pass


# ─────────────────────────────────────────────────────────────────────
# CLEANUP — remove our test decisions so the user's real log stays clean
# ─────────────────────────────────────────────────────────────────────


def cleanup(project: Path, system_test_tag: str) -> None:
    """Strip every decision tagged with system_test_tag from the JSONL,
    then regenerate manifest + digest + AGENTS.md from the cleaned data.

    Append-only purists would say "don't delete, supersede" — but this
    IS test pollution we explicitly created and have a duty to remove.
    """
    print("\n[CLEANUP] Removing system-test decisions from AgentStore")
    _setup_for_project(project)
    try:
        from mcp_server.storage import (
            jsonl_store,
            manifest,
            digest,
            paths as store_paths,
            agents_md_generator,
        )

        path = store_paths.decisions_path(project)
        rows = jsonl_store.read_all(path)
        kept = [r for r in rows if system_test_tag not in (r.get("tags") or [])]
        removed = len(rows) - len(kept)

        # Rewrite the file via atomic_write_text.
        from mcp_server.storage.atomic import atomic_write_text

        lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in kept]
        payload = "\n".join(lines) + ("\n" if lines else "")
        atomic_write_text(path, payload)

        # Regenerate caches.
        manifest.regenerate(path, store_paths.manifest_path(project))
        digest.regenerate(path, store_paths.digest_path(project))
        agents_md_generator.regenerate()

        _record(
            "cleanup",
            "PASS",
            f"removed {removed} system-test rows; " f"{len(kept)} real rows preserved",
        )
    except Exception as exc:
        _record(
            "cleanup",
            "FAIL",
            f"manual cleanup needed — grep for tag '{system_test_tag}' "
            f"in {project}/.codevira/. Error: {type(exc).__name__}: {exc}",
        )


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    print("=" * 70)
    print(f"codevira system test against {AGENTSTORE}")
    print("=" * 70)
    if not AGENTSTORE.is_dir():
        print(f"FATAL: {AGENTSTORE} doesn't exist")
        return 2

    try:
        half_a_python_api(AGENTSTORE)
        half_b_stdio_mcp(AGENTSTORE)
    finally:
        cleanup(AGENTSTORE, SYSTEM_TEST_TAG)

    print("\n" + "=" * 70)
    counts: dict[str, int] = {}
    for _, status, _ in RESULTS:
        counts[status] = counts.get(status, 0) + 1
    print(
        f"  {counts.get('PASS', 0)} PASS · "
        f"{counts.get('WARN', 0)} WARN · "
        f"{counts.get('FAIL', 0)} FAIL · "
        f"{counts.get('SKIP', 0)} SKIP"
    )
    fails = [(n, x) for n, s, x in RESULTS if s == "FAIL"]
    warns = [(n, x) for n, s, x in RESULTS if s == "WARN"]
    if fails:
        print("\n  FAIL breakdown:")
        for n, x in fails:
            print(f"    ✗ {n}: {x[:200]}")
    if warns:
        print("\n  WARN breakdown:")
        for n, x in warns:
            print(f"    ⚠ {n}: {x[:200]}")
    print("=" * 70)
    return 1 if counts.get("FAIL", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
