"""
cli_repair.py — cross-engineer id-collision surfaces (v3.7.0, Phase 25).

Two commands + an installer:

  - ``codevira repair-ids [--apply]`` — detect (and optionally repair) base-id
    collisions in the decision log, delegating to the deterministic
    ``id_repair.normalize`` via ``decisions_store.repair_ids``.
  - ``codevira merge-driver <base> <ours> <theirs>`` — a git custom merge
    driver for the append-only decision log: unions both sides, drops exact
    duplicates, resolves id collisions deterministically, writes the result to
    ``<ours>``. Because the repair is a pure fixed point, both engineers'
    merges produce byte-identical output.
  - ``install_merge_driver`` — registers the driver (`.gitattributes` +
    `git config`) so the merge runs automatically on `git merge` / rebase.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def cmd_repair_ids(
    *, apply: bool = False, verbose: bool = False, semantic: bool = False
) -> int:
    from mcp_server.storage import decisions_store

    res = decisions_store.repair_ids(apply=apply)
    if not res["changed"]:
        print("  No decision-id collisions found. ✓")
    else:
        print(
            f"  Found {res['collisions']} colliding id(s) and "
            f"{res['deduped']} exact-duplicate record(s)."
        )
        if verbose or not apply:
            for m in res["remap"]:
                host = m.get("loser_host") or "?"
                print(
                    f"    {m['old_id']} → {m['new_id']}   "
                    f"(renumbered loser; host {host})"
                )
        if res["applied"]:
            print("  Repaired .codevira/decisions.jsonl + rebuilt indexes. ✓")
        else:
            print("  Dry run — re-run with `--apply` to rewrite decisions.jsonl.")

    if semantic:
        # Tier-1: surface near-duplicate decisions (different ids, similar text)
        # for review. Never auto-merged — the structural repair above is
        # authoritative; this only escalates.
        pairs = decisions_store.find_semantic_duplicates()
        if not pairs:
            print("  No semantic near-duplicate decisions found. ✓")
        else:
            print(
                f"\n  {len(pairs)} semantic near-duplicate pair(s) — review + "
                f"supersede_decision to merge (not auto-merged):"
            )
            for p in pairs:
                print(f"    {p['a_id']} ~ {p['b_id']}   (similarity {p['similarity']})")
    return 0


def _union_dedup(*record_lists: list[dict]) -> list[dict]:
    """Concatenate records, dropping byte-identical duplicates (order-stable).

    A record committed on BOTH branches shows up twice after a git union; we
    collapse those. Distinct records — including id collisions — are kept for
    ``id_repair.normalize`` to resolve.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for records in record_lists:
        for rec in records:
            canon = json.dumps(rec, sort_keys=True, ensure_ascii=False)
            if canon in seen:
                continue
            seen.add(canon)
            out.append(rec)
    return out


def _split_managed(text: str) -> tuple[str, str, str] | None:
    """Split AGENTS.md into (before, managed_block, after) around the markers.

    Returns None when the markers are absent or malformed — the caller then
    declines to auto-resolve rather than guessing at the structure.
    """
    from mcp_server.storage.agents_md_generator import _BEGIN_MARKER, _END_MARKER

    b = text.find(_BEGIN_MARKER)
    e = text.find(_END_MARKER)
    if b == -1 or e == -1 or e < b:
        return None
    return text[:b], text[b : e + len(_END_MARKER)], text[e + len(_END_MARKER) :]


def cmd_merge_driver_agents(base: str, ours: str, theirs: str) -> int:
    """git merge driver for AGENTS.md — regenerate, do not pick a winner.

    AGENTS.md is fully derived from the decision log, so a merge conflict in
    the codevira-managed block is noise: the block is a pure function of
    ``decisions.jsonl``, which merges correctly through its own driver. The
    right resolution is to regenerate, not to choose a side.

    But the file is only PART generated. Everything outside the markers is
    prose a human wrote, and that is not codevira's to discard. So this
    auto-resolves ONLY when both sides agree outside the block; if the human
    text diverges, it returns non-zero and lets git raise a real conflict for
    a real disagreement.

    Ignoring AGENTS.md instead (the fix used for digest/manifest) was rejected:
    it is the contract Codex and Copilot read WITHOUT running codevira, so
    dropping it from the repo would trade a cross-tool promise for a merge
    conflict.

    Returns 0 when resolved, 1 to let git report a conflict.
    """
    ours_p = Path(ours)
    try:
        a = _split_managed(ours_p.read_text(encoding="utf-8"))
        b = _split_managed(Path(theirs).read_text(encoding="utf-8"))
    except OSError:
        return 1
    if a is None or b is None:
        # No usable markers — this is not a shape we can reason about.
        return 1
    a_before, a_block, a_after = a
    b_before, b_block, b_after = b
    if (a_before, a_after) != (b_before, b_after):
        # Human prose diverged. Real conflict; codevira does not adjudicate it.
        return 1
    if a_block == b_block:
        return 0  # identical already; nothing to do

    # Same prose, different generated block: regenerate from the merged log.
    # regenerate() rewrites the managed block in place and preserves
    # everything outside the markers, which is exactly the contract here.
    try:
        from mcp_server.storage import agents_md_generator

        agents_md_generator.regenerate(target_path=ours_p)
        return 0
    except Exception as exc:  # noqa: BLE001 — a driver must not traceback
        logger.warning("AGENTS.md merge driver could not regenerate: %s", exc)
        # Fall back to OURS rather than conflicting: the block is derived, and
        # the next record_decision / `codevira sync` rewrites it anyway.
        ours_p.write_text(a_before + a_block + a_after, encoding="utf-8")
        return 0


