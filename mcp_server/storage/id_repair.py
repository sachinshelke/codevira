"""
id_repair.py — deterministic detection + repair of base-record ID collisions
in append-only JSONL stores shared across engineers (v3.7.0, Phase 25 Tier-0).

Why this exists
---------------
``decisions.jsonl`` (and ``skills.jsonl`` …) mint ids as ``max(id)+1`` — unique
only WITHIN one machine. Two engineers on two branches both mint ``D000120``;
``git merge`` combines the two appended lines cleanly (no conflict) and
``jsonl_store.read_merged`` then keys by id and SILENTLY overwrites one
(``by_id[did] = rec``) — a lost decision, no error.

This module turns that silent data loss into a deterministic, convergent
repair. ``normalize`` is a pure function whose loser ids are derived from
record CONTENT, so every machine computes byte-identical output and the
repair is a fixed point::

    normalize(normalize(x)) == normalize(x)

That fixed-point property is what stops "continuously colliding" — a re-merge
can only surface collisions from genuinely NEW records, never oscillate.

Contract
--------
- A *base* record has no truthy ``amendment_field``. Two base records sharing
  an id is the collision we repair (amendments legitimately reuse a base id
  and are exempt).
- Among colliding base records, the WINNER keeps the id, chosen by a total
  order every machine computes identically: ``(ts, origin.host_hash,
  content_hash)`` — the final content hash guarantees a strict order even when
  ts and host collide.
- Byte-identical records are the SAME decision (a cherry-pick / double-commit)
  and are DEDUPED, not renumbered.
- LOSERS are renumbered to a content-derived id ``D<sha1(content)[:12]>`` — a
  pure function of content, so convergence needs zero shared state.
- Amendments follow their base when unambiguous (same old id + same
  ``origin.host_hash``); otherwise they stay with the winner (never guessed).

This is Tier-0 (structural, deterministic). Tier-1 (semantic dedup / conflict
escalation) layers on top via the reconcile engine — see Phase 29.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from typing import Any

# Width (hex chars) of the content-derived loser id. 12 hex = 48 bits; with a
# few thousand decisions the birthday-collision probability is negligible, and
# critically the width is FIXED (not adapted to the local id set) so two
# machines always mint the same loser id — the convergence guarantee.
_LOSER_HASH_WIDTH = 12

# A ts that sorts AFTER any real ISO-8601 timestamp, so a record missing ``ts``
# loses the "earliest writer keeps the id" race instead of winning it.
_TS_SENTINEL = "~"


def _canonical(record: dict[str, Any], *, exclude: tuple[str, ...] = ()) -> str:
    """Stable JSON encoding of a record, optionally excluding fields."""
    filtered = {k: v for k, v in record.items() if k not in exclude}
    return json.dumps(filtered, sort_keys=True, ensure_ascii=False, default=str)


def _content_hash(
    record: dict[str, Any], *, id_field: str, amendment_field: str
) -> str:
    """sha1 of the record's content EXCLUDING id-carrying fields.

    Excluding the id fields is what makes the loser id stable under
    renumbering: re-normalizing a record whose id already changed yields the
    same hash, so ``normalize`` is idempotent.
    """
    canon = _canonical(record, exclude=(id_field, amendment_field))
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()


def _host(record: dict[str, Any]) -> str:
    origin = record.get("origin") or {}
    if isinstance(origin, dict):
        return str(origin.get("host_hash") or "")
    return ""


def _order_key(
    record: dict[str, Any], *, id_field: str, amendment_field: str
) -> tuple[str, str, str]:
    """Total order for picking the winner. Smallest wins (earliest writer)."""
    ts = str(record.get("ts") or "") or _TS_SENTINEL
    return (
        ts,
        _host(record),
        _content_hash(record, id_field=id_field, amendment_field=amendment_field),
    )


def _mint_loser_id(
    record: dict[str, Any],
    claimed: set[str],
    *,
    id_field: str,
    amendment_field: str,
    prefix: str,
) -> str:
    """Content-derived id for a renumbered loser that is UNIQUE against
    ``claimed`` (all surviving base ids + already-minted loser ids).

    Starts at ``_LOSER_HASH_WIDTH`` hex of the content hash and widens the
    slice until the id isn't already claimed. Still a pure function of content
    + the (deterministic) claimed set, so two machines normalizing the same
    merged store mint identical ids — and because every id ends up unique,
    ``normalize`` is a true fixed point (a fresh loser id can never re-open a
    base-id collision in ``read_merged``).
    """
    h = _content_hash(record, id_field=id_field, amendment_field=amendment_field)
    for w in range(_LOSER_HASH_WIDTH, len(h) + 1):
        cand = f"{prefix}{h[:w]}"
        if cand not in claimed:
            return cand
    # Astronomically unlikely (full 40-hex sha1 already claimed): disambiguate.
    n = 0
    while f"{prefix}{h}-{n}" in claimed:
        n += 1
    return f"{prefix}{h}-{n}"


def find_collisions(
    records: list[dict[str, Any]],
    *,
    id_field: str = "id",
    amendment_field: str = "_amendment_to_id",
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{id: [base records sharing it]}`` for ids with >1 BASE record.

    Amendments (truthy ``amendment_field``) legitimately reuse a base id and
    are never counted. This is pure detection — no mutation.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rid = str(rec.get(id_field) or "")
        if not rid or rec.get(amendment_field):
            continue
        groups[rid].append(rec)
    return {rid: recs for rid, recs in groups.items() if len(recs) > 1}


def normalize(
    records: list[dict[str, Any]],
    *,
    id_field: str = "id",
    amendment_field: str = "_amendment_to_id",
    prefix: str = "D",
) -> dict[str, Any]:
    """Deterministically repair base-id collisions. Pure and idempotent.

    Returns ``{"records", "remap", "collisions", "deduped"}``:
      - ``records``: the repaired record list (input order preserved, exact
        duplicates dropped, losers renumbered, amendments followed).
      - ``remap``: list of ``{old_id, new_id, loser_host}`` for each renumbered
        loser — enough for a caller to surface the change / flag ambiguous
        references (which this Tier-0 pass never rewrites).
      - ``collisions``: number of colliding ids found.
      - ``deduped``: number of byte-identical duplicate records dropped.
    """
    recs = [dict(r) for r in records if isinstance(r, dict)]

    # Index base records by id.
    base_idxs: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(recs):
        rid = str(r.get(id_field) or "")
        if not rid or r.get(amendment_field):
            continue
        base_idxs[rid].append(i)

    dropped: set[int] = set()
    reassign: dict[int, str] = {}  # index -> new id
    remap: list[dict[str, Any]] = []
    collisions = 0

    # Every surviving base id keeps its value (winners + singletons), so a
    # minted loser id must avoid ALL of them — plus every other minted loser.
    claimed: set[str] = {
        str(r.get(id_field) or "")
        for r in recs
        if not r.get(amendment_field) and str(r.get(id_field) or "")
    }

    loser_idxs: list[int] = []
    winner_host_by_id: dict[str, str] = {}  # old_id -> host of the base that KEPT it
    for rid, idxs in base_idxs.items():
        if len(idxs) <= 1:
            continue
        collisions += 1

        # Dedup byte-identical records (same decision recorded twice).
        seen: dict[str, int] = {}
        distinct: list[int] = []
        for i in idxs:
            canon = _canonical(recs[i])
            if canon in seen:
                dropped.add(i)
            else:
                seen[canon] = i
                distinct.append(i)
        if len(distinct) <= 1:
            continue

        # Winner (smallest order key) keeps the id; the rest are losers.
        distinct.sort(
            key=lambda i: _order_key(
                recs[i], id_field=id_field, amendment_field=amendment_field
            )
        )
        winner_host_by_id[rid] = _host(recs[distinct[0]])
        loser_idxs.extend(distinct[1:])

    # Mint loser ids in a DETERMINISTIC global order (by content hash, then old
    # id) against the shared claimed set, so two machines converge identically.
    loser_idxs.sort(
        key=lambda i: (
            _content_hash(recs[i], id_field=id_field, amendment_field=amendment_field),
            str(recs[i].get(id_field) or ""),
        )
    )
    for loser in loser_idxs:
        old_id = str(recs[loser].get(id_field) or "")
        new_id = _mint_loser_id(
            recs[loser],
            claimed,
            id_field=id_field,
            amendment_field=amendment_field,
            prefix=prefix,
        )
        claimed.add(new_id)
        reassign[loser] = new_id
        remap.append(
            {"old_id": old_id, "new_id": new_id, "loser_host": _host(recs[loser])}
        )

    # M6: an amendment follows its renumbered loser base ONLY when the
    # (old_id, host) pair unambiguously identifies exactly one loser — i.e. a
    # single renumbered loser has that host AND the winner (which keeps old_id)
    # does NOT share the host, AND the host is non-empty. Otherwise the
    # attribution is a guess (host-less amendment, or winner+loser / two losers
    # sharing a host): leave the amendment on the WINNER (it keeps old_id) and
    # FLAG it, so nothing silently moves is_protected/is_outdated onto the wrong
    # engineer's decision. (The docstring promised "never guessed".)
    loser_keys: dict[tuple[str, str], list[str]] = defaultdict(list)
    for idx, new_id in reassign.items():
        loser_keys[(str(recs[idx].get(id_field) or ""), _host(recs[idx]))].append(
            new_id
        )
    follow: dict[tuple[str, str], str] = {}
    ambiguous_keys: set[tuple[str, str]] = set()
    for (old, host), new_ids in loser_keys.items():
        unambiguous = (
            bool(host) and len(new_ids) == 1 and winner_host_by_id.get(old) != host
        )
        if unambiguous:
            follow[(old, host)] = new_ids[0]
        else:
            ambiguous_keys.add((old, host))

    ambiguous_amendments = 0
    out: list[dict[str, Any]] = []
    for i, r in enumerate(recs):
        if i in dropped:
            continue
        if i in reassign:
            r = dict(r)
            r[id_field] = reassign[i]
            out.append(r)
            continue
        if r.get(amendment_field):
            key = (str(r.get(amendment_field)), _host(r))
            if key in follow:
                r = dict(r)
                r[id_field] = follow[key]
                r[amendment_field] = follow[key]
                out.append(r)
                continue
            if key in ambiguous_keys or (
                str(r.get(amendment_field)) in {k[0] for k in loser_keys}
                and not _host(r)
            ):
                # Its base id was split among renumbered losers but we can't
                # attribute this amendment — keep it on the winner, flag it.
                r = dict(r)
                r["_amendment_ambiguous"] = True
                ambiguous_amendments += 1
                out.append(r)
                continue
        out.append(dict(r))

    return {
        "records": out,
        "remap": remap,
        "collisions": collisions,
        "deduped": len(dropped),
        "ambiguous_amendments": ambiguous_amendments,
    }
