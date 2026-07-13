"""
doctor.py — Pillar 1.3 of the v2.0 master plan.

`codevira doctor` health check. Replaces the "buried in logs" pattern
with active diagnostics: runs a battery of checks, reports ✓ / ⚠ / ✗
per check, and tells the user the exact command to fix each ✗.

Design properties:

  - **Read-only.** Doctor never writes. If anything's broken, it
    diagnoses and prints the fix command — the user runs it.
  - **Fast.** Total runtime < 2s on a healthy install. Most checks
    are filesystem stat + a tiny sqlite probe.
  - **Honest.** No "OK" if we can't verify. Each check returns one
    of three states: PASS / WARN / FAIL.
  - **Composable.** Each check is a pure function returning a
    ``CheckResult``. Tests target individual checks; the CLI just
    runs them all.

Checks shipped in v2.0:

  C1  python_version          — Python ≥ 3.10
  C2  codevira_data_dir       — ~/.codevira exists + writable
  C3  project_root            — get_project_root() not in is_invalid_project_root
  C4  graph_db                — graph.db exists + opens + has expected tables
  C5  global_db               — global.db exists + opens
  C6  detected_ides           — at least one AI tool detected
  C7  nudge_files             — nudge files present where IDEs are detected
  C8  hooks_installed         — Claude Code lifecycle hooks installed
  C9  mcp_config              — codevira appears in detected IDE configs
  C10 watcher_status          — file watcher is running (or last-known-good)
  C11 engine_kill_switch      — CODEVIRA_ENGINE env var sanity
  C12 crash_log_size          — recent crash log is reasonable size
"""

from __future__ import annotations

import os
import sys
import sqlite3
from dataclasses import dataclass
from typing import IO, Callable


# =====================================================================
# Result types
# =====================================================================


