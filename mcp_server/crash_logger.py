"""
crash_logger.py — Crash-only logging with automatic secret sanitization.

Captures unhandled exceptions and tool-level errors to a rotating log file.
All log entries are sanitized to strip personal information, secrets, and
credentials before writing to disk.

Log location: ~/.codevira/logs/crashes.log
Max size: 5 MB, rotated with 3 backups (20 MB total)
"""

from __future__ import annotations

import logging
import hashlib
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Crash rate-limiting (P5 bounded resources + P10 observability).
#
# 2026-05-17 fix for the 41-crash UDAP spam pattern: ChromaDB's HNSW
# writer failed once, and the watcher retried every file in sequence,
# producing 41 identical entries in crashes.log. The log grew unbounded;
# the user had to wade through 41 stack traces to find the one root cause.
#
# Fix: dedupe by (exception type, first line of message) within a time
# window. After RATE_LIMIT_MAX entries with the same signature, suppress
# further writes for RATE_LIMIT_WINDOW seconds and replace with a single
# "(N more duplicates suppressed)" summary at window expiry.
# ---------------------------------------------------------------------------

_RATE_LIMIT_MAX = 3  # entries per signature before suppression kicks in
_RATE_LIMIT_WINDOW = 60.0  # seconds — duplicates within this window are coalesced

# In-memory state — thread-safe via _RATE_LIMIT_LOCK below.
# Maps signature → (first_seen_ts, count_since_first_seen, suppressed_count)
_recent_crashes: dict[str, list[float]] = {}
_RATE_LIMIT_LOCK = threading.Lock()


def _crash_signature(error: BaseException) -> str:
    """Build a stable signature for rate-limiting. Same root cause → same key.

    Uses (exception class) + (first line of str(error)) — robust against
    tracebacks varying due to threading / async wrappers.
    """
    msg = str(error)
    first_line = msg.split("\n", 1)[0][:200]  # cap at 200 chars
    return f"{type(error).__name__}:{first_line}"


def crash_fingerprint(error: BaseException, *, version: str = "") -> str:
    """A stable 12-char fingerprint for a crash — same root cause on any
    machine → same key.

    Hashes the exception type + the normalized top stack frames (file
    basename + function name only; NOT line numbers, absolute paths, or
    runtime values, which vary across edits/machines) + the codevira
    ``major.minor``. This is the dedup key the crash digest groups by, and
    the key any future opt-in reporting would collapse duplicates on.
    Distinct from ``_crash_signature`` (the message-based key used for the
    60-second local rate-limit). Never raises.
    """
    try:
        frames = traceback.extract_tb(error.__traceback__)
        norm = [f"{Path(fr.filename).name}:{fr.name}" for fr in frames[-5:]]
        major_minor = ".".join(str(version).split(".")[:2]) if version else ""
        raw = f"{type(error).__name__}|{'>'.join(norm)}|{major_minor}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    except Exception:  # noqa: BLE001 — fingerprinting must never break logging
        return "unknown00000"


def _should_rate_limit(error: BaseException) -> tuple[bool, int]:
    """Decide whether to suppress this crash log.

    Returns:
        (suppress, suppressed_count)
          suppress: True → don't write; False → write
          suppressed_count: if write, how many duplicates were coalesced
                            (0 unless this is the unsuppression-summary write)
    """
    sig = _crash_signature(error)
    now = time.time()
    with _RATE_LIMIT_LOCK:
        # Drop signatures whose window has expired.
        for key in list(_recent_crashes.keys()):
            timestamps = _recent_crashes[key]
            cutoff = now - _RATE_LIMIT_WINDOW
            _recent_crashes[key] = [t for t in timestamps if t > cutoff]
            if not _recent_crashes[key]:
                del _recent_crashes[key]
        # Record this hit.
        timestamps = _recent_crashes.setdefault(sig, [])
        timestamps.append(now)
        count = len(timestamps)
        if count <= _RATE_LIMIT_MAX:
            return (False, 0)  # write normally
        # We're over the limit. Suppress, unless this is exactly the
        # MAX+1th hit (in which case we write a one-time "rate-limit
        # engaged" notice). Subsequent hits silently increment count.
        if count == _RATE_LIMIT_MAX + 1:
            # Write a notice that further entries are being suppressed.
            return (False, -1)  # sentinel: write rate-limit notice
        return (True, 0)


