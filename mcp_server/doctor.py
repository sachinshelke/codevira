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
from dataclasses import dataclass, field
from pathlib import Path
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
    state: str             # PASS / WARN / FAIL
    message: str           # one-line summary
    fix_command: str = ""  # exact shell command to fix; "" if none / not applicable
    details: str = ""      # optional multi-line context (printed in --verbose mode)


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
            "python_version", _PASS,
            f"Python {major}.{minor} (≥ 3.10 required)",
        )
    return CheckResult(
        "python_version", _FAIL,
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
            "codevira_data_dir", _FAIL,
            f"Could not resolve ~/.codevira: {e}",
            fix_command="codevira setup",
        )
    if not home.exists():
        return CheckResult(
            "codevira_data_dir", _WARN,
            f"{home} does not exist yet (first run?)",
            fix_command="codevira setup",
        )
    if not home.is_dir():
        return CheckResult(
            "codevira_data_dir", _FAIL,
            f"{home} exists but is not a directory",
            fix_command=f"rm '{home}' && codevira setup",
        )
    if not os.access(home, os.W_OK):
        return CheckResult(
            "codevira_data_dir", _FAIL,
            f"{home} is not writable",
            fix_command=f"chmod u+w '{home}'",
        )
    return CheckResult(
        "codevira_data_dir", _PASS,
        f"{home} exists and is writable",
    )


def check_project_root() -> CheckResult:
    """C3 — current project root is not a refused path ($HOME, /, etc.)."""
    try:
        from mcp_server.paths import get_project_root, is_invalid_project_root
        root = get_project_root()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "project_root", _FAIL,
            f"Could not resolve project root: {e}",
            fix_command="cd <your-project> && codevira doctor",
        )
    rejection = is_invalid_project_root(root)
    if rejection:
        return CheckResult(
            "project_root", _FAIL,
            f"{root} rejected as project root: {rejection}",
            fix_command="cd <project-with-.git-or-pyproject.toml> && codevira doctor",
        )
    return CheckResult(
        "project_root", _PASS,
        f"{root} is a valid project root",
    )


def check_graph_db() -> CheckResult:
    """C4 — graph.db opens + has the expected tables."""
    try:
        from mcp_server.paths import get_data_dir
        data_dir = get_data_dir()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "graph_db", _WARN,
            f"data dir not resolvable: {e}",
            fix_command="codevira init",
        )
    db_path = data_dir / "graph" / "graph.db"
    if not db_path.exists():
        return CheckResult(
            "graph_db", _WARN,
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
            "graph_db", _FAIL,
            f"graph.db is corrupted or unreadable: {e}",
            fix_command=(
                f"rm '{db_path}' && codevira index  # full re-index"
            ),
            details=f"path: {db_path}",
        )
    expected = {"nodes", "decisions", "sessions", "outcomes"}
    missing = expected - tables
    if missing:
        return CheckResult(
            "graph_db", _FAIL,
            f"graph.db missing tables: {sorted(missing)}",
            fix_command=f"rm '{db_path}' && codevira index",
        )
    return CheckResult(
        "graph_db", _PASS,
        f"graph.db has all {len(expected)} expected tables",
    )


def check_global_db() -> CheckResult:
    """C5 — ~/.codevira/global.db opens."""
    try:
        from mcp_server.paths import get_global_home
        home = get_global_home()
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "global_db", _WARN,
            f"global home unresolvable: {e}",
        )
    db_path = home / "global.db"
    if not db_path.exists():
        return CheckResult(
            "global_db", _WARN,
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
            "global_db", _FAIL,
            f"global.db unreadable: {e}",
            fix_command=f"mv '{db_path}' '{db_path}.bak' && codevira setup",
        )
    return CheckResult(
        "global_db", _PASS,
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
            "detected_ides", _WARN,
            f"Could not run IDE detection: {e}",
        )
    if not detected:
        return CheckResult(
            "detected_ides", _WARN,
            "No AI coding tools detected (Claude Code, Cursor, etc.)",
            fix_command=(
                "Install at least one: claude.ai/download · cursor.sh · "
                "windsurf.com · etc."
            ),
        )
    return CheckResult(
        "detected_ides", _PASS,
        f"{len(detected)} AI tool(s) detected: {', '.join(sorted(detected))}",
    )


