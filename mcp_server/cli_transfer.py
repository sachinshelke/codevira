"""
cli_transfer.py — `codevira export setup` + `codevira import` (machine transfer).

v3.3.0 (Phase 6): one command each way to move a codevira setup to a new
machine, replacing the manual three-step procedure documented in FAQ.md
("How do I back up Codevira's memory or move it to a new machine?").

Archive layout (tar.gz):
  codevira-setup.json          ← manifest: schema, provenance, contents
  .codevira/**                 ← the canonical project memory, verbatim
  global/preferences.jsonl     ← ~/.codevira/global.db global_preferences rows
  global/rules.jsonl           ← ~/.codevira/global.db global_rules rows

Deliberately NOT included:
  - ~/.codevira/projects/**    → derived indexes, rebuilt from source
  - global.db `projects` table → registry rows key on absolute paths and
    are machine-specific; every project re-registers itself on first use,
    so transferring them only creates ghost entries. Skipping the table
    entirely removes the path-remap problem.
  - .codevira-cache/**         → per-machine, rebuildable by design

Import merges global learning through GlobalDB.upsert_preference /
upsert_rule (existing frequency/UNIQUE merge semantics) rather than
overwriting the destination global.db — the new machine's own learning
survives.

Degradation (P9): no global.db on export → project memory still bundles,
global section is skipped with a notice. No .codevira/ AND no global.db →
clear error, nothing written.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MANIFEST_NAME = "codevira-setup.json"
_MANIFEST_SCHEMA = 1
_GLOBAL_PREFS_ARC = "global/preferences.jsonl"
_GLOBAL_RULES_ARC = "global/rules.jsonl"

# P5: refuse archives with absurd member counts — a codevira setup is a
# handful of JSONL/YAML files, not tens of thousands of entries.
_MAX_MEMBERS = 10_000


# ─── export ───────────────────────────────────────────────────────────────


def _read_global_learning() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read (preferences, rules) rows from ~/.codevira/global.db.

    Returns ([], []) when the db doesn't exist or can't be read — the
    caller reports the skip; it never aborts the project-memory export.
    """
    from mcp_server.paths import get_global_db_path

    db_path = get_global_db_path()
    if not db_path.is_file():
        return [], []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            prefs = [
                dict(r)
                for r in conn.execute(
                    "SELECT category, signal, example, frequency,"
                    " source_projects FROM global_preferences"
                ).fetchall()
            ]
            rules = [
                dict(r)
                for r in conn.execute(
                    "SELECT rule_text, confidence, source_projects,"
                    " category, language FROM global_rules"
                ).fetchall()
            ]
        finally:
            conn.close()
        return prefs, rules
    except sqlite3.Error as exc:
        print(
            f"[transfer] warning: could not read global.db ({exc}); "
            f"exporting project memory only.",
            file=sys.stderr,
        )
        return [], []


