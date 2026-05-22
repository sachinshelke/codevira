"""
jsonl_store.py — atomic append-only writer + line-by-line reader.

The foundation of v2.2.0's in-repo storage. Every decision, outcome,
session, changeset, preference, and learned rule lives in a JSONL file
under ``.codevira/`` in the project repo. This module provides the
read/write primitives.

Design principles:

1. **Append-only writes.** Each record is ONE line ending in ``\\n``.
   Writes are atomic at the line level (write+fsync inside an exclusive
   file lock). Concurrent appenders never interleave bytes.

2. **No mid-line edits.** "Mutations" (e.g., `mark_decision_protected`,
   `supersede_decision`) append an AMENDMENT line that references the
   original by id. Readers apply amendments in order. This keeps git
   diffs clean and merges trivial.

3. **Lock granularity = per file.** ``fcntl.flock`` (posix) / a
   sentinel-file pattern (Windows). Critical section is the
   append-then-fsync, which is sub-millisecond for normal record sizes.

4. **Bad lines logged, not raised.** A user edit that corrupts one
   line shouldn't kill reads of the other 999. Malformed lines emit
   a warning via ``logger`` and are skipped. ``codevira doctor`` flags
   the file so the user can manually repair.

5. **No schema enforcement here.** This module is shape-agnostic — it
   handles dicts. Schema validation lives in the per-record-type
   wrappers (``decisions.py``, ``outcomes.py``, etc., added in Phase B).

6. **UTF-8 throughout.** Decisions are human text; emoji, accents,
   Cyrillic, CJK all round-trip.

History note: in v2.1.x we used SQLite for all of this. The git-diff
hostility of binary blobs + the ChromaDB HNSW corruption pattern
pushed us to plain text. JSONL gives us 99% of SQLite's benefits with
none of the corruption surface and full git-friendliness.
"""

from __future__ import annotations

import io
import json
import logging
import os
from pathlib import Path
from typing import Any, Iterator

# v3.0.0 round-3: the file-lock helpers moved to
# ``mcp_server.storage.atomic`` so every write site in codevira shares
# one implementation (Posix flock + Windows sentinel fallback). This
# module imports the canonical version; the ``_file_lock`` private
# alias preserves the symbol any internal caller used previously.
from mcp_server.storage.atomic import file_lock as _file_lock

logger = logging.getLogger(__name__)


def append(path: Path, record: dict[str, Any]) -> None:
    """Append one record to a JSONL file. Atomic at the line level.

    Raises:
        OSError on disk-full / permission errors.
        TypeError if record contains non-JSON-serializable values.
    """
    # Serialize FIRST (so we hold the lock as briefly as possible).
    # ``ensure_ascii=False`` preserves UTF-8 (emoji, CJK, etc.) in the
    # on-disk text — easier to grep/read for humans.
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if "\n" in line:
        # Defensive: should be impossible with json.dumps, but a custom
        # serializer in the future could break our line-per-record contract.
        raise ValueError("jsonl_store.append: record serialization contains newline")
    blob = (line + "\n").encode("utf-8")

    with _file_lock(path, exclusive=True):
        # Open in append+binary so we don't read the whole file just to
        # add one line. Standard append-mode write is itself atomic on
        # POSIX for writes ≤PIPE_BUF (4096 bytes), but the flock above
        # guarantees correctness regardless of size.
        with open(path, "ab") as fh:
            fh.write(blob)
            # fsync the data so a crash mid-write doesn't lose the line.
            # Costs ~1ms per write but a power loss after record_decision
            # returning OK would be a bigger problem than the latency.
            fh.flush()
            os.fsync(fh.fileno())


def append_many(path: Path, records: list[dict[str, Any]]) -> None:
    """Append many records as one batched fsync. Use for bulk ops.

    All records succeed together or the file is unchanged (best-effort —
    a power loss mid-batch may leave a partial trailing line, which
    ``read_all`` will skip with a warning).
    """
    if not records:
        return

    lines: list[bytes] = []
    for r in records:
        line = json.dumps(r, ensure_ascii=False, separators=(",", ":"))
        if "\n" in line:
            raise ValueError(
                "jsonl_store.append_many: record serialization contains newline"
            )
        lines.append((line + "\n").encode("utf-8"))

    blob = b"".join(lines)
    with _file_lock(path, exclusive=True):
        with open(path, "ab") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())


def read_all(path: Path) -> list[dict[str, Any]]:
    """Read every record from a JSONL file.

    Bad lines are logged and skipped (resilience over strictness).
    Returns an empty list if the file doesn't exist (callers don't need
    to check existence first).
    """
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    # Shared lock so concurrent writers don't interleave reads with
    # half-written lines (defensive — we fsync per-write anyway).
    with _file_lock(path, exclusive=False):
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.rstrip("\n")
                if not raw:
                    continue  # blank line, tolerate
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "jsonl_store.read_all: %s:%d malformed JSON (skipping): %s",
                        path,
                        lineno,
                        exc,
                    )
                    continue
                if not isinstance(rec, dict):
                    logger.warning(
                        "jsonl_store.read_all: %s:%d record is not an object "
                        "(skipping): %r",
                        path,
                        lineno,
                        type(rec).__name__,
                    )
                    continue
                out.append(rec)
    return out


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    """Iterate records one at a time. Useful for large files.

    Same bad-line policy as ``read_all`` — skip + log.
    """
    if not path.is_file():
        return
    with _file_lock(path, exclusive=False):
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                raw = raw.rstrip("\n")
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "jsonl_store.iter_records: %s:%d malformed JSON (skipping): %s",
                        path,
                        lineno,
                        exc,
                    )
                    continue
                if not isinstance(rec, dict):
                    continue
                yield rec


