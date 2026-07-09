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

7. **Schema versioning convention (v3.0.1+).** New JSONL stores
   introduced from v3.1.0 onwards (working, skills, activity,
   pending_conflicts, reflections) carry a top-level ``_schema_v``
   integer on each record (starting at ``1``). Readers MUST tolerate
   ``_schema_v`` absent (treats as v1) so legacy records keep working.
   The existing ``decisions.jsonl`` and ``sessions.jsonl`` schemas are
   UNCHANGED — they continue to read via field presence; no version
   field is retroactively added. This module is shape-agnostic and does
   not enforce versions; per-store wrappers may.

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
from collections.abc import Callable
from pathlib import Path
from typing import Any, Iterator

# v3.0.0 round-3: the file-lock helpers moved to
# ``mcp_server.storage.atomic`` so every write site in codevira shares
# one implementation (Posix flock + Windows sentinel fallback). This
# module imports the canonical version; the ``_file_lock`` private
# alias preserves the symbol any internal caller used previously.
from mcp_server.storage.atomic import atomic_write_text, file_lock as _file_lock

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
    """Compute next ID inside an already-held file lock. Internal.

    Returns ``max(real id) + 1``, scanning the FULL file — NOT the last /
    tail id. Computing the MAX is what makes minting collision-safe across
    rewrites: a store can be left out of id order by a junk cleanup,
    ``compact``, or a cross-project repair, which puts a LOWER id at the tail.

    Two pre-v3.6 bugs both came from trusting the tail:
      - **last-not-max**: ``last id + 1`` re-issued an id that already existed
        earlier in the file, silently clobbering that record (incl.
        ``do_not_revert`` decisions) in the merged view.
      - **tail-window**: for files >= 100 KB only the last 4 KB was read, so a
        burst of small amendment lines could hide every real id and the old
        code fell through to "D000001", colliding with the existing one.

    Amendment records (carrying ``_amendment_to_id``) re-use an existing id
    and are skipped. A full O(n) scan per append is acceptable here: these
    stores are bounded and ``append_with_generated_id`` already fsyncs.
    """
    if not path.is_file() or path.stat().st_size == 0:
        return f"{prefix}{'0' * (width - 1)}1"

    max_n = -1
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("_amendment_to_id") is not None:
                continue  # amendment — id is borrowed from an earlier record
            val = rec.get(id_field)
            if not isinstance(val, str) or not val.startswith(prefix):
                continue
            try:
                n = int(val[len(prefix) :], 36)
            except ValueError:
                continue
            if n > max_n:
                max_n = n

    if max_n < 0:
        return f"{prefix}{'0' * (width - 1)}1"
    encoded = _to_base36(max_n + 1)
    if len(encoded) <= width:
        return f"{prefix}{encoded.rjust(width, '0')}"
    return f"{prefix}{encoded}"


# =====================================================================
# v3.0.1: shared primitives for per-store wrappers (read_merged,
# compact, read_recent). Extracted from decisions_store._read_merged /
# sessions_store.read_recent so the five v3.1.0 memory subsystems
# (working, skills, activity, pending_conflicts, reflections) reuse
# one tested implementation instead of duplicating the amendment-
# overlay dance five times.
# =====================================================================


def read_merged(
    path: Path,
    *,
    id_field: str = "id",
    amendment_field: str = "_amendment_to_id",
) -> list[dict[str, Any]]:
    """Read a JSONL store and fold amendment lines into their base records.

    Convention (matches existing decisions.jsonl semantics):

    - **Base records** carry an ``id_field`` value and no truthy
      ``amendment_field``. They emit in the file's insertion order.
    - **Amendment records** carry the SAME ``id_field`` value as the
      base they amend, PLUS a truthy ``amendment_field`` value
      (typically equal to the same id — it is a marker, not a different
      pointer). Their fields overlay onto the base; later amendments
      win over earlier ones. Fields whose names start with ``"_"``
      are treated as metadata and are NOT overlaid (so the marker
      itself, and any future ``_promoted_to`` / ``_evicted`` style
      tombstones, do not leak into user-visible state).
    - **Orphan amendments** (amendment_field set but no matching base
      seen yet) are emitted as their own record so a misordered or
      truncated file can still be diagnosed by callers.
    - **Amendment chains** target the base directly. There is no
      recursive amendment-of-amendment semantic: every amendment must
      reference the base id. Tests cover three consecutive amendments
      to one base merging in order.

    Args:
        path: JSONL file path. Missing file returns ``[]``.
        id_field: schema field carrying the record id. Defaults to
            ``"id"`` for compatibility with decisions/sessions.
        amendment_field: schema field whose truthiness marks the
            record as an amendment. Defaults to ``"_amendment_to_id"``.

    Returns:
        List of merged record dicts in base-record insertion order.

    See ``decisions_store._read_merged`` (the original implementation)
    and ``mcp_server.storage.decisions_store`` for the canonical caller.
    """
    raw = read_all(path)
    by_id: dict[str, dict[str, Any]] = {}
    order: list[str] = []  # preserves insertion order of base records

    for rec in raw:
        did = str(rec.get(id_field, ""))
        if not did:
            continue
        if rec.get(amendment_field):
            # Overlay onto existing base, or emit as orphan.
            base = by_id.get(did)
            if base is None:
                # Orphan amendment — should not happen in a well-formed
                # file but don't crash; surface it for diagnosis.
                by_id[did] = dict(rec)
                order.append(did)
            else:
                base.update({k: v for k, v in rec.items() if not k.startswith("_")})
        else:
            # v3.7.0 (Phase 25): two BASE records sharing an id is a
            # cross-machine mint collision (see storage/id_repair). read_merged
            # would silently overwrite one — turning a lost decision into a
            # visible warning. Only warn on a genuine base-vs-base clash
            # (amendments legitimately reuse the id and are handled above).
            if did in by_id and not by_id[did].get(amendment_field):
                logger.warning(
                    "jsonl_store.read_merged: base-id collision on %r in %s — "
                    "one record is being shadowed. Run `codevira repair-ids` "
                    "(or let the git merge driver resolve it).",
                    did,
                    path.name,
                )
            if did not in by_id:
                order.append(did)
            by_id[did] = dict(rec)

    return [by_id[did] for did in order]


