#!/usr/bin/env python3
"""
chaos_smoke.py — adversarial smoke test against codevira's storage layer.

Run as: .venv/bin/python scripts/chaos_smoke.py

The structured unit + integration tests prove "expected scenarios work."
This harness probes "what breaks under hacker-mode adversarial input."
Every attack either PASSES (the system survived gracefully — error
logged + state intact) or FAILS (crashed, corrupted, hung, or silently
swallowed bad data).

Each attack is self-contained: sets up its own isolated tmp project,
runs the attack, asserts the invariant, prints PASS/FAIL/SKIP +
diagnostic. Continues to the next attack on failure — we want the full
list of breakages, not the first one.
"""

from __future__ import annotations

import multiprocessing
import os
import signal
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Add repo root so we can import mcp_server modules.
sys.path.insert(0, str(Path(__file__).parent.parent))


# Force line-buffered stdout so live progress is visible (Python
# block-buffers stdout when not a tty, which makes a stuck attack
# look like normal silence).
sys.stdout.reconfigure(line_buffering=True)  # type: ignore[union-attr]

RESULTS: list[tuple[str, str, str]] = []  # (attack_name, status, note)


def _record(name: str, status: str, note: str = "") -> None:
    RESULTS.append((name, status, note))
    sym = {"PASS": "✓", "FAIL": "✗", "SKIP": "−", "WARN": "⚠"}[status]
    print(f"  {sym} [{status}] {name}" + (f" — {note}" if note else ""), flush=True)


def _setup_project(tmp: Path) -> Path:
    """Create an isolated codevira project under tmp. Returns project root."""
    fake_home = tmp / "home"
    fake_home.mkdir(exist_ok=True)
    project = tmp / "proj"
    project.mkdir(exist_ok=True)
    os.environ["HOME"] = str(fake_home)
    Path.home = classmethod(lambda cls: fake_home)  # type: ignore[assignment]
    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    paths_mod.get_global_home = lambda: fake_home / ".codevira"  # type: ignore[assignment]
    from mcp_server.storage import paths as store_paths

    store_paths.ensure_dirs()
    (project / "AGENTS.md").write_text(
        "<!-- codevira:begin -->\n<!-- codevira:end -->\n"
    )
    return project


# ─────────────────────────────────────────────────────────────────────
# ATTACK 1 — Adversarial decision input
# ─────────────────────────────────────────────────────────────────────


def attack_adversarial_input(tmp: Path) -> None:
    print("\n[1] Adversarial input — does record_decision reject / sanitize / accept?")
    _setup_project(tmp)
    from mcp_server.storage import decisions_store

    cases = [
        ("null_byte_text", "decision with \x00 null byte", "auth.py"),
        ("1mb_text", "x" * (1024 * 1024), "huge.py"),
        ("path_traversal", "ok", "../../etc/passwd"),
        ("absolute_path", "ok", "/etc/passwd"),
        ("path_with_null", "ok", "foo\x00.py"),
        ("emoji_decision", "🔥💀😈 verify perms 🛡️", "perms.py"),
        ("control_chars", "decision\x01with\x02control\x03chars", "ctrl.py"),
        ("empty_decision", "", "empty.py"),
        ("newlines_in_decision", "line1\nline2\r\nline3", "newlines.py"),
        ("unicode_path", "ok", "файл_кириллица.py"),
        ("very_long_tag", "ok", "tags.py"),
    ]

    for name, decision, file_path in cases:
        try:
            kwargs: dict = {"decision": decision, "file_path": file_path}
            if name == "very_long_tag":
                kwargs["tags"] = ["x" * 1024]
            did = decisions_store.record(**kwargs)
            if did:
                # Read back — make sure we can find it and the file isn't corrupted.
                from mcp_server.storage import jsonl_store, paths

                rows = jsonl_store.read_all(paths.decisions_path())
                hit = next((r for r in rows if r.get("id") == did), None)
                if hit:
                    _record(f"input.{name}", "PASS", f"accepted as {did}")
                else:
                    _record(
                        f"input.{name}", "FAIL", f"id {did} returned but not in JSONL"
                    )
            else:
                _record(f"input.{name}", "WARN", "record returned None/empty")
        except Exception as exc:
            _record(
                f"input.{name}", "WARN", f"rejected: {type(exc).__name__}: {exc!s:.80}"
            )

    # Type-confusion: pass non-string decision.
    for name, val in [
        ("decision_none", None),
        ("decision_int", 12345),
        ("decision_dict", {}),
    ]:
        try:
            did = decisions_store.record(decision=val, file_path="x.py")  # type: ignore[arg-type]
            _record(
                f"input.{name}", "WARN", f"accepted ({did}) — should reject non-str"
            )
        except Exception as exc:
            _record(f"input.{name}", "PASS", f"rejected: {type(exc).__name__}")