def count(path: Path) -> int:
    """Count records without parsing JSON. Fast for size-budget checks.

    Counts non-empty lines (so blank lines / trailing-newline don't skew).
    """
    if not path.is_file():
        return 0
    n = 0
    with _file_lock(path, exclusive=False):
        with open(path, "rb") as fh:
            for raw in fh:
                if raw.strip():
                    n += 1
    return n


def last_id(path: Path, *, id_field: str = "id") -> str | None:
    """Return the ``id_field`` of the LAST record (for monotonic ID gen).

    Tail-reads the file (cheap on small files; uses seek-from-end for
    larger ones). Returns None if file is empty or doesn't exist.
    """
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None

    # For small files, just read_all (simpler + correct).
    if size < 100_000:  # ~100 KB
        records = read_all(path)
        if not records:
            return None
        last = records[-1]
        val = last.get(id_field)
        return str(val) if val is not None else None

    # For larger files, seek-from-end and scan backwards for the last
    # newline so we only parse one line.
    with _file_lock(path, exclusive=False):
        with open(path, "rb") as fh:
            # Read the last 4 KB; that's enough for one record + safety.
            chunk_size = min(4096, size)
            fh.seek(-chunk_size, io.SEEK_END)
            tail = fh.read()
    lines = tail.splitlines()
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict):
            val = rec.get(id_field)
            return str(val) if val is not None else None
    return None


def next_monotonic_id(
    path: Path, *, prefix: str = "D", width: int = 6, id_field: str = "id"
) -> str:
    """Generate the next monotonic ID for a JSONL store.

    Format: ``<prefix><zero-padded-base36>`` (e.g. ``D000001``).
    Base 36 → ~2.1 billion IDs in 6 digits. Plenty for any single
    project; if exceeded, falls back to wider IDs (``D1ZZZZZ`` etc.).

    Reads the LAST record's id_field and increments. Collision-safe
    only INSIDE the same flock; callers MUST hold the file lock if they
    plan to read-then-write (use ``append_with_generated_id`` for the
    atomic version).
    """
    last = last_id(path, id_field=id_field)
    if last is None or not last.startswith(prefix):
        return f"{prefix}{'0' * (width - 1)}1"

    suffix = last[len(prefix) :]
    try:
        n = int(suffix, 36)
    except ValueError:
        # Old format we don't recognize — start fresh.
        return f"{prefix}{'0' * (width - 1)}1"
    n += 1
    encoded = _to_base36(n)
    if len(encoded) <= width:
        return f"{prefix}{encoded.rjust(width, '0')}"
    return f"{prefix}{encoded}"  # overflowed width — accept wider ID


def _to_base36(n: int) -> str:
    """Encode a non-negative int as upper-case base36."""
    if n == 0:
        return "0"
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    out: list[str] = []
    while n > 0:
        out.append(chars[n % 36])
        n //= 36
    return "".join(reversed(out))


def append_with_generated_id(
    path: Path,
    record: dict[str, Any],
    *,
    prefix: str = "D",
    width: int = 6,
    id_field: str = "id",
) -> str:
    """Atomically: read last id → increment → set record[id_field] → append.

    Returns the generated ID. Holds the file lock for the entire
    read-modify-write so concurrent callers always get distinct IDs.
    """
    line: bytes
    new_id: str

    with _file_lock(path, exclusive=True):
        # Compute next ID under the lock so two concurrent callers can't
        # both compute "D000005" and lose one record.
        # Inline the last_id + increment logic to avoid re-locking.
        new_id = _compute_next_id_locked(
            path, prefix=prefix, width=width, id_field=id_field
        )
        record_copy = dict(record)
        record_copy[id_field] = new_id

        line_str = json.dumps(record_copy, ensure_ascii=False, separators=(",", ":"))
        if "\n" in line_str:
            raise ValueError(
                "jsonl_store.append_with_generated_id: serialization contains newline"
            )
        line = (line_str + "\n").encode("utf-8")

        with open(path, "ab") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())

    return new_id


def _compute_next_id_locked(
    path: Path, *, prefix: str, width: int, id_field: str
) -> str:
    """Compute next ID inside an already-held file lock. Internal."""
    if not path.is_file() or path.stat().st_size == 0:
        return f"{prefix}{'0' * (width - 1)}1"

    # Tail-read (no re-lock).
    size = path.stat().st_size
    if size < 100_000:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    else:
        with open(path, "rb") as fh:
            fh.seek(-4096, io.SEEK_END)
            tail = fh.read()
        lines = [
            line + "\n" for line in tail.decode("utf-8", errors="ignore").splitlines()
        ]

    last_val: str | None = None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            val = rec.get(id_field)
            if val is not None:
                last_val = str(val)
                break

    if last_val is None or not last_val.startswith(prefix):
        return f"{prefix}{'0' * (width - 1)}1"
    try:
        n = int(last_val[len(prefix) :], 36) + 1
    except ValueError:
        return f"{prefix}{'0' * (width - 1)}1"
    encoded = _to_base36(n)
    if len(encoded) <= width:
        return f"{prefix}{encoded.rjust(width, '0')}"
    return f"{prefix}{encoded}"