def cmd_merge_driver(base: str, ours: str, theirs: str) -> int:
    """git custom merge driver for the append-only codevira decision log.

    git invokes ``merge-driver %O %A %B`` (base / ours / theirs) and reads the
    merged result from the ``ours`` path. We union ours+theirs, drop exact
    duplicates, run the deterministic id-collision repair, and write it back to
    ``ours``. Deterministic → both sides converge to identical bytes. Returns 0
    on a clean merge (this driver never reports a conflict — union always
    succeeds).
    """
    from mcp_server.storage import id_repair, jsonl_store

    ours_p = Path(ours)
    a, a_bad = jsonl_store.read_records_and_malformed(ours_p)
    b, b_bad = jsonl_store.read_records_and_malformed(Path(theirs))
    combined = _union_dedup(a, b)
    repaired = id_repair.normalize(combined)["records"]
    lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in repaired]
    # Preserve malformed lines from either side (deduped) so the merge never
    # silently drops unparseable data.
    for bad in [*a_bad, *b_bad]:
        if bad not in lines:
            lines.append(bad)
    ours_p.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    return 0


def install_merge_driver(project_root: Path) -> dict:
    """Register the codevira merge driver for this repo. Idempotent.

    Writes a ``.gitattributes`` entry and sets the ``merge.codevira-jsonl``
    git config so ``git merge`` / rebase resolve decision-log collisions
    automatically. No-op (and no error) outside a git repo. Returns
    ``{gitattributes, configured}``.
    """
    result: dict = {"gitattributes": None, "configured": False}
    if not (project_root / ".git").exists():
        return result

    ga = project_root / ".gitattributes"
    # AGENTS.md gets its own driver rather than an ignore: it is derived, but
    # other AI tools read it WITHOUT running codevira, so it must stay in the
    # repo. See cmd_merge_driver_agents.
    entries = [
        ".codevira/decisions.jsonl merge=codevira-jsonl",
        "AGENTS.md merge=codevira-agents",
    ]
    existing = ga.read_text() if ga.exists() else ""
    missing = [e for e in entries if e not in existing.splitlines()]
    if missing:
        with open(ga, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n# Codevira — deterministic merges for generated memory\n")
            for e in missing:
                f.write(f"{e}\n")
    result["gitattributes"] = str(ga)

    # Stage .gitattributes so a teammate who clones INHERITS the driver mapping
    # (the driver-command config below is per-machine local and can't be
    # committed — .gitattributes is the shareable half).
    try:
        subprocess.run(
            ["git", "-C", str(project_root), "add", ".gitattributes"],
            check=False,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError):
        pass

    try:
        r1 = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "config",
                "merge.codevira-jsonl.name",
                "Codevira decision-log merge",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        r2 = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "config",
                "merge.codevira-jsonl.driver",
                "codevira merge-driver %O %A %B",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        r3 = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "config",
                "merge.codevira-agents.name",
                "Codevira AGENTS.md regenerate-on-merge",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        r4 = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "config",
                "merge.codevira-agents.driver",
                "codevira merge-driver-agents %O %A %B",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        # Honest: only 'configured' when git actually accepted every write.
        result["configured"] = all(r.returncode == 0 for r in (r1, r2, r3, r4))
    except (FileNotFoundError, OSError):
        pass

    if result["configured"]:
        result["commit_hint"] = (
            "Commit .gitattributes so teammates inherit the decision-log merge "
            "driver (each teammate still runs `codevira init` once to configure "
            "the driver command locally)."
        )
    return result


def merge_driver_gap(project_root: Path) -> str | None:
    """Return a warning if ``.gitattributes`` references the codevira merge
    driver but THIS clone hasn't configured the driver command — the fresh-clone
    gap where a teammate would otherwise hit the exact hard merge conflict the
    driver exists to prevent. Returns None when there's no gap. Never raises.
    """
    ga = project_root / ".gitattributes"
    try:
        if not ga.is_file() or "merge=codevira-jsonl" not in ga.read_text():
            return None
        r = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "config",
                "--get",
                "merge.codevira-jsonl.driver",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, OSError):
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return (
            "decision-log merge driver is referenced in .gitattributes but not "
            "configured in this clone — run `codevira init` (or "
            "`codevira repair-ids`) or decision-log merges will conflict."
        )
    return None