# ---------------------------------------------------------------------------
# Secret patterns — matched and replaced BEFORE writing to disk.
#
# CodeVira is a code-indexing/memory tool. Crash tracebacks may contain
# file paths, config values, or connection strings from the user's project.
# We sanitize structural patterns (connection strings, key=value, PEM blocks)
# rather than vendor-specific token formats — CodeVira never handles those.
# ---------------------------------------------------------------------------
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Private key PEM blocks
    (
        re.compile(
            r"-----BEGIN\s+\w+\s+PRIVATE\s+KEY-----[\s\S]*?-----END\s+\w+\s+PRIVATE\s+KEY-----"
        ),
        "***PRIVATE_KEY***",
    ),
    # Connection strings with embedded passwords (postgres://, mongodb://, redis://, amqp://, etc.)
    (re.compile(r"(?i)(://[^:]*:)[^@]+(@)"), r"\1***@"),
    # Private/internal IP addresses (RFC 1918)
    (
        re.compile(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b"
        ),
        "***INTERNAL_IP***",
    ),
    # ---- Generic keyword-value patterns (catch-all) ----
    # key=value / key: value / key = value (in text and logs)
    (
        re.compile(
            r"(?i)(api[_-]?key|token|secret|password|authorization|bearer|credential)\s*[:=]\s*\S+"
        ),
        r"\1=***REDACTED***",
    ),
    # JSON-style: "password": "value" or 'password': 'value'
    (
        re.compile(
            r"""(?i)(["'](?:password|secret|token|api_key|api-key|auth|credential)["'])\s*:\s*["'][^"']+["']"""
        ),
        r'\1: "***REDACTED***"',
    ),
    # .env file values for known secret variable names
    (
        re.compile(
            r"(?i)((?:DATABASE_URL|REDIS_URL|MONGO_URL|SECRET_KEY|PRIVATE_KEY|ACCESS_TOKEN|REFRESH_TOKEN|API_KEY|AUTH_TOKEN|ENCRYPTION_KEY)\s*=\s*)\S+"
        ),
        r"\1***REDACTED***",
    ),
]

# Home directory — replaced with ~ in all paths
_HOME = str(Path.home())


def _sanitize(text: str) -> str:
    """Remove secrets, PII, and sensitive data from log text."""
    # Replace home directory with ~ (hides username from paths)
    text = text.replace(_HOME, "~")
    # Apply all secret patterns
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    # Strip environment variable dumps that might contain secrets
    text = re.sub(
        r"(?i)environ\s*\{[^}]{200,}\}",
        "environ{***REDACTED_ENV***}",
        text,
    )
    return text


def _get_log_dir() -> Path:
    """Return <global_home>/logs/, creating it if needed.

    Uses get_global_home() so tests (which patch it to a tmp dir) don't
    pollute the real ~/.codevira/logs/crashes.log file.
    """
    from mcp_server.paths import get_global_home

    log_dir = get_global_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


# ---------------------------------------------------------------------------
# Module-level logger — initialized once, thread-safe via lock.
# ---------------------------------------------------------------------------
_logger: logging.Logger | None = None
_logger_lock = threading.Lock()


def _get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    with _logger_lock:
        # Double-check after acquiring lock (another thread may have initialized)
        if _logger is not None:
            return _logger

        logger = logging.getLogger("codevira.crash")
        logger.setLevel(logging.ERROR)
        logger.propagate = False  # Don't pollute stdout/stderr (MCP protocol)

        log_path = _get_log_dir() / "crashes.log"
        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,  # 20 MB total history
            encoding="utf-8",
        )
        handler.setLevel(logging.ERROR)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

        _logger = logger
        return _logger


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def log_crash(
    error: BaseException,
    *,
    context: str = "",
    tool_name: str = "",
    project_path: str = "",
) -> None:
    """
    Log a crash to ~/.codevira/logs/crashes.log.

    Only writes ERROR-level entries. All content is sanitized before writing.

    Args:
        error: The exception that was raised.
        context: What was happening when the crash occurred (e.g. "server startup").
        tool_name: MCP tool name if the crash happened during a tool call.
        project_path: Project directory, if known.
    """
    try:
        # P5 (bounded resources): rate-limit identical crashes so a stuck
        # watcher can't fill the log with 41 copies of the same error.
        suppress, sentinel = _should_rate_limit(error)
        if suppress:
            return  # silently drop — log already has _RATE_LIMIT_MAX copies

        logger = _get_logger()
        tb = traceback.format_exception(type(error), error, error.__traceback__)
        tb_text = "".join(tb)

        # Build structured log entry
        lines = [
            f"{'=' * 72}",
            f"CRASH: {type(error).__name__}: {error}",
            f"TIME:  {datetime.now(timezone.utc).isoformat()}",
        ]
        if sentinel == -1:
            # This is the "further duplicates suppressed" notice.
            lines.append(
                f"NOTE:  rate-limit engaged for this signature — further "
                f"identical crashes within {_RATE_LIMIT_WINDOW:.0f}s will be silently dropped"
            )
        if context:
            lines.append(f"WHERE: {context}")
        if tool_name:
            lines.append(f"TOOL:  {tool_name}")
        if project_path:
            lines.append(f"PROJECT: {project_path}")

        # System info (non-sensitive)
        lines.append(f"PYTHON: {sys.version.split()[0]}")
        _cv_version = ""
        try:
            from importlib.metadata import version as pkg_version

            _cv_version = pkg_version("codevira")
            lines.append(f"CODEVIRA: {_cv_version}")
        except Exception:
            pass

        # Stable cross-machine dedup key — surfaced by `doctor` and ready for
        # any future opt-in reporting (groups duplicates of one root cause).
        lines.append(f"FINGERPRINT: {crash_fingerprint(error, version=_cv_version)}")

        lines.append(f"TRACEBACK:\n{tb_text}")
        lines.append("")  # blank line separator

        entry = "\n".join(lines)
        entry = _sanitize(entry)

        logger.error(entry)
    except Exception:
        # Crash logger must never itself crash the server.
        # Last resort: try stderr (won't break MCP protocol since
        # MCP uses stdout only; stderr is for diagnostics).
        try:
            print(f"[codevira] crash logger failed: {error}", file=sys.stderr)
        except Exception:
            pass


