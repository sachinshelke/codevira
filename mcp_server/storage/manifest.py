"""
manifest.py — tag/file → decision-id index.

``manifest.yaml`` is the fast-lookup index the relevance hook uses to
find candidate decisions for a prompt. Instead of scanning every
decision in digest.jsonl, the hook:

1. Extracts candidate tags + file paths from the prompt
2. Looks them up in the manifest (O(1) per tag/file)
3. Pulls the matching decision IDs
4. Loads only those decisions from digest.jsonl

This keeps the hook fast (<10ms) even on projects with thousands of
decisions.

The manifest is regenerable from decisions.jsonl. If it's missing or
corrupted, ``manifest.regenerate()`` rebuilds it cleanly.

We use YAML (not JSON) for the manifest because:
- It's human-editable for debugging
- The data is small (<10 KB typical); YAML's verbosity overhead is fine
- ``codevira doctor`` can pretty-print it
- Other yaml files in the project (config.yaml, roadmap.yaml,
  enforcement.yaml) use the same format, single dependency
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from mcp_server.storage import jsonl_store

_SCHEMA_VERSION = 1


def _empty_manifest() -> dict[str, Any]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_decisions": 0,
        "active_decisions": 0,
        "tags": {},
        "files": {},
        "do_not_revert_ids": [],
    }


def load(path: Path) -> dict[str, Any]:
    """Load manifest from ``path``. Returns empty manifest if missing."""
    if not path.is_file():
        return _empty_manifest()
    try:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except (yaml.YAMLError, OSError):
        return _empty_manifest()
    if not isinstance(data, dict):
        return _empty_manifest()

    # Normalize shape — fill missing keys with defaults so callers
    # don't have to None-guard everything.
    return {
        "schema_version": data.get("schema_version", _SCHEMA_VERSION),
        "generated_at": data.get("generated_at", _empty_manifest()["generated_at"]),
        "total_decisions": int(data.get("total_decisions", 0)),
        "active_decisions": int(data.get("active_decisions", 0)),
        "tags": dict(data.get("tags", {}) or {}),
        "files": dict(data.get("files", {}) or {}),
        "do_not_revert_ids": list(data.get("do_not_revert_ids", []) or []),
    }


def save(path: Path, manifest: dict[str, Any]) -> None:
    """Atomically write manifest to ``path`` (write-tmp + rename)."""
    manifest["generated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        # sort_keys=False so tags + files retain insertion order (more
        # readable diffs when only one new decision adds a single tag).
        # allow_unicode so emoji / accents in tag names don't get
        # \uXXXX-escaped.
        yaml.safe_dump(
            manifest,
            fh,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )
    tmp.replace(path)


def regenerate(decisions_path: Path, manifest_path: Path) -> dict[str, Any]:
    """Rebuild manifest from decisions.jsonl. Returns the new manifest."""
    decisions = jsonl_store.read_all(decisions_path)
    manifest = _empty_manifest()

    tags_map: dict[str, list[str]] = {}
    files_map: dict[str, list[str]] = {}
    do_not_revert: list[str] = []
    active_count = 0
    total = 0

    for d in decisions:
        total += 1
        # Skip superseded for active count + do_not_revert list, but
        # still count them in total_decisions.
        if d.get("is_superseded") or d.get("superseded_by"):
            continue
        active_count += 1

        did = str(d.get("id", ""))
        if not did:
            continue

        for tag in d.get("tags") or []:
            tag_str = str(tag).strip().lower()
            if not tag_str:
                continue
            tags_map.setdefault(tag_str, []).append(did)

        fp = d.get("file_path")
        if fp:
            files_map.setdefault(str(fp), []).append(did)

        if d.get("do_not_revert"):
            do_not_revert.append(did)

    # Sort each value list for deterministic output (cache-friendly).
    manifest["tags"] = {t: sorted(set(ids)) for t, ids in sorted(tags_map.items())}
    manifest["files"] = {f: sorted(set(ids)) for f, ids in sorted(files_map.items())}
    manifest["do_not_revert_ids"] = sorted(set(do_not_revert))
    manifest["total_decisions"] = total
    manifest["active_decisions"] = active_count

    save(manifest_path, manifest)
    return manifest


def incremental_add(manifest_path: Path, decision: dict[str, Any]) -> None:
    """Update manifest with one new decision (cheap, no full rebuild).

    Used by ``record_decision`` to keep the manifest in sync without
    re-scanning decisions.jsonl on every call. Idempotent if the
    decision ID is already present (avoids dup entries from retries).
    """
    manifest = load(manifest_path)
    did = str(decision.get("id", ""))
    if not did:
        return

    # If we already have this ID anywhere, skip (idempotent).
    for ids in manifest["tags"].values():
        if did in ids:
            return  # already indexed

    manifest["total_decisions"] += 1
    if not (decision.get("is_superseded") or decision.get("superseded_by")):
        manifest["active_decisions"] += 1

    for tag in decision.get("tags") or []:
        tag_str = str(tag).strip().lower()
        if not tag_str:
            continue
        bucket = manifest["tags"].setdefault(tag_str, [])
        if did not in bucket:
            bucket.append(did)
            bucket.sort()

    fp = decision.get("file_path")
    if fp:
        bucket = manifest["files"].setdefault(str(fp), [])
        if did not in bucket:
            bucket.append(did)
            bucket.sort()

    if decision.get("do_not_revert"):
        if did not in manifest["do_not_revert_ids"]:
            manifest["do_not_revert_ids"].append(did)
            manifest["do_not_revert_ids"].sort()

    save(manifest_path, manifest)