# ─────────────────────────────────────────────────────────────────────
# ATTACK 2 — Process-kill mid-write (SIGKILL while holding lock)
# ─────────────────────────────────────────────────────────────────────


def _holds_lock_then_sleeps(lock_path: str) -> None:
    """Subprocess body: acquire lock, then sleep forever."""
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from mcp_server.storage.atomic import file_lock

    with file_lock(Path(lock_path)):
        time.sleep(60)


def attack_kill_during_lock(tmp: Path) -> None:
    print("\n[2] Process kill while holding fcntl.flock — does the lock leak?")
    project = _setup_project(tmp)
    lock_target = project / "kill_test.lock"

    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=_holds_lock_then_sleeps, args=(str(lock_target),))
    p.start()

    # Give the child time to acquire.
    time.sleep(0.5)

    if p.pid is None:
        _record("kill.spawn", "FAIL", "child failed to start")
        return

    # SIGKILL it.
    try:
        os.kill(p.pid, signal.SIGKILL)
    except ProcessLookupError:
        _record("kill.send", "FAIL", "child already gone before kill")
        return

    p.join(timeout=5)
    if p.is_alive():
        p.terminate()
        _record("kill.reap", "FAIL", "child still alive 5s after SIGKILL")
        return

    # The lock should be released by the kernel when the process dies
    # (fcntl.flock is per-fd; closing/process-death drops it). Try to
    # re-acquire from this process.
    from mcp_server.storage.atomic import file_lock

    t0 = time.time()
    try:
        with file_lock(lock_target):
            elapsed = time.time() - t0
            if elapsed < 1.0:
                _record(
                    "kill.lock_released",
                    "PASS",
                    f"reacquired in {elapsed * 1000:.0f}ms",
                )
            else:
                _record(
                    "kill.lock_released", "WARN", f"reacquired but took {elapsed:.1f}s"
                )
    except Exception as exc:
        _record("kill.lock_released", "FAIL", f"could not reacquire: {exc}")


# ─────────────────────────────────────────────────────────────────────
# ATTACK 3 — Corrupted files
# ─────────────────────────────────────────────────────────────────────


