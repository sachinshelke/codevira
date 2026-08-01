"""
memory_undo.py — a way back for ``.codevira/`` (4.0 Step 10).

This is a **blocking prerequisite** for any migration or backfill 4.0 might
offer, and the reason is one line in ``.gitignore``::

    .gitignore:61:  .codevira/

The memory store is gitignored. So the rollback path every developer
reaches for first — ``git revert``, ``git checkout --``, ``git stash`` —
does not exist for the one directory that holds a team's decision history.
Before 4.0 offers to rewrite anything in there, there has to be an answer
to "and if that goes wrong?" that is not "restore from a backup you did
not know to take".

# What it snapshots

Only ``.codevira/`` — the canonical, non-rebuildable store. Not
``.codevira-cache/``, not ``graph.db``, not the FTS5 index: those are
derived from the canonical files and ``codevira sync`` rebuilds them. A
snapshot that included them would be mostly noise, and restoring a stale
index over a fresh one would create a problem rather than undo one.

# Where snapshots live, and why not in the project

``~/.codevira/snapshots/<project-key>/<timestamp>/``.

Not inside ``.codevira/``: D00012K caps committed per-project state at
2 MB, and a directory of snapshots would blow through that while also
making every snapshot part of the thing being snapshotted. Keeping them
under the global home also means a rollback survives anything done to the
project's own directories, which is the point.

Specifically NOT anchored to ``get_data_dir()`` — for a legacy-layout
project that returns ``<project>/.codevira/`` itself, so the destination
would sit inside its own source and ``copytree`` would recurse until the
path overflowed. See :func:`_snapshots_dir`.

# The property that matters most

**Undo is itself undoable.** ``restore()`` snapshots the current state
before overwriting anything, so running undo by mistake — or discovering
the snapshot you picked was the wrong one — is recoverable. A rollback
tool that can destroy data is worse than no rollback tool, because people
trust it.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: Directory name under the global home (NOT the project — see module doc).
SNAPSHOTS_DIRNAME = "snapshots"

#: Written into every snapshot so a human (or a later version) can tell
#: what it was and why it was taken.
MANIFEST_NAME = "snapshot.json"

#: Keep this many per project. Old snapshots are pruned oldest-first.
#: Generous, because a decision store is small and the cost of pruning one
#: someone needed is unrecoverable.
MAX_SNAPSHOTS = 20


@dataclass(frozen=True)
class Snapshot:
    """One captured state of a project's ``.codevira/``."""

    path: Path
    taken_at: str
    note: str
    project_root: str
    files: int
    bytes: int

    @property
    def name(self) -> str:
        return self.path.name


def _snapshots_dir(project_root: Path | None = None) -> Path:
    """``~/.codevira/snapshots/<project-key>/``.

    Deliberately anchored to the GLOBAL home rather than ``get_data_dir()``.
    That function returns ``<project>/.codevira/`` for a legacy-layout
    project, which would put the snapshot destination INSIDE its own
    source: ``copytree`` then copies the snapshot into the snapshot until
    the path overflows. Found by running the real CLI on a real repo —
    every unit test passed, because the fixture monkeypatched
    ``get_data_dir`` to a separate tmp dir and so never exercised the
    layout that breaks.

    Keying on the project path also means a snapshot survives anything
    done to the project's own directories, which is the point.
    """
    from mcp_server.paths import _sanitize_path_key, get_global_home
    from mcp_server.storage import paths as spaths

    root = Path(project_root) if project_root else spaths.get_project_root()
    return get_global_home() / SNAPSHOTS_DIRNAME / _sanitize_path_key(root.resolve())


