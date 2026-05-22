"""
atomic.py — canonical atomic-write + cross-process file-lock helpers.

This module is the single source of truth for "write a file safely" in
codevira's product surface. Every storage / tool / cli module that
touches the on-disk product state goes through here so we don't ship
four hand-rolled copies of the same os.replace dance.

# Why this module exists

The v3.0.0 round-2 G5 audit caught two distinct race shapes:

1. **Atomic-write race.** Several modules used a fixed
   ``<path>.tmp`` suffix; two threads racing on ``os.replace`` saw
   ``FileNotFoundError: <path>.tmp`` because thread A consumed the
   tmp before thread B's rename. Fixed inline in three places
   (manifest, digest, agents_md_generator).

2. **Lost-update race.** Read-modify-write paths (manifest.yaml,
   roadmap.yaml) had no lock; concurrent updates dropped 13 of 50
   writes on a stress run.

Round-3 collapsed those inline fixes into this helper so:
- Every write site has the same crash-safety contract.
- A future write site can't silently regress by forgetting one piece
  of the dance (fsync, same-fs tmp, ownership transfer).
- The Posix vs Windows split is in ONE place (was previously in
  jsonl_store; now shared).

# Contract

``atomic_write_text(path, content)``:
  - Writes ``content`` to a unique temp in ``path.parent`` (via
    ``tempfile.mkstemp``).
  - ``fsync`` if supported (graceful fallback if not).
  - ``os.replace(tmp, path)`` — atomic on Posix, near-atomic on
    Windows (single replace vs unlink-then-rename).
  - Cleans up the tmp on failure.
  - Returns bytes written.

``atomic_write_bytes(path, content)``: same, for binary content.

``file_lock(path, exclusive=True)``: context manager that holds an
OS-level advisory lock on ``path``. Posix uses ``fcntl.flock``;
Windows uses a sentinel ``.lock`` file with a 5-second retry. Falls
back to the in-process threading.Lock on filesystems that don't
support either (graceful — preserves the v2.x best-effort contract).

# Anti-goals

- Not a transaction. Two callers using ``file_lock`` MUST both
  acquire it; this is advisory, not mandatory.
- Not a queue. If 100 writers contend, they serialize but no
  fairness guarantee.
- Not a substitute for ``jsonl_store.append`` for append-only logs —
  that path already uses ``file_lock`` internally for the append.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)


# =====================================================================
# atomic_write_text / atomic_write_bytes
# =====================================================================


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    mode: int | None = None,
) -> int:
    """Atomically write ``content`` (str) to ``path``.

    Strategy: write to a unique temp file in the SAME directory (so
    the rename is on the same filesystem and ``os.replace`` is
    atomic), fsync if supported, then ``os.replace`` into place.

    Per-write unique tmp via ``tempfile.mkstemp`` — fixes the
    fixed-suffix race where two threads' renames collided on
    ``<path>.tmp``.

    Args:
        path: target file path.
        content: string to write.
        encoding: passed through to ``str.encode``. Defaults to utf-8.
        mode: optional permission bits (e.g. ``0o600`` for secrets).
            Applied via ``os.chmod`` AFTER the rename so the final
            file has the requested mode.

    Returns:
        Number of bytes written.

    Raises:
        OSError on disk-full / permission errors. The tmp file is
        cleaned up before propagating.
    """
    return atomic_write_bytes(path, content.encode(encoding), mode=mode)


def atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    mode: int | None = None,
) -> int:
    """Atomically write ``content`` (bytes) to ``path``. See ``atomic_write_text``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path: str | None = raw_tmp
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # Some filesystems (tmpfs, certain container mounts,
                # network mounts) don't support fsync. Fallback: the
                # os.replace below is still atomic; we just lose the
                # "data hit the disk before the rename" guarantee.
                pass
        os.replace(tmp_path, path)
        tmp_path = None  # ownership transferred — finally block skips cleanup
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError as exc:
                # chmod failure (e.g. on Windows or restrictive
                # filesystems) is not fatal — log and continue.
                logger.warning(
                    "atomic_write: chmod %o on %s failed: %s", mode, path, exc
                )
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return len(content)


# =====================================================================
# file_lock
# =====================================================================


# Per-process in-memory lock, keyed by absolute path string. Two
# threads in the same process MUST serialize even if the OS-level lock
# doesn't (fcntl.flock is per-OS-file-descriptor on macOS, not
# per-process, so two file_lock() calls in the same process can both
# acquire LOCK_EX simultaneously without this).
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


def _process_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


@contextlib.contextmanager
def file_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    """Acquire an OS-level advisory lock on ``path``.

    POSIX: ``fcntl.flock`` (LOCK_EX or LOCK_SH).
    Windows: sentinel ``.lock`` file with 5-second retry.

    The path is touched if it doesn't exist (the lock needs something
    to anchor to). Acquires the per-process in-memory lock first so
    two threads in the same process can't both pass through the
    OS-level lock (which on macOS is per-fd, not per-process).

    Use as::

        with file_lock(path):
            # read-modify-write here
            ...
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        proc_lock = _process_lock_for(path)
        with proc_lock:
            if not path.exists():
                path.touch()

    proc_lock = _process_lock_for(path)
    proc_lock.acquire()
    fd: int | None = None
    try:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
        if sys.platform == "win32":
            # Windows fallback: sentinel file with retry loop.
            sentinel = path.with_suffix(path.suffix + ".lock")
            acquired = False
            for _ in range(50):  # ~5s total
                try:
                    sfd = os.open(str(sentinel), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                    os.close(sfd)
                    acquired = True
                    break
                except FileExistsError:
                    time.sleep(0.1)
            if not acquired:
                logger.warning(
                    "atomic.file_lock: could not acquire windows sentinel "
                    "on %s after 5s; proceeding without lock (writers may race)",
                    sentinel,
                )
            try:
                yield
            finally:
                if acquired:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(str(sentinel))
        else:
            import fcntl

            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            try:
                fcntl.flock(fd, mode)
            except OSError as exc:
                # Some filesystems (NFS in some configs, fuse mounts)
                # don't support flock. Log and proceed — the in-process
                # lock still serializes intra-process writers.
                logger.warning(
                    "atomic.file_lock: flock on %s failed (%s); "
                    "proceeding without OS-level lock",
                    path,
                    exc,
                )
                try:
                    yield
                    return
                finally:
                    pass
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        proc_lock.release()