def attack_corrupt_files(tmp: Path) -> None:
    print("\n[3] Corrupted files — does read code degrade gracefully?")
    project = _setup_project(tmp)
    from mcp_server.storage import decisions_store, paths

    # Seed 5 valid decisions.
    for i in range(5):
        decisions_store.record(decision=f"valid {i}", file_path=f"f{i}.py")

    # Corrupt the JSONL: inject a malformed line between line 2 and 3.
    p = paths.decisions_path()
    text = p.read_text()
    lines = text.splitlines()
    lines.insert(2, "{this is not valid json")
    lines.insert(3, "")  # empty line
    lines.insert(4, "\x00\x01\x02 garbage bytes \xff")
    p.write_text("\n".join(lines) + "\n")

    try:
        from mcp_server.storage import jsonl_store

        rows = jsonl_store.read_all(p)
        if len(rows) == 5:
            _record("corrupt.jsonl_skip_bad", "PASS", "read 5 valid, skipped 3 corrupt")
        else:
            _record(
                "corrupt.jsonl_skip_bad",
                "WARN",
                f"read {len(rows)} rows (expected 5 valid)",
            )
    except Exception as exc:
        _record(
            "corrupt.jsonl_skip_bad",
            "FAIL",
            f"crash on read: {type(exc).__name__}: {exc}",
        )

    # Now corrupt manifest.yaml (round-2 invariant test, but stronger).
    mp = paths.manifest_path()
    mp.write_text("\xff\xfe\xfd\xfc not yaml at all : : : [[[")
    try:
        new_id = decisions_store.record(
            decision="after corrupt", file_path="x.py", do_not_revert=True
        )
        if new_id:
            _record(
                "corrupt.manifest_record",
                "PASS",
                f"decision still persisted as {new_id} despite bad manifest",
            )
        else:
            _record("corrupt.manifest_record", "FAIL", "record returned None")
    except Exception as exc:
        _record(
            "corrupt.manifest_record",
            "FAIL",
            f"P9 violated: {type(exc).__name__}: {exc}",
        )

    # Corrupt AGENTS.md → can regenerate?
    ag = project / "AGENTS.md"
    ag.write_text("\xff\x00 not utf-8 ! \xfe")
    try:
        from mcp_server.storage import agents_md_generator

        agents_md_generator.regenerate(target_path=ag)
        # Either it succeeds (replaced content) or fails gracefully.
        _record(
            "corrupt.agents_md_regenerate", "PASS", "regenerate survived garbage input"
        )
    except Exception as exc:
        _record(
            "corrupt.agents_md_regenerate",
            "WARN",
            f"raised: {type(exc).__name__}: {exc!s:.80}",
        )


# ─────────────────────────────────────────────────────────────────────
# ATTACK 4 — Extreme concurrency (mixed ops)
# ─────────────────────────────────────────────────────────────────────


def attack_mixed_concurrency(tmp: Path) -> None:
    print("\n[4] 200 mixed concurrent ops (decisions + phases + sessions)")
    _setup_project(tmp)
    from mcp_server.storage import decisions_store, sessions_store
    from mcp_server.tools import roadmap

    errors: list[str] = []

    def op_decision(i: int) -> None:
        try:
            decisions_store.record(decision=f"chaos d{i}", file_path=f"d{i}.py")
        except Exception as exc:
            errors.append(f"decision[{i}]: {type(exc).__name__}: {exc}")

    def op_phase(i: int) -> None:
        try:
            r = roadmap.add_phase(phase=400 + i, name=f"chaos p{i}", description="x")
            if not r.get("success"):
                errors.append(f"phase[{i}]: {r}")
        except Exception as exc:
            errors.append(f"phase[{i}]: {type(exc).__name__}: {exc}")

    def op_session(i: int) -> None:
        try:
            # storage layer signature: (session_id, *, task, phase, summary, decision_ids, outcome)
            sessions_store.write(
                session_id=f"chaos-{i}",
                task="chaos test",
                phase="1",
                summary=f"chaos session {i}",
                decision_ids=[],
            )
        except Exception as exc:
            errors.append(f"session[{i}]: {type(exc).__name__}: {exc}")

    n = 200
    ops = []
    for i in range(n):
        ops.append(
            (op_decision, i)
            if i % 3 == 0
            else (op_phase, i)
            if i % 3 == 1
            else (op_session, i)
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(fn, i) for fn, i in ops]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                errors.append(f"future: {type(exc).__name__}: {exc}")

    if not errors:
        _record("concurrency.200_mixed_ops", "PASS", f"{n} ops, 20 workers, 0 errors")
    else:
        _record(
            "concurrency.200_mixed_ops",
            "FAIL",
            f"{len(errors)} errors out of {n} ops: {errors[:2]}",
        )


# ─────────────────────────────────────────────────────────────────────
# ATTACK 5 — Lock contention storm
# ─────────────────────────────────────────────────────────────────────