def _codevira_dir(project_root: Path | None = None) -> Path:
    from mcp_server.storage import paths as spaths

    return spaths.codevira_dir(project_root)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def take(
    project_root: Path | None = None,
    *,
    note: str = "",
) -> Snapshot | None:
    """Copy ``.codevira/`` into a timestamped snapshot. None if there is
    nothing to snapshot (an uninitialised project).

    Never raises: this runs immediately before destructive operations, and
    a snapshot failing must be reported to the caller as "no snapshot",
    not as an exception that leaves the caller unsure whether to proceed.
    """
    try:
        src = _codevira_dir(project_root)
        if not src.is_dir():
            return None

        root = Path(project_root).resolve() if project_root else src.parent
        dest_parent = _snapshots_dir(project_root)

        # Refuse to write a snapshot inside the thing being snapshotted.
        # copytree would then copy the snapshot into itself until the path
        # overflows — the failure this module hit for real. A hard check
        # rather than a comment, because the path resolution above is not
        # local to this file and can change underneath it.
        if (
            src.resolve() in dest_parent.resolve().parents
            or dest_parent.resolve() == src.resolve()
        ):
            logger.error(
                "memory_undo: refusing to snapshot into %s — it is inside %s",
                dest_parent,
                src,
            )
            return None

        dest_parent.mkdir(parents=True, exist_ok=True)

        # Collide-proof: two snapshots in the same second get -1, -2 ...
        base = _stamp()
        dest = dest_parent / base
        n = 1
        while dest.exists():
            dest = dest_parent / f"{base}-{n}"
            n += 1

        # Copy to a temp name and rename, so a crash mid-copy never leaves
        # a half-written snapshot that `list` would present as restorable.
        staging = dest_parent / f".{dest.name}.partial"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(src, staging, dirs_exist_ok=True)

        files = [p for p in staging.rglob("*") if p.is_file()]
        taken_at = datetime.now(timezone.utc).isoformat()
        clipped_note = note[:500]
        file_count = len(files)
        total_bytes = sum(p.stat().st_size for p in files)

        (staging / MANIFEST_NAME).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "taken_at": taken_at,
                    "note": clipped_note,
                    "project_root": str(root),
                    "files": file_count,
                    "bytes": total_bytes,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        staging.rename(dest)

        _prune(dest_parent)
        return Snapshot(
            path=dest,
            taken_at=taken_at,
            note=clipped_note,
            project_root=str(root),
            files=file_count,
            bytes=total_bytes,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_undo.take failed: %s", exc)
        return None


def _prune(dest_parent: Path) -> None:
    try:
        snaps = sorted(
            (
                p
                for p in dest_parent.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ),
            key=lambda p: p.name,
        )
        for old in snaps[:-MAX_SNAPSHOTS]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError as exc:
        logger.debug("memory_undo._prune: %s", exc)


def list_snapshots(project_root: Path | None = None) -> list[Snapshot]:
    """Newest first. Half-written snapshots are never listed."""
    out: list[Snapshot] = []
    try:
        d = _snapshots_dir(project_root)
        if not d.is_dir():
            return out
        for p in sorted(d.iterdir(), reverse=True):
            if not p.is_dir() or p.name.startswith("."):
                continue
            meta = _read_manifest(p)
            out.append(
                Snapshot(
                    path=p,
                    taken_at=str(meta.get("taken_at") or p.name),
                    note=str(meta.get("note") or ""),
                    project_root=str(meta.get("project_root") or ""),
                    files=int(meta.get("files") or 0),
                    bytes=int(meta.get("bytes") or 0),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory_undo.list_snapshots failed: %s", exc)
    return out


def _read_manifest(snapshot_dir: Path) -> dict:
    try:
        raw = (snapshot_dir / MANIFEST_NAME).read_text(encoding="utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def restore(
    project_root: Path | None = None,
    *,
    snapshot: str | None = None,
) -> dict:
    """Restore ``.codevira/`` from a snapshot. Returns a result dict.

    ``snapshot`` is a snapshot directory name; omitted means the most
    recent. The CURRENT state is snapshotted first (noted as a pre-restore
    safety capture), so an undo run by mistake is itself undoable — see
    the module docstring for why that is non-negotiable.
    """
    snaps = list_snapshots(project_root)
    if not snaps:
        return {
            "success": False,
            "error": "no snapshots exist for this project",
            "fix_command": "codevira memory snapshot",
        }

    if snapshot:
        chosen = next((s for s in snaps if s.name == snapshot), None)
        if chosen is None:
            return {
                "success": False,
                "error": f"no snapshot named {snapshot!r}",
                "fix_command": "codevira memory list",
            }
    else:
        chosen = snaps[0]

    # Snapshot the CURRENT state before touching it.
    safety = take(
        project_root, note=f"pre-restore safety capture (undoing {chosen.name})"
    )

    try:
        dest = _codevira_dir(project_root)
        staging = dest.parent / ".codevira.restoring"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

        # Build the new tree beside the old one, then swap. A copy
        # straight over `dest` would leave the store in a mixed state if
        # it failed halfway — the exact situation someone running `undo`
        # is already trying to get out of.
        shutil.copytree(chosen.path, staging)
        (staging / MANIFEST_NAME).unlink(missing_ok=True)

        previous = dest.parent / ".codevira.previous"
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        if dest.exists():
            dest.rename(previous)
        staging.rename(dest)
        shutil.rmtree(previous, ignore_errors=True)

        _invalidate_caches()
        return {
            "success": True,
            "restored": chosen.name,
            "taken_at": chosen.taken_at,
            "files": chosen.files,
            "safety_snapshot": safety.name if safety else None,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
            "safety_snapshot": safety.name if safety else None,
        }


def _invalidate_caches() -> None:
    """Derived state now describes a store that no longer exists."""
    try:
        from mcp_server.storage import decisions_store

        decisions_store.invalidate_merged_cache()
    except Exception as exc:  # noqa: BLE001
        logger.debug("memory_undo: cache invalidation skipped: %s", exc)