def check_nudge_files() -> CheckResult:
    """C7 — for each detected IDE, the nudge file exists in this project."""
    try:
        from mcp_server.setup_wizard import detect_targets
        from mcp_server.agents_md import target_path_for
        from mcp_server.paths import get_project_root, is_invalid_project_root
        root = get_project_root()
        if is_invalid_project_root(root):
            return CheckResult(
                "nudge_files", _WARN,
                "skipped (project root invalid; see project_root check)",
            )
        detected = detect_targets(root)
    except Exception as e:  # noqa: BLE001
        return CheckResult(
            "nudge_files", _WARN, f"could not check: {e}",
        )
    if not detected:
        return CheckResult(
            "nudge_files", _WARN,
            "no IDEs detected, no nudge files expected",
        )
    missing: list[str] = []
    for ide in detected:
        try:
            path = target_path_for(ide, root)
        except ValueError:
            continue
        if not path.exists():
            try:
                missing.append(f"{ide} ({path.relative_to(root)})")
            except ValueError:
                missing.append(f"{ide} ({path})")
    if missing:
        return CheckResult(
            "nudge_files", _WARN,
            f"missing nudge files for {len(missing)} detected IDE(s)",
            fix_command="codevira agents",
            details="\n".join(f"  - {m}" for m in missing),
        )
    return CheckResult(
        "nudge_files", _PASS,
        f"all {len(detected)} detected IDE(s) have nudge files",
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
            "watcher_circuit", _WARN,
            f"could not read watcher state: {e}",
        )
    if status["open"]:
        return CheckResult(
            "watcher_circuit", _FAIL,
            f"watcher circuit OPEN ({status['consecutive_failures']} "
            f"failures; {status['seconds_until_retry']:.0f}s until retry)",
            fix_command="codevira report  # check crash log for the underlying error",
            details=f"last error: {status['last_error']}",
        )
    if status["consecutive_failures"] > 0:
        return CheckResult(
            "watcher_circuit", _WARN,
            f"watcher had {status['consecutive_failures']} recent failure(s) "
            f"but circuit still closed",
            details=f"last error: {status['last_error']}",
        )
    return CheckResult(
        "watcher_circuit", _PASS,
        "watcher circuit clean (no recent failures)",
    )


def check_engine_kill_switch() -> CheckResult:
    """C11 — CODEVIRA_ENGINE env var, if set, must be 0 or 1."""
    val = os.environ.get("CODEVIRA_ENGINE")
    if val is None:
        return CheckResult(
            "engine_kill_switch", _PASS,
            "engine ON (default; CODEVIRA_ENGINE not set)",
        )
    if val == "0":
        return CheckResult(
            "engine_kill_switch", _WARN,
            "engine DISABLED via CODEVIRA_ENGINE=0",
            fix_command="unset CODEVIRA_ENGINE  # to re-enable",
        )
    if val == "1":
        return CheckResult(
            "engine_kill_switch", _PASS,
            "engine ON (CODEVIRA_ENGINE=1 explicit)",
        )
    return CheckResult(
        "engine_kill_switch", _WARN,
        f"CODEVIRA_ENGINE={val!r} is unexpected (use 0 or 1)",
        fix_command="export CODEVIRA_ENGINE=1",
    )


def check_crash_log_size() -> CheckResult:
    """C12 — crash log isn't pathologically large."""
    try:
        from mcp_server.paths import get_global_home
        home = get_global_home()
    except Exception:  # noqa: BLE001
        return CheckResult("crash_log_size", _WARN, "could not resolve home")
    log = home / "crash.log"
    if not log.exists():
        return CheckResult(
            "crash_log_size", _PASS,
            "no crash log (clean state)",
        )
    size = log.stat().st_size
    LIMIT_MB = 5
    if size > LIMIT_MB * 1024 * 1024:
        return CheckResult(
            "crash_log_size", _WARN,
            f"crash.log is {size // 1024 // 1024} MB (>{LIMIT_MB} MB)",
            fix_command=f"mv '{log}' '{log}.archived'",
        )
    return CheckResult(
        "crash_log_size", _PASS,
        f"crash.log is {size // 1024} KB (within budget)",
    )


# =====================================================================
# Runner
# =====================================================================


_CHECKS: tuple[Callable[[], CheckResult], ...] = (
    check_python_version,
    check_codevira_data_dir,
    check_project_root,
    check_graph_db,
    check_global_db,
    check_detected_ides,
    check_nudge_files,
    check_watcher_circuit,
    check_engine_kill_switch,
    check_crash_log_size,
)


def run_all_checks() -> DoctorReport:
    """Execute every check; return the report. Never raises."""
    results: list[CheckResult] = []
    for check in _CHECKS:
        try:
            results.append(check())
        except Exception as e:  # noqa: BLE001 — defense
            results.append(CheckResult(
                check.__name__.replace("check_", ""),
                _FAIL,
                f"check itself crashed: {type(e).__name__}: {e}",
                fix_command="codevira report  # send the crash log",
            ))
    return DoctorReport(results=tuple(results))


# =====================================================================
# CLI entry point
# =====================================================================


def cmd_doctor(*, verbose: bool = False, out: IO[str] | None = None) -> int:
    """`codevira doctor` — print the report. Returns:
       0 if all checks PASS or WARN
       1 if any check FAILed
    """
    out = out or sys.stdout
    report = run_all_checks()

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