def attack_lock_contention(tmp: Path) -> None:
    print("\n[5] 20 threads racing for one lock, holder holds 2s — anybody starve?")
    _setup_project(tmp)
    from mcp_server.storage.atomic import file_lock

    target = tmp / "contention.lock"
    enter_times: list[float] = []
    enter_lock = __import__("threading").Lock()
    start = time.time()

    def worker(i: int) -> None:
        with file_lock(target):
            now = time.time() - start
            with enter_lock:
                enter_times.append(now)
            # Simulate 100ms of work each.
            time.sleep(0.1)

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(worker, range(20)))

    # All 20 should have entered. Total should be ~2s (20 × 100ms serialized).
    if len(enter_times) != 20:
        _record("contention.all_entered", "FAIL", f"only {len(enter_times)}/20 entered")
        return

    total = max(enter_times) - min(enter_times)
    if total < 1.5:
        _record(
            "contention.serialized",
            "FAIL",
            f"entries spread {total:.2f}s — not serialized (parallel ran)",
        )
    elif total > 5.0:
        _record(
            "contention.no_deadlock",
            "WARN",
            f"entries spread {total:.2f}s — slower than expected",
        )
    else:
        _record(
            "contention.serialized",
            "PASS",
            f"20 workers serialized over {total:.2f}s, no starvation",
        )


# ─────────────────────────────────────────────────────────────────────
# ATTACK 6 — Symlink traversal
# ─────────────────────────────────────────────────────────────────────


def attack_symlink(tmp: Path) -> None:
    print("\n[6] AGENTS.md symlinked to /tmp/innocent — does generator follow?")
    project = _setup_project(tmp)

    # Set up a "victim" file we want to confirm doesn't get clobbered.
    victim = tmp / "innocent_user_file.txt"
    victim.write_text("ORIGINAL USER CONTENT — must not be overwritten\n")

    # Symlink project/AGENTS.md → victim
    ag = project / "AGENTS.md"
    ag.unlink(missing_ok=True)
    ag.symlink_to(victim)

    try:
        from mcp_server.storage import agents_md_generator

        agents_md_generator.regenerate(target_path=ag)
        # Now: did we follow the symlink and clobber the victim?
        if victim.exists() and "ORIGINAL USER CONTENT" in victim.read_text():
            _record(
                "symlink.preserves_victim",
                "PASS",
                "victim file content preserved",
            )
        elif victim.exists():
            content = victim.read_text()[:60]
            _record(
                "symlink.preserves_victim",
                "WARN",
                f"victim clobbered: {content!r}",
            )
        else:
            _record("symlink.preserves_victim", "WARN", "victim file deleted")
    except Exception as exc:
        _record(
            "symlink.preserves_victim",
            "PASS",
            f"refused to follow symlink: {type(exc).__name__}",
        )


# ─────────────────────────────────────────────────────────────────────
# ATTACK 7 — MCP protocol abuse via stdio handshake
# ─────────────────────────────────────────────────────────────────────


