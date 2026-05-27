"""
cli_export.py — `codevira export <target>` + shared backup helper.

2026-05-18 v2.1.2 Item 3:

Two entry points share one implementation:
  1. `codevira export decisions [--format json|sql] [--out PATH]`
     — standalone backup command. Closes Report 1 §7 gap.
  2. `_auto_export_before_destructive(...)` — called by `codevira reset`
     BEFORE any wipe of graph/ so decisions are never silently destroyed
     (Report 1 §3.2 + your earlier "heal is dangerous" feedback).

Format choices:
  - **json** (default): list of decision dicts. Human-readable, jq-friendly.
    Includes decisions + sessions + outcomes + preferences + learned_rules.
  - **sql**: full SQL dump via SQLite's `iterdump()`. Replay against an
    empty graph.db to fully reconstruct (preserves FK relationships).

Per-target tables exported:
  - `decisions` → always
  - `sessions` → always (FK parent for decisions)
  - `outcomes` → always (linked to decisions)
  - `preferences` → always
  - `learned_rules` → always
  - `phases` (roadmap) → always when present
  - `nodes`, `edges`, `symbols`, `call_edges`, `file_hashes` → only if --target=all

Failure-mode policy (P4 + P9):
  - Missing graph.db → clear error + non-zero exit
  - Malformed table → log warning, skip that table, continue
  - Output file unwritable → clear error before any partial write
"""

from __future__ import annotations

import json
import os
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Tables we always export when target=decisions (and they exist).
_DECISION_TABLES = (
    "sessions",
    "decisions",
    "outcomes",
    "preferences",
    "learned_rules",
    "phases",
)

# Tables included only when target=all (the full-state export).
_FULL_STATE_TABLES = (
    *_DECISION_TABLES,
    "nodes",
    "edges",
    "symbols",
    "call_edges",
    "file_hashes",
)


def _resolve_graph_db_path() -> Path:
    """Return the current project's graph.db path, validated to exist."""
    from mcp_server.paths import get_data_dir

    p = get_data_dir() / "graph" / "graph.db"
    if not p.is_file():
        raise FileNotFoundError(
            f"No graph.db at {p}. Has this project been initialized? "
            f"Run `codevira init` first."
        )
    return p


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    """Defensive check — schema may drift across codevira versions."""
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _dump_table_as_json(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    """Read every row of `table` and return a list of dicts. Safe to call
    on missing/malformed tables — returns [] and logs a warning to stderr.
    """
    if not _table_exists(conn, table):
        return []
    try:
        cur = conn.execute(f"SELECT * FROM {table}")
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            # 2026-05-18 v2.1.2 Item 5 alignment: coerce known bool-shaped
            # INTEGER columns to actual bool for cleaner JSON.
            for bool_col in ("do_not_revert", "is_public"):
                if bool_col in d:
                    d[bool_col] = bool(d[bool_col])
            rows.append(d)
        return rows
    except Exception as exc:  # noqa: BLE001
        print(
            f"[export] warning: failed to dump table {table!r}: {exc}",
            file=sys.stderr,
        )
        return []


def export_decisions_to_path(
    out_path: Path,
    *,
    format: str = "json",
    target: str = "decisions",
) -> dict[str, Any]:
    """Write export to `out_path`. Returns a summary dict.

    Args:
        out_path: destination file. Parent directory will be created.
        format: "json" or "sql".
        target: "decisions" (default — decisions+sessions+outcomes+prefs+
                rules+phases) or "all" (above + nodes/edges/symbols/etc).

    Returns: {"path": str, "format": format, "tables": {name: count}, "bytes": N}
    """
    if format not in ("json", "sql"):
        raise ValueError(f"format must be 'json' or 'sql', got {format!r}")
    if target not in ("decisions", "all"):
        raise ValueError(f"target must be 'decisions' or 'all', got {target!r}")

    graph_db_path = _resolve_graph_db_path()
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Open in read-only mode so a concurrent indexer write can't be blocked.
    # SQLite URI form is required for read-only.
    uri = f"file:{graph_db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row

    tables_to_dump = _DECISION_TABLES if target == "decisions" else _FULL_STATE_TABLES
    summary: dict[str, Any] = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_db": str(graph_db_path),
        "format": format,
        "target": target,
        "tables": {},
    }

    try:
        if format == "json":
            payload = {
                "schema_version": 1,
                "exported_at": summary["exported_at"],
                "source_db": str(graph_db_path),
                "target": target,
                "tables": {},
            }
            for table in tables_to_dump:
                rows = _dump_table_as_json(conn, table)
                payload["tables"][table] = rows
                summary["tables"][table] = len(rows)
            # v3.0.0 round-3: shared atomic-write helper (was a fixed
            # ``.tmp`` suffix — same race shape as the manifest bug).
            from mcp_server.storage.atomic import atomic_write_text

            atomic_write_text(
                out_path,
                json.dumps(payload, indent=2, default=str),
            )

        elif format == "sql":
            # SQL dump preserves schema + FK relationships. iterdump
            # yields every CREATE / INSERT in one stream — we stream
            # to a unique tmp file (mkstemp) and rename into place so
            # large exports don't have to fit in memory and concurrent
            # invocations can't race on the rename target.
            import tempfile as _tempfile

            out_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = _tempfile.mkstemp(
                prefix=f".{out_path.name}.",
                suffix=".tmp",
                dir=str(out_path.parent),
            )
            tmp = Path(tmp_name)
            with os.fdopen(fd, "w") as f:
                f.write(f"-- codevira export ({summary['exported_at']})\n")
                f.write(f"-- source: {graph_db_path}\n")
                f.write(f"-- target: {target}\n\n")
                f.write("BEGIN TRANSACTION;\n")
                for line in conn.iterdump():
                    # Filter by target: when target=decisions, skip
                    # CREATE/INSERT for tables not in our target list.
                    if target == "decisions":
                        skip = False
                        for tbl in (
                            "nodes",
                            "edges",
                            "symbols",
                            "call_edges",
                            "file_hashes",
                        ):
                            if (
                                f'TABLE "{tbl}"' in line
                                or f"TABLE {tbl}" in line
                                or f'INSERT INTO "{tbl}"' in line
                                or f"INSERT INTO {tbl}" in line
                            ):
                                skip = True
                                break
                        if skip:
                            continue
                    f.write(line + "\n")
                f.write("COMMIT;\n")
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(str(tmp), str(out_path))
            # For SQL dumps, table counts are not directly available;
            # estimate via re-counting from the source.
            for table in tables_to_dump:
                if _table_exists(conn, table):
                    try:
                        cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
                        summary["tables"][table] = cur.fetchone()[0]
                    except Exception:
                        summary["tables"][table] = 0
    finally:
        conn.close()

    try:
        summary["bytes"] = out_path.stat().st_size
    except OSError:
        summary["bytes"] = 0
    summary["path"] = str(out_path)
    return summary


