"""
uid.py — a record identity that is the same on every machine (4.0 Step 9 · S2).

``id`` (``D000120``) is minted as ``max(id)+1``. That is unique only within
one store: two engineers on two branches both mint ``D000120``, git merges
the appended lines with no conflict, and ``read_merged`` keys by id and
silently drops one. ``id_repair`` repairs that after the fact, but the
repair renumbers records — so an id is not a name you can hold onto.

``uid`` is ``sha256`` of the record's content. Two machines that hold the
same decision compute the same uid with zero coordination, and the uid does
not change when ``id_repair`` renumbers the record around it.

# Why not uuid4

Measured, because the plan asserted it and an assertion is not evidence::

    byte-identical pair      -> 1 record,  deduped=1
    same pair + uuid4 uid    -> 2 records, deduped=0   <- 1 decision became 2
    same pair + content uid  -> 1 record,  deduped=1

A cherry-pick or a double-commit puts the same decision in the store twice.
``id_repair`` recognises that today because the records are byte-identical.
Stamp a random uid on each and they stop being identical: the dedup misses,
and one decision permanently becomes two. Content-addressing is not a nicety
here — a random uid actively destroys a property the store already has.

# What is excluded from the hash, and why each one

- ``id`` — machine-local and rewritten by ``id_repair``. Including it would
  make the uid change during a repair, which is the whole thing uid exists
  to avoid.
- ``uid`` — obviously; a hash cannot include itself.
- Anything starting with ``_`` — internal bookkeeping (``_amendment_to_id``,
  ``_amendment_ambiguous``, ``_reference_ambiguous``). All of it is either
  derived or rewritten by the repair.
- ``superseded_by`` / ``supersedes`` — repointed by ``id_repair`` when their
  target is renumbered (Step 9 · S3a). A record's identity must not change
  because something it points AT moved.

Everything else is in, including ``ts`` and ``origin``: the same sentence
recorded by two people on two days is two decisions, not one.

# No backfill

Existing records have no ``uid`` and are never rewritten to add one. They
do not need to be — ``uid_of()`` derives it from content, so a pre-4.0
record has exactly the same uid a 4.0 one would. This is a pure function of
data that is already on disk, not an assertion about the record, which is
why it is safe here where back-filling ``device_id`` was not: a device id
cannot be recovered from content, but a content hash is content.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: 64 bits. At 10k records the birthday probability is ~5e-12, and a
#: collision here would merge two distinct decisions, so the width is
#: chosen for that consequence rather than for display.
UID_WIDTH = 16

#: Fields excluded from the content hash. See the module docstring for why
#: each is here; the short version is that every one of them is either
#: machine-local or rewritten by ``id_repair``.
EXCLUDED = frozenset({"id", "uid", "superseded_by", "supersedes"})


def canonical_content(record: dict[str, Any]) -> str:
    """The bytes a uid is computed over. Exposed so tests can pin it."""
    payload = {
        k: v for k, v in record.items() if k not in EXCLUDED and not k.startswith("_")
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def compute(record: dict[str, Any]) -> str:
    """Content-derived uid. Pure — same record in, same uid out, forever."""
    digest = hashlib.sha256(canonical_content(record).encode("utf-8")).hexdigest()
    return digest[:UID_WIDTH]


def uid_of(record: dict[str, Any]) -> str:
    """The record's uid: stored if present, otherwise derived.

    This is what makes "no backfill" work. A pre-4.0 record has no stored
    uid, and gets the identical value a 4.0 record with the same content
    would — so uid-keyed lookups span both eras without touching a file.

    Prefers the STORED value when there is one so that a record whose
    content is later edited by hand keeps the identity its edges point at,
    rather than silently becoming a different record.
    """
    if not isinstance(record, dict):
        return ""
    stored = record.get("uid")
    if isinstance(stored, str) and stored:
        return stored
    return compute(record)