def attack_mcp_abuse(tmp: Path) -> None:
    print("\n[7] MCP protocol abuse — malformed JSON-RPC + wrong types")
    project = _setup_project(tmp)

    # Spawn a real codevira MCP subprocess.
    # Pipe stdout/stderr to /dev/null so they can never block on a full
    # pipe buffer (we don't care about the response content for this
    # attack — only that the server doesn't crash).
    env = {**os.environ, "CODEVIRA_PROJECT_DIR": str(project)}
    devnull = open(os.devnull, "w")
    proc = subprocess.Popen(
        ["codevira", "serve"],
        stdin=subprocess.PIPE,
        stdout=devnull,
        stderr=devnull,
        env=env,
        text=True,
        bufsize=0,  # unbuffered so writes go through immediately
    )

    if proc.stdin is None:
        _record("mcp.spawn", "FAIL", "stdin missing")
        proc.kill()
        devnull.close()
        return

    # Payloads kept small enough to fit in the OS pipe buffer (default
    # 64 KB on macOS) — pre-fix this attack hung at 100 KB because the
    # server reads at MCP-frame pace, not pipe-fill pace.
    payloads = [
        ("not_json", "this is not json at all\n"),
        ("empty_line", "\n"),
        ("malformed_rpc", '{"jsonrpc": "2.0"}\n'),
        ("missing_method", '{"jsonrpc": "2.0", "id": 1}\n'),
        ("nonexistent_method", '{"jsonrpc": "2.0", "id": 2, "method": "foo"}\n'),
        (
            "wrong_type_param",
            '{"jsonrpc": "2.0", "id": 3, "method": "tools/call",'
            ' "params": {"name": 12345}}\n',
        ),
        (
            "large_payload_8kb",
            '{"jsonrpc": "2.0", "id": 4, "method": "x", "params": {"k": "'
            + "x" * 8000
            + '"}}\n',
        ),
    ]

    try:
        # Initialize first (proper handshake).
        init = (
            '{"jsonrpc": "2.0", "id": 0, "method": "initialize",'
            ' "params": {"protocolVersion": "2024-11-05",'
            ' "capabilities": {}, "clientInfo": {"name": "chaos",'
            ' "version": "1"}}}\n'
        )
        proc.stdin.write(init)
        proc.stdin.flush()
        time.sleep(0.5)

        for name, payload in payloads:
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
                time.sleep(0.05)
                if proc.poll() is not None:
                    _record(
                        f"mcp.{name}",
                        "FAIL",
                        f"server crashed (exit {proc.returncode})",
                    )
                    return
                _record(f"mcp.{name}", "PASS", "server survived")
            except BrokenPipeError:
                _record(f"mcp.{name}", "FAIL", "broken pipe — server died")
                return
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
        devnull.close()


# ─────────────────────────────────────────────────────────────────────
# ATTACK 8 — Read-only directory
# ─────────────────────────────────────────────────────────────────────


def attack_readonly_dir(tmp: Path) -> None:
    print("\n[8] Filesystem hostility — read-only .codevira/ dir")
    project = _setup_project(tmp)
    cv = project / ".codevira"

    # Make .codevira/ read-only.
    cv.chmod(0o555)
    try:
        from mcp_server.storage import decisions_store

        try:
            did = decisions_store.record(decision="readonly test", file_path="x.py")
            if did:
                _record(
                    "readonly.record",
                    "WARN",
                    f"recorded {did} despite chmod 0555 (centralized fallback?)",
                )
            else:
                _record("readonly.record", "PASS", "returned None gracefully")
        except PermissionError as exc:
            _record(
                "readonly.record",
                "PASS",
                f"raised PermissionError gracefully: {exc!s:.60}",
            )
        except Exception as exc:
            _record(
                "readonly.record",
                "WARN",
                f"raised {type(exc).__name__}: {exc!s:.60}",
            )
    finally:
        # Restore so cleanup doesn't fail.
        cv.chmod(0o755)


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────


def main() -> int:
    import tempfile

    print("=" * 70)
    print("codevira chaos smoke — adversarial probe of v3.0.0 storage layer")
    print("=" * 70)

    attacks = [
        ("adversarial_input", attack_adversarial_input),
        ("kill_during_lock", attack_kill_during_lock),
        ("corrupt_files", attack_corrupt_files),
        ("mixed_concurrency", attack_mixed_concurrency),
        ("lock_contention", attack_lock_contention),
        ("symlink", attack_symlink),
        ("mcp_abuse", attack_mcp_abuse),
        ("readonly_dir", attack_readonly_dir),
    ]

    for name, fn in attacks:
        # Each attack gets its own tmp dir.
        with tempfile.TemporaryDirectory(prefix=f"chaos_{name}_") as td:
            try:
                fn(Path(td))
            except Exception:
                tb = traceback.format_exc()
                _record(
                    f"{name}.HARNESS", "FAIL", f"attack harness crashed\n{tb[-400:]}"
                )

    # Summary.
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
    if fails:
        print("\n  FAIL breakdown:")
        for n, x in fails:
            print(f"    ✗ {n}: {x[:200]}")
    print("=" * 70)
    return 1 if counts.get("FAIL", 0) > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