def auto_export_before_destructive(target_kind: str) -> Path | None:
    """Called by `codevira reset` (and deprecated `heal --graph/--all`)
    BEFORE the destructive op. Writes a timestamped backup to
    `<data_dir>/exports/<timestamp>-pre-<target_kind>.json`.

    Returns the export path on success, None on any failure (P9: never
    block the user-initiated destructive op even if backup fails — but
    the calling cmd_reset will refuse to proceed without `--no-backup`
    in that case).
    """
    try:
        from mcp_server.paths import get_data_dir

        data_dir = get_data_dir()
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        exports_dir = data_dir / "exports"
        out_path = exports_dir / f"{ts}-pre-{target_kind}.json"
        summary = export_decisions_to_path(out_path, format="json", target="decisions")
        return Path(summary["path"])
    except FileNotFoundError:
        # No graph.db exists yet — nothing to back up. Not an error.
        return None
    except Exception as exc:  # noqa: BLE001
        print(
            f"[export] auto-backup failed: {exc}\n"
            f"  Continuing without backup. Pass --no-backup to suppress this attempt.",
            file=sys.stderr,
        )
        return None


# ─── CLI entry point ──────────────────────────────────────────────────────


def cmd_export(
    target: str = "decisions",
    *,
    fmt: str = "json",
    out: str | None = None,
    dry_run: bool = False,
) -> int:
    """`codevira export <target>` command.

    Returns POSIX exit code (0 = success, 1 = error, 2 = nothing to export).
    """
    if target not in ("decisions", "all"):
        print(
            f"Error: target must be 'decisions' or 'all', got {target!r}.",
            file=sys.stderr,
        )
        return 1
    if fmt not in ("json", "sql"):
        print(
            f"Error: --format must be 'json' or 'sql', got {fmt!r}.",
            file=sys.stderr,
        )
        return 1

    try:
        graph_db_path = _resolve_graph_db_path()
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        # v3.0 hardening: get_data_dir refuses invalid project roots
        # ($HOME, system top-levels). Run from a real project directory.
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if out is None:
        from mcp_server.paths import get_data_dir

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        ext = "json" if fmt == "json" else "sql"
        out_path = get_data_dir() / "exports" / f"{ts}-{target}.{ext}"
    else:
        out_path = Path(out).expanduser().resolve()

    if dry_run:
        print(f"  [dry-run] Would export {target} as {fmt} → {out_path}")
        print(f"  Source: {graph_db_path}")
        return 0

    try:
        summary = export_decisions_to_path(out_path, format=fmt, target=target)
    except Exception as exc:  # noqa: BLE001
        print(f"Error: export failed: {exc}", file=sys.stderr)
        return 1

    print(f"  ✓ Exported {target} as {fmt}")
    print(f"  Path: {summary['path']}")
    print(f"  Size: {summary['bytes']:,} bytes")
    print("  Tables:")
    for name, count in summary["tables"].items():
        print(f"    {name:<16} {count:>6} rows")
    return 0
