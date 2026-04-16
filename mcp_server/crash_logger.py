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
import re
import sys
import threading
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
    (re.compile(r'-----BEGIN\s+\w+\s+PRIVATE\s+KEY-----[\s\S]*?-----END\s+\w+\s+PRIVATE\s+KEY-----'), '***PRIVATE_KEY***'),
    # Connection strings with embedded passwords (postgres://, mongodb://, redis://, amqp://, etc.)
    (re.compile(r'(?i)(://[^:]*:)[^@]+(@)'), r'\1***@'),
    # Private/internal IP addresses (RFC 1918)
    (re.compile(r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b'), '***INTERNAL_IP***'),

    # ---- Generic keyword-value patterns (catch-all) ----

    # key=value / key: value / key = value (in text and logs)
    (re.compile(r'(?i)(api[_-]?key|token|secret|password|authorization|bearer|credential)\s*[:=]\s*\S+'), r'\1=***REDACTED***'),
    # JSON-style: "password": "value" or 'password': 'value'
    (re.compile(r"""(?i)(["'](?:password|secret|token|api_key|api-key|auth|credential)["'])\s*:\s*["'][^"']+["']"""), r'\1: "***REDACTED***"'),
    # .env file values for known secret variable names
    (re.compile(r'(?i)((?:DATABASE_URL|REDIS_URL|MONGO_URL|SECRET_KEY|PRIVATE_KEY|ACCESS_TOKEN|REFRESH_TOKEN|API_KEY|AUTH_TOKEN|ENCRYPTION_KEY)\s*=\s*)\S+'), r'\1***REDACTED***'),
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
        r'(?i)environ\s*\{[^}]{200,}\}',
        'environ{***REDACTED_ENV***}',
        text,
    )
    return text


def _get_log_dir() -> Path:
    """Return ~/.codevira/logs/, creating it if needed."""
    log_dir = Path.home() / ".codevira" / "logs"
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
            backupCount=3,             # 20 MB total history
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
        logger = _get_logger()
        tb = traceback.format_exception(type(error), error, error.__traceback__)
        tb_text = "".join(tb)

        # Build structured log entry
        lines = [
            f"{'=' * 72}",
            f"CRASH: {type(error).__name__}: {error}",
            f"TIME:  {datetime.now(timezone.utc).isoformat()}",
        ]
        if context:
            lines.append(f"WHERE: {context}")
        if tool_name:
            lines.append(f"TOOL:  {tool_name}")
        if project_path:
            lines.append(f"PROJECT: {project_path}")

        # System info (non-sensitive)
        lines.append(f"PYTHON: {sys.version.split()[0]}")
        try:
            from importlib.metadata import version as pkg_version
            lines.append(f"CODEVIRA: {pkg_version('codevira')}")
        except Exception:
            pass

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