def get_crash_log_path() -> Path:
    """Return the path to the crash log file."""
    return _get_log_dir() / "crashes.log"


def read_recent_crashes(limit: int = 20) -> str:
    """
    Read the most recent crash entries from the log file.

    Returns a formatted string with up to `limit` most recent crashes.
    Content is already sanitized (written sanitized to disk).
    """
    if limit < 1:
        limit = 1

    log_path = get_crash_log_path()
    if not log_path.exists():
        return "No crash log found. No crashes have been recorded."

    text = log_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return "Crash log is empty. No crashes have been recorded."

    # Split on separator line and take the most recent entries
    entries = text.split("=" * 72)
    entries = [e.strip() for e in entries if e.strip()]

    if not entries:
        return "No crash entries found."

    recent = entries[-limit:]
    count_total = len(entries)

    # Sanitize the header too (log_path contains home dir)
    safe_path = str(log_path).replace(_HOME, "~")
    header = f"Showing {len(recent)} of {count_total} total crashes"
    header += f"\nLog file: {safe_path}\n"
    header += f"Log size: {log_path.stat().st_size / 1024:.1f} KB\n"

    body = ("\n" + "=" * 72 + "\n").join(recent)
    return f"{header}\n{'=' * 72}\n{body}"


def crash_digest() -> dict:
    """Summarize the crash log for ``doctor``: total entries, distinct
    fingerprints (falls back to distinct exception types for legacy entries
    written before FINGERPRINT existed), the most-recent crash type, and the
    on-disk size in KB. One cheap read; never raises.
    """
    out: dict = {"total": 0, "distinct": 0, "recent_type": None, "size_kb": 0.0}
    try:
        log_path = get_crash_log_path()
        if not log_path.exists():
            return out
        text = log_path.read_text(encoding="utf-8", errors="replace")
        entries = [e for e in text.split("=" * 72) if e.strip()]
        out["total"] = len(entries)
        out["size_kb"] = round(log_path.stat().st_size / 1024, 1)
        fingerprints: set[str] = set()
        types: set[str] = set()
        for entry in entries:
            for line in entry.splitlines():
                if line.startswith("FINGERPRINT:"):
                    fingerprints.add(line.split(":", 1)[1].strip())
                elif line.startswith("CRASH:"):
                    # "CRASH: <ExcType>: <msg>" → ExcType
                    t = line.split(":", 1)[1].strip().split(":", 1)[0].strip()
                    types.add(t)
                    out["recent_type"] = t  # chronological file → last wins
        out["distinct"] = len(fingerprints) if fingerprints else len(types)
    except Exception:  # noqa: BLE001 — the digest must never break doctor
        pass
    return out


def install_global_handler() -> None:
    """
    Install a global exception handler that logs unhandled exceptions.

    Call this once at MCP server startup. The original excepthook is
    preserved — this only adds logging, it doesn't suppress errors.
    """
    original_hook = sys.excepthook

    def _crash_hook(exc_type, exc_value, exc_tb):
        if exc_type is not KeyboardInterrupt:
            log_crash(
                exc_value,
                context="unhandled exception (global handler)",
            )
        original_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _crash_hook