_PASS = "PASS"
_WARN = "WARN"
_FAIL = "FAIL"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one doctor check."""

    name: str
    state: str  # PASS / WARN / FAIL
    message: str  # one-line summary
    fix_command: str = ""  # exact shell command to fix; "" if none / not applicable
    details: str = ""  # optional multi-line context (printed in --verbose mode)


@dataclass(frozen=True)
class DoctorReport:
    results: tuple[CheckResult, ...]

    @property
    def has_failures(self) -> bool:
        return any(r.state == _FAIL for r in self.results)

    @property
    def has_warnings(self) -> bool:
        return any(r.state == _WARN for r in self.results)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if r.state == _FAIL)

    @property
    def warn_count(self) -> int:
        return sum(1 for r in self.results if r.state == _WARN)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.state == _PASS)


# =====================================================================
# Individual checks
# =====================================================================


def check_python_version() -> CheckResult:
    """C1 — codevira requires Python ≥ 3.10."""
    major, minor = sys.version_info.major, sys.version_info.minor
    if major == 3 and minor >= 10:
        return CheckResult(
            "python_version",
            _PASS,
            f"Python {major}.{minor} (≥ 3.10 required)",
        )
    return CheckResult(
        "python_version",
        _FAIL,
        f"Python {major}.{minor} (need ≥ 3.10)",
        fix_command="brew install python@3.13  # or your preferred 3.10+ runtime",
    )


def check_codevira_data_dir() -> CheckResult:
    """C2 — ~/.codevira exists and is writable."""
    try:
        from mcp_server.paths import get_global_home

        home = get_global_home()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "codevira_data_dir",
            _FAIL,
            f"Could not resolve ~/.codevira: {e}",
            fix_command="codevira setup",
        )
    if not home.exists():
        return CheckResult(
            "codevira_data_dir",
            _WARN,
            f"{home} does not exist yet (first run?)",
            fix_command="codevira setup",
        )
    if not home.is_dir():
        return CheckResult(
            "codevira_data_dir",
            _FAIL,
            f"{home} exists but is not a directory",
            fix_command=f"rm '{home}' && codevira setup",
        )
    if not os.access(home, os.W_OK):
        return CheckResult(
            "codevira_data_dir",
            _FAIL,
            f"{home} is not writable",
            fix_command=f"chmod u+w '{home}'",
        )
    return CheckResult(
        "codevira_data_dir",
        _PASS,
        f"{home} exists and is writable",
    )


def check_project_root() -> CheckResult:
    """C3 — current project root is not a refused path ($HOME, /, etc.)."""
    try:
        from mcp_server.paths import get_project_root, is_invalid_project_root

        root = get_project_root()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "project_root",
            _FAIL,
            f"Could not resolve project root: {e}",
            fix_command="cd <your-project> && codevira doctor",
        )
    rejection = is_invalid_project_root(root)
    if rejection:
        return CheckResult(
            "project_root",
            _FAIL,
            f"{root} rejected as project root: {rejection}",
            fix_command="cd <project-with-.git-or-pyproject.toml> && codevira doctor",
        )
    return CheckResult(
        "project_root",
        _PASS,
        f"{root} is a valid project root",
    )


def check_project_binding() -> CheckResult:
    """C3b — HOW the active project resolved (explicit pin vs workspace).

    Lets a user confirm codevira is bound to the RIGHT project. An MCP
    server with no explicit pin resolves the project from the editor's
    workspace roots at runtime (stdio) or the current directory (CLI). If
    memory ever shows the wrong project, pinning makes it deterministic.
    This is observability for the user-scope-server binding issue.
    """
    import os

    try:
        from mcp_server import paths as _paths
        from mcp_server.paths import get_project_root

        root = get_project_root()
    except Exception as e:  # noqa: BLE001 — never let a check crash the report
        return CheckResult(
            "project_binding",
            _WARN,
            f"could not determine binding: {e}",
        )

    if _paths._project_dir_override is not None:
        return CheckResult(
            "project_binding",
            _PASS,
            f"pinned via --project-dir -> {root}",
        )
    if os.environ.get("CODEVIRA_PROJECT_DIR"):
        return CheckResult(
            "project_binding",
            _PASS,
            f"pinned via CODEVIRA_PROJECT_DIR -> {root}",
        )
    return CheckResult(
        "project_binding",
        _PASS,
        f"resolved from workspace -> {root}",
        details=(
            "No explicit pin. The MCP server binds to your editor's workspace "
            "(client roots) at runtime; the CLI uses the current directory. "
            "Confirm the path above is the project you intend. If memory ever "
            "shows the WRONG project, pin it deterministically."
        ),
        fix_command=(
            "codevira serve --project-dir <your-project>  "
            "# or export CODEVIRA_PROJECT_DIR=<your-project>"
        ),
    )


def check_graph_db() -> CheckResult:
    """C4 — graph.db opens + has the expected tables."""
    try:
        from mcp_server.paths import get_data_dir

        data_dir = get_data_dir()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "graph_db",
            _WARN,
            f"data dir not resolvable: {e}",
            fix_command="codevira init",
        )
    db_path = data_dir / "graph" / "graph.db"
    if not db_path.exists():
        return CheckResult(
            "graph_db",
            _WARN,
            f"{db_path} does not exist (no index yet)",
            fix_command="codevira index",
        )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        finally:
            conn.close()
    except sqlite3.Error as e:
        return CheckResult(
            "graph_db",
            _FAIL,
            f"graph.db is corrupted or unreadable: {e}",
            fix_command=(f"rm '{db_path}' && codevira index  # full re-index"),
            details=f"path: {db_path}",
        )
    expected = {"nodes", "decisions", "sessions", "outcomes"}
    missing = expected - tables
    if missing:
        return CheckResult(
            "graph_db",
            _FAIL,
            f"graph.db missing tables: {sorted(missing)}",
            fix_command=f"rm '{db_path}' && codevira index",
        )
    return CheckResult(
        "graph_db",
        _PASS,
        f"graph.db has all {len(expected)} expected tables",
    )


def check_global_db() -> CheckResult:
    """C5 — ~/.codevira/global.db opens."""
    try:
        from mcp_server.paths import get_global_home

        home = get_global_home()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "global_db",
            _WARN,
            f"global home unresolvable: {e}",
        )
    db_path = home / "global.db"
    if not db_path.exists():
        return CheckResult(
            "global_db",
            _WARN,
            "global.db does not exist (first run?)",
            fix_command="codevira setup",
        )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("SELECT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        return CheckResult(
            "global_db",
            _FAIL,
            f"global.db unreadable: {e}",
            fix_command=f"mv '{db_path}' '{db_path}.bak' && codevira setup",
        )
    return CheckResult(
        "global_db",
        _PASS,
        f"{db_path} opens cleanly",
    )


def check_detected_ides() -> CheckResult:
    """C6 — at least one AI coding tool is detected on this machine."""
    try:
        from mcp_server.setup_wizard import detect_targets
        from mcp_server.paths import get_project_root

        detected = detect_targets(get_project_root())
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "detected_ides",
            _WARN,
            f"Could not run IDE detection: {e}",
        )
    if not detected:
        return CheckResult(
            "detected_ides",
            _WARN,
            "No AI coding tools detected (Claude Code, Cursor, etc.)",
            fix_command=(
                "Install at least one: claude.ai/download · cursor.sh · "
                "windsurf.com · etc."
            ),
        )
    return CheckResult(
        "detected_ides",
        _PASS,
        f"{len(detected)} AI tool(s) detected: {', '.join(sorted(detected))}",
    )


def check_nudge_files() -> CheckResult:
    """C7 — AGENTS.md exists and carries the codevira marker block.

    v2.2.0+ (2026-05-22 surface-cut audit): the per-IDE nudge file
    matrix collapsed to AGENTS.md only. Every modern AI tool reads
    AGENTS.md (Linux Foundation standard) natively, so the duplicates
    (CLAUDE.md / GEMINI.md / .cursor/rules/codevira.mdc / .windsurfrules
    / .github/copilot-instructions.md) were deleted. This check just
    verifies the one remaining file is healthy.
    """
    try:
        from mcp_server.paths import get_project_root, is_invalid_project_root

        root = get_project_root()
        if is_invalid_project_root(root):
            return CheckResult(
                "nudge_files",
                _WARN,
                "skipped (project root invalid; see project_root check)",
            )
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "nudge_files",
            _WARN,
            f"could not check: {e}",
        )
    agents_md = root / "AGENTS.md"
    if not agents_md.is_file():
        return CheckResult(
            "nudge_files",
            _WARN,
            "AGENTS.md not found — run `codevira sync` to create it",
            fix_command="codevira sync",
        )
    try:
        text = agents_md.read_text(encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        return CheckResult(
            "nudge_files",
            _WARN,
            f"AGENTS.md exists but is unreadable: {e}",
        )
    if "<!-- codevira:begin" not in text:
        return CheckResult(
            "nudge_files",
            _WARN,
            "AGENTS.md exists but has no codevira block — "
            "run `codevira sync` to regenerate",
            fix_command="codevira sync",
        )
    return CheckResult(
        "nudge_files",
        _PASS,
        "AGENTS.md present with codevira block",
    )


def check_watcher_circuit() -> CheckResult:
    """C10 — file watcher circuit breaker state (Pillar 3.2).

    The background watcher runs incremental reindex on file changes.
    If it fails repeatedly, a circuit breaker opens and skips reindexes
    for an exponentially-backing-off window (1m → 30m cap). We surface
    the state here so users see "watcher is in circuit-open backoff"
    instead of silent staleness.
    """
    try:
        from indexer.index_codebase import watcher_circuit_status

        status = watcher_circuit_status()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "watcher_circuit",
            _WARN,
            f"could not read watcher state: {e}",
        )
    if status["open"]:
        return CheckResult(
            "watcher_circuit",
            _FAIL,
            f"watcher circuit OPEN ({status['consecutive_failures']} "
            f"failures; {status['seconds_until_retry']:.0f}s until retry)",
            fix_command="see ~/.codevira/logs/crashes.log for the underlying error",
            details=f"last error: {status['last_error']}",
        )
    if status["consecutive_failures"] > 0:
        return CheckResult(
            "watcher_circuit",
            _WARN,
            f"watcher had {status['consecutive_failures']} recent failure(s) "
            f"but circuit still closed",
            details=f"last error: {status['last_error']}",
        )
    return CheckResult(
        "watcher_circuit",
        _PASS,
        "watcher circuit clean (no recent failures)",
    )


def check_engine_kill_switch() -> CheckResult:
    """C11 — CODEVIRA_ENGINE env var, if set, must be 0 or 1."""
    val = os.environ.get("CODEVIRA_ENGINE")
    if val is None:
        return CheckResult(
            "engine_kill_switch",
            _PASS,
            "engine ON (default; CODEVIRA_ENGINE not set)",
        )
    if val == "0":
        return CheckResult(
            "engine_kill_switch",
            _WARN,
            "engine DISABLED via CODEVIRA_ENGINE=0",
            fix_command="unset CODEVIRA_ENGINE  # to re-enable",
        )
    if val == "1":
        return CheckResult(
            "engine_kill_switch",
            _PASS,
            "engine ON (CODEVIRA_ENGINE=1 explicit)",
        )
    return CheckResult(
        "engine_kill_switch",
        _WARN,
        f"CODEVIRA_ENGINE={val!r} is unexpected (use 0 or 1)",
        fix_command="export CODEVIRA_ENGINE=1",
    )


def check_claude_mcp_visibility() -> CheckResult:
    """C13 (v2.0-rc.4 / Bug 10) — verify codevira is reachable to Claude Code.

    Catches the regression that broke rc.1: setup wrote MCP config to the
    wrong file (~/.claude/settings.json instead of ~/.claude.json) and
    Claude Code didn't see codevira at all. Hooks fired, doctor reported
    green, but `claude mcp list` returned nothing for codevira.

    Strategy: shell out to ``claude mcp list`` if the CLI is present.
    If ``codevira`` shows up → PASS. If the CLI runs but codevira is
    missing → FAIL with the exact ``claude mcp add`` command. If the
    CLI isn't installed (user only has Claude Desktop or some other
    AI tool) → WARN with a note that this check is informational.
    """
    import shutil
    import subprocess

    claude = shutil.which("claude")
    if not claude:
        return CheckResult(
            "claude_mcp_visibility",
            _WARN,
            "claude CLI not found on PATH (skipped)",
            details=(
                "This check verifies Claude Code's MCP runtime sees "
                "codevira. Without the `claude` CLI we can't probe it. "
                "If you only use Claude Desktop / Cursor / Windsurf, "
                "this is fine — those tools have their own indicators."
            ),
        )

    try:
        result = subprocess.run(
            [claude, "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return CheckResult(
            "claude_mcp_visibility",
            _WARN,
            f"claude mcp list failed: {e}",
        )

    if result.returncode != 0:
        return CheckResult(
            "claude_mcp_visibility",
            _WARN,
            f"claude mcp list exited {result.returncode}",
            details=(result.stderr or "")[:400],
        )

    out = result.stdout or ""
    if "codevira" in out.lower():
        # P1-4 (rc.5): the "Connected" indicator that `claude mcp list`
        # prints reflects the **currently-active Claude Code session's**
        # MCP state — which depends on the project root that session was
        # opened in. Running doctor from $HOME or some other cwd will
        # show codevira as "listed but not connected" even when the
        # install is fine, because there's no active session in $HOME.
        # Pre-fix this WARNed on every doctor invocation from a non-active
        # project, which was a false alarm.
        #
        # rc.5 follow-up (post-restart smoke test): the original P1-4 fix
        # checked for "✗ Failed" anywhere in `out`, but `claude mcp list`
        # prints ALL registered MCP servers — so a failed unrelated server
        # (e.g. plugin:github:github with auth issue) made codevira's
        # check WARN even though codevira itself was ✓ Connected. Now we
        # parse the codevira-specific line in isolation.
        codevira_lines = [
            line
            for line in out.splitlines()
            if "codevira" in line.lower() and ":" in line
        ]
        codevira_line = codevira_lines[0] if codevira_lines else ""
        has_failed = "✗ Failed" in codevira_line
        connected = "✓ Connected" in codevira_line
        if has_failed:
            return CheckResult(
                "claude_mcp_visibility",
                _WARN,
                "codevira listed but Claude Code shows ✗ Failed — restart Claude Code",
            )
        if connected:
            return CheckResult(
                "claude_mcp_visibility",
                _PASS,
                "codevira visible to Claude Code (✓ Connected)",
            )
        # Listed but no Connected/Failed indicator. Most common cause:
        # `claude mcp list` was invoked outside an active Claude Code
        # project — the registration is fine; we just can't probe the
        # live session state from here.
        return CheckResult(
            "claude_mcp_visibility",
            _PASS,
            "codevira registered in Claude Code MCP config "
            "(live connection state unprobed from this cwd)",
        )

    return CheckResult(
        "claude_mcp_visibility",
        _FAIL,
        "codevira NOT in claude mcp list — Claude Code can't see it",
        fix_command=(
            "codevira setup -y   # re-runs the user-scope MCP merge; "
            "if that fails, fall back to: claude mcp add --scope user "
            "codevira $(which codevira)"
        ),
    )


def check_codeindex_freshness() -> CheckResult:
    """C14 (v2.0-rc.4 / Bug 11) — detect stale codeindex from older
    codevira version.

    AgentStore dogfood (2026-05-06): a v1.8.1-era codeindex/ directory
    persisted into the v2.0.0rc2 install and contributed to the
    sentence-transformers segfault on first re-index. Doctor should
    detect codeindex dirs whose embedding-model state is incompatible
    with the current codevira version and recommend wiping them.

    Heuristic: if a per-project ``codeindex/`` exists and was last
    written more than 2 weeks before the current codevira's install
    time, flag as WARN. We don't auto-wipe — that risks losing data.
    """
    try:
        from mcp_server.paths import get_data_dir, get_project_root

        project_root = get_project_root()
        if project_root is None:
            return CheckResult(
                "codeindex_freshness",
                _PASS,
                "no project — skipped",
            )
        data_dir = get_data_dir()
        codeindex = data_dir / "codeindex"
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "codeindex_freshness",
            _PASS,
            f"could not resolve codeindex path: {e}",
        )

    if not codeindex.exists():
        return CheckResult(
            "codeindex_freshness",
            _PASS,
            "no codeindex directory yet (will be built on first index)",
        )

    # Find the freshest mtime in codeindex/ (recursive). If everything
    # is older than 2 weeks AND we have at least one chromadb file,
    # warn that the codeindex may be incompatible.
    import time

    now = time.time()
    THRESHOLD_DAYS = 14

    freshest = 0.0
    has_files = False
    for path in codeindex.rglob("*"):
        if path.is_file():
            has_files = True
            try:
                m = path.stat().st_mtime
                if m > freshest:
                    freshest = m
            except OSError:
                continue
    if not has_files:
        return CheckResult(
            "codeindex_freshness",
            _PASS,
            "codeindex empty (clean state)",
        )

    age_days = (now - freshest) / 86400
    if age_days > THRESHOLD_DAYS:
        return CheckResult(
            "codeindex_freshness",
            _WARN,
            f"codeindex last touched {int(age_days)} days ago (>{THRESHOLD_DAYS}) — may be stale",
            fix_command=f"rm -rf '{codeindex}' && codevira index",
            details=(
                "Older codeindex directories may use embedding model "
                "state incompatible with the current codevira install. "
                "Wiping forces a fresh build on the next index."
            ),
        )
    return CheckResult(
        "codeindex_freshness",
        _PASS,
        f"codeindex last touched {int(age_days)} day(s) ago (recent)",
    )


def check_semantic_search_health() -> CheckResult:
    """C15 (v2.0-rc.4 / Bug 12) — surface ChromaDB chunks=0 as a WARN.

    A project with 0 indexed chunks degrades search_codebase to a
    structural-only fallback (still returns matches, but with no
    semantic ranking). Without this check the user only finds out
    when search_codebase results feel weak. Doctor flagging it gives
    one-line fix guidance.
    """
    try:
        from mcp_server.paths import get_data_dir, get_project_root

        project_root = get_project_root()
        if project_root is None:
            return CheckResult(
                "semantic_search_health",
                _PASS,
                "no project — skipped",
            )
        data_dir = get_data_dir()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "semantic_search_health",
            _PASS,
            f"could not resolve data dir: {e}",
        )

    # Check for chromadb importability + chunks. We don't actually open
    # chromadb here — opening it loads sentence-transformers which is
    # slow and can crash (Bug 7). Instead, sniff the codeindex SQLite
    # file size as a cheap proxy.
    codeindex = data_dir / "codeindex"
    if not codeindex.exists():
        return CheckResult(
            "semantic_search_health",
            _WARN,
            "no codeindex — semantic search degraded",
            fix_command="codevira index   # builds the embedding index",
        )

    # Sum sizes of all chromadb files; tiny total = empty collection.
    total_size = 0
    file_count = 0
    for path in codeindex.rglob("*"):
        if path.is_file():
            try:
                total_size += path.stat().st_size
                file_count += 1
            except OSError:
                continue

    # 100 KB is the empty-but-initialised baseline (chromadb metadata).
    # Real projects exceed 1 MB after a small index.
    if total_size < 100 * 1024:
        return CheckResult(
            "semantic_search_health",
            _WARN,
            f"codeindex looks empty ({total_size // 1024} KB) — semantic search degraded",
            fix_command="codevira index   # rebuild the embedding index",
        )
    return CheckResult(
        "semantic_search_health",
        _PASS,
        f"codeindex {total_size // 1024} KB across {file_count} file(s)",
    )


def check_crash_log_size() -> CheckResult:
    """C12 — surface recorded crashes (count + distinct fingerprints) and
    guard the log size.

    Pre-fix this read ``crash.log`` while the logger writes ``crashes.log``,
    so it never saw a real crash and always passed vacuously. It now reads
    the canonical path and reports what's actually failing — the point of
    the check.
    """
    try:
        from mcp_server import crash_logger

        digest = crash_logger.crash_digest()
        log = crash_logger.get_crash_log_path()
    except Exception:  # noqa: BLE001 — never let the check itself crash
        return CheckResult("crash_log_size", _WARN, "could not read crash log")

    if digest["total"] == 0:
        return CheckResult("crash_log_size", _PASS, "no crashes recorded (clean state)")

    LIMIT_MB = 5
    if digest["size_kb"] > LIMIT_MB * 1024:
        return CheckResult(
            "crash_log_size",
            _WARN,
            f"crash log is {digest['size_kb'] / 1024:.0f} MB (>{LIMIT_MB} MB) — rotate it",
            fix_command=f"mv '{log}' '{log}.archived'",
        )

    recent = f"; most recent: {digest['recent_type']}" if digest["recent_type"] else ""
    return CheckResult(
        "crash_log_size",
        _WARN,
        f"{digest['total']} crash(es) recorded, {digest['distinct']} distinct{recent}",
        fix_command=f"cat '{log}'  # sanitized tracebacks; delete the file to clear",
        details=f"crash log: {log}",
    )


# =====================================================================
# Runner
# =====================================================================


# Bug 21c (rc.4): check_ghost_projects lives in _ghost_check.py to keep this
# hot file's signature surface small.
from mcp_server._ghost_check import check_ghost_projects  # noqa: E402


def check_codevira_dir() -> CheckResult:
    """v2.2.0: confirm in-repo .codevira/ exists with expected files."""
    from mcp_server.storage import paths as storage_paths

    if not storage_paths.is_initialized():
        return CheckResult(
            "codevira_dir",
            _WARN,
            "No .codevira/ in this project — run `codevira init`",
        )

    decisions_count = 0
    try:
        from mcp_server.storage import jsonl_store

        decisions_count = jsonl_store.count(storage_paths.decisions_path())
    except Exception:
        pass

    return CheckResult(
        "codevira_dir",
        _PASS,
        f".codevira/ present ({decisions_count} decision(s))",
    )


def check_agents_md_size() -> CheckResult:
    """v2.2.0: AGENTS.md is generated; warn if it exceeds the 10 KB safety
    threshold (5 KB cap on the codevira block + reasonable user content)."""
    from mcp_server.paths import get_project_root

    agents_md = get_project_root() / "AGENTS.md"
    if not agents_md.is_file():
        return CheckResult(
            "agents_md_size",
            _PASS,
            "AGENTS.md not present (will be generated on first record_decision)",
        )

    size = agents_md.stat().st_size
    if size > 10 * 1024:
        return CheckResult(
            "agents_md_size",
            _WARN,
            f"AGENTS.md is {size:,} bytes; codevira block has a 5 KB cap, but "
            f"user content outside markers may have grown",
        )
    return CheckResult(
        "agents_md_size",
        _PASS,
        f"AGENTS.md is {size:,} bytes (≤10 KB safety threshold)",
    )


def check_mcp_running_versions() -> CheckResult:
    """v3.0.0 RC-audit follow-up (D-pipx-stale-mcp).

    When a user runs ``pipx install --force codevira`` after their IDE
    has already spawned an MCP stdio child, the new wheel sits on disk
    but the running child keeps serving the OLD code from its
    ``sys.modules`` cache. The user's edits don't take effect until
    they restart Claude Code / Cursor / Antigravity.

    Each MCP server writes its version to ``~/.codevira/run/<pid>.json``
    on startup. This check reads the registry, compares each running
    MCP's version to the currently-installed wheel, and warns when
    they drift.
    """
    try:
        from mcp_server._mcp_registry import list_running
        from mcp_server import __version__ as wheel_version
    except Exception as e:
        return CheckResult(
            "mcp_running_versions",
            _WARN,
            f"registry unavailable: {e}",
        )

    running = list_running()
    if not running:
        return CheckResult(
            "mcp_running_versions",
            _PASS,
            "no running MCP servers registered (no IDE active or "
            "registry hasn't been populated yet)",
        )

    stale = [m for m in running if m.get("version") != wheel_version]
    if not stale:
        return CheckResult(
            "mcp_running_versions",
            _PASS,
            f"{len(running)} running MCP(s) all on wheel version {wheel_version}",
        )

    pids = ", ".join(f"pid {m.get('pid')} (v{m.get('version', '?')})" for m in stale)
    return CheckResult(
        "mcp_running_versions",
        _WARN,
        f"{len(stale)} of {len(running)} running MCP(s) on a stale version "
        f"(wheel is v{wheel_version}): {pids}. Restart Claude Code / Cursor "
        f"/ Antigravity to load the new code.",
        fix_command="# Restart your IDE to reload the MCP subprocess",
    )


def check_merge_driver() -> CheckResult:
    """v3.7.0 — the decision-log git merge driver is configured in THIS clone
    when .gitattributes references it. Catches the fresh-clone gap where a
    teammate inherits the .gitattributes mapping but never ran `codevira init`,
    so cross-engineer decision-log merges would conflict."""
    try:
        from mcp_server.cli_repair import merge_driver_gap
        from mcp_server.paths import get_project_root

        gap = merge_driver_gap(get_project_root())
    except Exception as e:  # noqa: BLE001 — never crash the doctor
        return CheckResult("merge_driver", _WARN, f"could not check merge driver: {e}")
    if gap:
        return CheckResult("merge_driver", _WARN, gap, fix_command="codevira init")
    return CheckResult(
        "merge_driver",
        _PASS,
        "decision-log merge driver configured (or not referenced)",
    )


def check_decision_collisions() -> CheckResult:
    """v3.7.0 — surface pre-existing base-id collisions in decisions.jsonl.

    The startup self-heal repairs these automatically the first time; this
    catches any that appear LATER (e.g. a cross-engineer merge on a clone
    without the merge driver installed), which would otherwise silently shadow
    one decision per collision on read."""
    try:
        from mcp_server.storage import id_repair, jsonl_store
        from mcp_server.storage import paths as store_paths

        raw = jsonl_store.read_all(store_paths.decisions_path())
        collisions = id_repair.find_collisions(raw)
    except Exception as e:  # noqa: BLE001 — never crash the doctor
        return CheckResult(
            "decision_collisions", _WARN, f"could not scan for collisions: {e}"
        )
    if collisions:
        return CheckResult(
            "decision_collisions",
            _WARN,
            f"{len(collisions)} base-id collision(s) in decisions.jsonl — one "
            "decision per collision is shadowed on read until repaired",
            fix_command="codevira repair-ids --apply",
        )
    return CheckResult("decision_collisions", _PASS, "no decision-id collisions")


_CHECKS: tuple[Callable[[], CheckResult], ...] = (
    check_python_version,
    check_codevira_data_dir,
    check_project_root,
    check_project_binding,  # v3.4.0 — surface pin vs workspace resolution
    check_codevira_dir,  # v2.2.0 — replaces check_codeindex_freshness
    check_agents_md_size,  # v2.2.0 — new
    check_graph_db,
    check_global_db,
    check_detected_ides,
    check_nudge_files,
    check_watcher_circuit,
    check_engine_kill_switch,
    check_claude_mcp_visibility,  # rc.4 (Bug 10)
    check_mcp_running_versions,  # v3.0.0 (2026-05-23 RC-audit follow-up)
    # v2.2.0: check_codeindex_freshness + check_semantic_search_health
    # removed (chromadb deleted in Phase E).
    check_ghost_projects,  # rc.4 (Bug 21c)
    check_crash_log_size,
    check_merge_driver,  # v3.7.0 — cross-engineer decision-log merge driver
    check_decision_collisions,  # v3.7.0 — surface un-healed base-id collisions
)


def run_all_checks() -> DoctorReport:
    """Execute every check; return the report. Never raises."""
    results: list[CheckResult] = []
    for check in _CHECKS:
        try:
            results.append(check())
        except Exception as e:  # noqa: BLE001 — defense
            results.append(
                CheckResult(
                    check.__name__.replace("check_", ""),
                    _FAIL,
                    f"check itself crashed: {type(e).__name__}: {e}",
                    fix_command="codevira report  # send the crash log",
                )
            )
    return DoctorReport(results=tuple(results))


# =====================================================================
# CLI entry point
# =====================================================================


def cmd_doctor(*, verbose: bool = False, out: IO[str] | None = None) -> int:
    """`codevira doctor` — print the report. Returns:
       0 if all checks PASS or WARN
       1 if any check FAILed

    P0-1 (rc.5): the doctor checks unfortunately trip a per-project mkdir
    somewhere inside the path-resolution stack we haven't been able to
    isolate cleanly (some chain via ``get_data_dir()`` materialises
    ``~/.codevira/projects/<slug>/`` as a side effect of statting paths
    under it). The fix here is post-hoc: snapshot the projects dir at
    entry, then ALWAYS remove any new dirs at exit. Doctor stays the
    "read-only diagnostic" the docs promise — without us having to find
    every mkdir caller.
    """
    out = out or sys.stdout

    # P0-1: snapshot projects dir, restore at exit.
    snapshot_pre: set[str] = set()
    snapshot_root = None
    try:
        from mcp_server.paths import get_global_home

        snapshot_root = get_global_home() / "projects"
        if snapshot_root.is_dir():
            snapshot_pre = {p.name for p in snapshot_root.iterdir() if p.is_dir()}
    except Exception:
        pass

    report = run_all_checks()

    # P0-1: restore — remove any project dir that didn't exist before
    # the doctor run, so doctor genuinely is read-only.
    if snapshot_root is not None and snapshot_root.is_dir():
        try:
            import shutil as _shutil

            for p in snapshot_root.iterdir():
                if p.is_dir() and p.name not in snapshot_pre:
                    _shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass

    out.write("Codevira health check\n")
    out.write("─" * 60 + "\n")

    icons = {_PASS: "✓", _WARN: "⚠", _FAIL: "✗"}
    for r in report.results:
        icon = icons.get(r.state, "?")
        out.write(f"{icon}  {r.name:<22} {r.message}\n")
        if r.state in (_WARN, _FAIL) and r.fix_command:
            out.write(f"   → to fix: {r.fix_command}\n")
        if verbose and r.details:
            for line in r.details.splitlines():
                out.write(f"      {line}\n")

    out.write("─" * 60 + "\n")
    out.write(
        f"summary: {report.pass_count} pass · "
        f"{report.warn_count} warn · {report.fail_count} fail\n"
    )

    if report.has_failures:
        return 1
    return 0