def compact(
    path: Path,
    *,
    keep_predicate: Callable[[dict[str, Any]], bool],
) -> int:
    """Atomically rewrite ``path`` keeping only records where
    ``keep_predicate(rec)`` returns True.

    Used for capacity-bounded stores (e.g. v3.1 working-memory
    eviction): after appending tombstone amendments throughout a
    session, ``compact`` drops the tombstoned records during
    ``codevira sync``.

    Concurrency: holds the exclusive file lock for the ENTIRE
    read-filter-write so no concurrent appender's record is lost in
    the read-vs-write window. ``atomic_write_text`` does not take the
    file lock itself (it relies on tempfile + os.replace for atomicity)
    so calling it inside this lock does not deadlock.

    Malformed lines are PRESERVED. This function's job is
    predicate-based filtering, not corruption cleanup — use
    ``codevira doctor`` for the latter so users don't silently lose
    data they could otherwise diagnose.

    Args:
        path: target JSONL file. Missing file returns ``0`` (no-op).
        keep_predicate: callable receiving each parsed dict; return
            True to keep, False to drop. Exceptions inside the
            predicate propagate (caller's bug, not silently swallowed).

    Returns:
        Number of records dropped.
    """
    if not path.is_file():
        return 0

    dropped = 0
    with _file_lock(path, exclusive=True):
        kept_lines: list[str] = []
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                stripped = raw.rstrip("\n")
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    # Preserve corrupt lines — compaction is filtering,
                    # not corruption cleanup.
                    kept_lines.append(stripped)
                    continue
                if not isinstance(rec, dict):
                    kept_lines.append(stripped)
                    continue
                if keep_predicate(rec):
                    kept_lines.append(stripped)
                else:
                    dropped += 1

        # Trailing newline only when there is content (matches append's
        # one-record-per-line + final-newline convention).
        new_content = ("\n".join(kept_lines) + "\n") if kept_lines else ""
        atomic_write_text(path, new_content)

    return dropped


def rewrite_all(path: Path, records: list[dict[str, Any]]) -> int:
    """Atomically replace the whole file with ``records`` (one JSON per line).

    v3.7.0 (Phase 25): used by ``decisions_store.repair_ids`` to write back the
    id-collision-repaired record list. Holds the exclusive file lock for the
    entire replace so a concurrent appender can't interleave, and writes via
    tempfile + os.replace (atomic). Serialization matches ``append`` exactly
    (compact separators, ensure_ascii=False, one line per record).
    """
    lines: list[str] = []
    for rec in records:
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        if "\n" in line:
            raise ValueError("jsonl_store.rewrite_all: serialization contains newline")
        lines.append(line)
    with _file_lock(path, exclusive=True):
        atomic_write_text(path, ("\n".join(lines) + "\n") if lines else "")
    return len(lines)


def read_recent(
    path: Path,
    *,
    limit: int,
    ts_field: str = "ts",
) -> list[dict[str, Any]]:
    """Return the most recent ``limit`` records, sorted by ``ts_field``
    descending (newest first).

    Records missing ``ts_field`` sort to the end (treated as empty
    string for ordering). Extracted from
    ``sessions_store.read_recent`` so v3.1 stores (working memory,
    reflections, activity) get the same behavior without copying the
    sort+slice dance.

    Args:
        path: JSONL file. Missing file returns ``[]``.
        limit: maximum number of records to return.
        ts_field: schema field carrying an ISO 8601 timestamp string.
            Defaults to ``"ts"``.

    Returns:
        List of dicts, newest first, length ``≤ limit``.
    """
    all_records = read_all(path)
    all_records.sort(key=lambda r: r.get(ts_field) or "", reverse=True)
    return all_records[:limit]