def cmd_export_setup(*, out: str | None = None, dry_run: bool = False) -> int:
    """`codevira export setup` — bundle .codevira/ + global learning.

    Returns POSIX exit code (0 success, 1 error, 2 nothing to export).
    """
    from mcp_server.paths import get_project_root
    from mcp_server.storage.paths import codevira_dir

    project_root = get_project_root()
    cv_dir = codevira_dir(project_root)
    prefs, rules = _read_global_learning()

    if not cv_dir.is_dir() and not prefs and not rules:
        print(
            "Error: nothing to export — no .codevira/ in this project and "
            "no global learning in ~/.codevira/global.db. "
            "Run `codevira init` and record some memory first.",
            file=sys.stderr,
        )
        return 2

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if out is None:
        out_path = project_root / f"codevira-setup-{ts}.tar.gz"
    else:
        out_path = Path(out).expanduser().resolve()

    manifest = {
        "schema_version": _MANIFEST_SCHEMA,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "source_project_root": str(project_root),
        "has_project_memory": cv_dir.is_dir(),
        "global_preferences": len(prefs),
        "global_rules": len(rules),
    }

    if dry_run:
        print(f"  [dry-run] Would write {out_path}")
        print(f"  Project memory: {cv_dir if cv_dir.is_dir() else '(none)'}")
        print(f"  Global learning: {len(prefs)} preference(s), {len(rules)} rule(s)")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=str(out_path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as tar:
            _add_bytes(tar, _MANIFEST_NAME, json.dumps(manifest, indent=2))
            if cv_dir.is_dir():
                tar.add(str(cv_dir), arcname=".codevira", recursive=True)
            if prefs:
                _add_bytes(
                    tar,
                    _GLOBAL_PREFS_ARC,
                    "\n".join(json.dumps(p) for p in prefs) + "\n",
                )
            if rules:
                _add_bytes(
                    tar,
                    _GLOBAL_RULES_ARC,
                    "\n".join(json.dumps(r) for r in rules) + "\n",
                )
        os.replace(str(tmp), str(out_path))
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        print(
            f"Error: could not write archive at {out_path}: {exc}. "
            f"Check disk space and directory permissions.",
            file=sys.stderr,
        )
        return 1

    print("  ✓ Exported codevira setup")
    print(f"  Path: {out_path}")
    print(f"  Size: {out_path.stat().st_size:,} bytes")
    print(f"  Project memory: {'included' if cv_dir.is_dir() else 'none'}")
    print(f"  Global learning: {len(prefs)} preference(s), {len(rules)} rule(s)")
    print("  On the new machine: codevira import <archive>")
    print("  (Don't commit the archive — it may contain other projects' learning.)")
    return 0


def _add_bytes(tar: tarfile.TarFile, arcname: str, text: str) -> None:
    import io

    data = text.encode("utf-8")
    info = tarfile.TarInfo(name=arcname)
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


# ─── import ───────────────────────────────────────────────────────────────


def _safe_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    """Validate archive members against path traversal (P4).

    Only plain files/dirs whose normalized paths stay inside the
    extraction root are allowed. Links, devices, absolute paths and
    ``..`` segments are rejected loudly — a tampered archive should
    fail, not half-extract.
    """
    members = tar.getmembers()
    if len(members) > _MAX_MEMBERS:
        raise ValueError(
            f"archive has {len(members)} members (limit {_MAX_MEMBERS}) — "
            f"this is not a codevira setup archive"
        )
    safe: list[tarfile.TarInfo] = []
    for m in members:
        name = m.name
        if name.startswith("/") or name.startswith("\\"):
            raise ValueError(f"absolute path in archive: {name!r}")
        parts = Path(name).parts
        if ".." in parts:
            raise ValueError(f"path traversal in archive: {name!r}")
        if not (m.isfile() or m.isdir()):
            raise ValueError(f"unsupported member type in archive: {name!r}")
        safe.append(m)
    return safe


def _merge_global_learning(
    prefs: list[dict[str, Any]], rules: list[dict[str, Any]]
) -> tuple[int, int]:
    """Merge imported rows into ~/.codevira/global.db via GlobalDB upserts.

    Reuses upsert_preference/upsert_rule so frequency aggregation and
    UNIQUE conflicts follow the same semantics as organic learning.
    Returns (prefs_merged, rules_merged).
    """
    from indexer.global_db import GlobalDB
    from mcp_server.paths import get_global_db_path

    db = GlobalDB(get_global_db_path())
    merged_p = merged_r = 0
    try:
        for row in prefs:
            try:
                sources = json.loads(row.get("source_projects") or "[]")
            except json.JSONDecodeError:
                sources = []
            source = sources[0] if sources else "imported"
            db.upsert_preference(
                category=row.get("category") or "uncategorized",
                signal=row.get("signal") or "",
                example=row.get("example"),
                source_project=source,
                frequency=int(row.get("frequency") or 1),
            )
            merged_p += 1
        for row in rules:
            if not row.get("rule_text"):
                continue
            try:
                sources = json.loads(row.get("source_projects") or "[]")
            except json.JSONDecodeError:
                sources = []
            source = sources[0] if sources else "imported"
            db.upsert_rule(
                rule_text=row["rule_text"],
                confidence=float(row.get("confidence") or 0.5),
                source_project=source,
                category=row.get("category"),
                language=row.get("language"),
            )
            merged_r += 1
    finally:
        db.close()
    return merged_p, merged_r


def cmd_import_setup(
    archive: str, *, force: bool = False, dry_run: bool = False
) -> int:
    """`codevira import <archive>` — restore a setup archive.

    Returns POSIX exit code (0 success, 1 error).
    """
    from mcp_server.paths import get_project_root
    from mcp_server.storage.paths import codevira_dir

    archive_path = Path(archive).expanduser().resolve()
    if not archive_path.is_file():
        print(
            f"Error: archive not found at {archive_path}. "
            f"Pass the path produced by `codevira export setup`.",
            file=sys.stderr,
        )
        return 1

    project_root = get_project_root()
    cv_dir = codevira_dir(project_root)

    try:
        with tarfile.open(archive_path, mode="r:gz") as tar:
            members = _safe_members(tar)
            names = {m.name for m in members}
            if _MANIFEST_NAME not in names:
                print(
                    f"Error: {archive_path.name} has no {_MANIFEST_NAME} — "
                    f"not a codevira setup archive. "
                    f"Create one with `codevira export setup`.",
                    file=sys.stderr,
                )
                return 1
            manifest_fh = tar.extractfile(_MANIFEST_NAME)
            manifest = json.loads(manifest_fh.read()) if manifest_fh else {}
            if manifest.get("schema_version") != _MANIFEST_SCHEMA:
                print(
                    f"Error: archive schema_version "
                    f"{manifest.get('schema_version')!r} is not supported "
                    f"(expected {_MANIFEST_SCHEMA}). It may come from a "
                    f"newer codevira — upgrade this machine first.",
                    file=sys.stderr,
                )
                return 1

            has_memory = any(
                n == ".codevira" or n.startswith(".codevira/") for n in names
            )
            prefs = _read_jsonl_member(tar, _GLOBAL_PREFS_ARC)
            rules = _read_jsonl_member(tar, _GLOBAL_RULES_ARC)

            if dry_run:
                print(f"  [dry-run] Would import from {archive_path.name}")
                print(f"  Exported: {manifest.get('exported_at', 'unknown')}")
                print(f"  Source: {manifest.get('source_project_root', 'unknown')}")
                print(f"  Project memory: {'yes' if has_memory else 'no'}")
                print(
                    f"  Global learning: {len(prefs)} preference(s), "
                    f"{len(rules)} rule(s)"
                )
                if cv_dir.is_dir() and any(cv_dir.iterdir()) and has_memory:
                    print("  ⚠ .codevira/ exists here — import needs --force")
                return 0

            backup_dir: Path | None = None
            if has_memory:
                if cv_dir.is_dir() and any(cv_dir.iterdir()):
                    if not force:
                        print(
                            f"Error: {cv_dir} already exists and is not empty. "
                            f"Importing would overwrite this project's memory. "
                            f"Re-run with --force to proceed (the existing "
                            f"directory is backed up first).",
                            file=sys.stderr,
                        )
                        return 1
                    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                    backup_dir = project_root / f".codevira.pre-import-{ts}"
                    cv_dir.replace(backup_dir)
                # Python ≥3.12: the stdlib 'data' filter adds a second
                # layer of traversal/metadata protection on top of
                # _safe_members (which remains the only guard on 3.10/3.11).
                extract_kwargs: dict[str, Any] = (
                    {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
                )
                tar.extractall(
                    path=str(project_root),
                    members=[
                        m
                        for m in members
                        if m.name == ".codevira" or m.name.startswith(".codevira/")
                    ],
                    **extract_kwargs,
                )
    except (tarfile.TarError, ValueError, json.JSONDecodeError) as exc:
        print(
            f"Error: cannot read {archive_path.name}: {exc}. "
            f"The file may be corrupted — re-create it with "
            f"`codevira export setup` on the source machine.",
            file=sys.stderr,
        )
        return 1

    merged_p = merged_r = 0
    if prefs or rules:
        merged_p, merged_r = _merge_global_learning(prefs, rules)

    print("  ✓ Imported codevira setup")
    print(
        f"  Project memory: {'restored to ' + str(cv_dir) if has_memory else 'none in archive'}"
    )
    if backup_dir is not None:
        print(f"  Previous .codevira/ backed up to: {backup_dir}")
    print(f"  Global learning merged: {merged_p} preference(s), {merged_r} rule(s)")
    print("  Next: run `codevira init` to re-register IDE hooks and rebuild the index.")
    return 0


def _read_jsonl_member(tar: tarfile.TarFile, arcname: str) -> list[dict[str, Any]]:
    """Read a JSONL member from the archive; missing member → []."""
    try:
        fh = tar.extractfile(arcname)
    except KeyError:
        return []
    if fh is None:
        return []
    rows: list[dict[str, Any]] = []
    for line in fh.read().decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # skip bad line, keep the rest (P4)
        if isinstance(row, dict):
            rows.append(row)
    return rows
