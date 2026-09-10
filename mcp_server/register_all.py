"""
register_all.py — clean-slate, one-MCP-per-project registration across IDEs.

Codevira's original "one shared 'codevira' entry that auto-detects the project"
model proved fragile: a bare (project-less) entry out-ranks project-scoped ones,
so sessions bind to the wrong project's memory (D000126/D000128). This module
implements the reliable alternative used by `codevira register-all`:

  ZERO every ``codevira*`` MCP entry on the machine, then register each project
  as its OWN uniquely-named MCP (``codevira-<slug>``) hard-pinned to
  ``--project-dir <path>``. One MCP per project. No auto-detect. No collisions.

That model is right only for a client whose config SCOPES entries by project.
Claude Code does (``projects.<path>.mcpServers``). Claude Desktop does not —
its ``mcpServers`` map is flat, so every entry loads in every conversation.
Fanning out there spawned one server per project and advertised
tool_count x projects tools in every conversation (12 servers / 432 tools on
the reference machine), all but one bound to a project the user was not in.
Claude Desktop is also the one client with per-tool-call project binding
(``server._maybe_bind_from_tool_path``), so it gets exactly ONE unpinned
entry. See ``SCOPE_*`` below.

Discovery is dynamic (no hardcoded paths): projects are the union of
  * dirs already carrying a codevira entry in an IDE config, and
  * dirs with a ``.codevira/`` store found by scanning the ancestors of those
    (plus any extra roots the caller passes),
minus nested monorepo sub-stores and junk/worktree/temp paths.

Every config file is backed up before it is rewritten, and only ``codevira*``
keys are ever touched — all other MCP servers and settings are preserved.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from mcp_server.paths import is_invalid_project_root
from mcp_server.ide_inject import (
    _antigravity_write_targets,
    _claude_desktop_config_path,
    _claude_global_config_path,
    _read_json_safe,
    _resolve_command,
    _write_json_safe,
)

_SKIP_DIRS = {
    "node_modules",
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".next",
    ".cache",
    "site-packages",
}
_JUNK_FRAGMENTS = (
    "/.claude/worktrees/",
    "/var/folders/",
    "/private/var/folders/",
    "/tmp/",
    "pytest-",
    "cv-isolated",
    "codevira-g3-",
)


def _is_junk(path: str) -> bool:
    return any(frag in path for frag in _JUNK_FRAGMENTS)


def slug(project: str) -> str:
    """codevira-<sanitized project basename> — the per-project MCP server name."""
    name = Path(project).name.lower()
    return "codevira-" + "".join(c if c.isalnum() else "-" for c in name).strip("-")


def _named_entry(cmd_path: str, python_exe: str, project: str, ide: str) -> dict:
    """A codevira MCP entry hard-pinned to a project via --project-dir."""
    if cmd_path == python_exe:  # python-fallback (codevira binary not found)
        args = ["-m", "mcp_server", "--project-dir", project]
    else:
        args = ["--project-dir", project]
    return {
        "type": "stdio",
        "command": cmd_path,
        "args": args,
        "env": {"CODEVIRA_IDE": ide},
    }


def _dynamic_entry(cmd_path: str, python_exe: str, ide: str) -> dict:
    """ONE codevira entry with no ``--project-dir``, for clients whose config
    has no per-project scoping.

    The project is resolved per tool call from the call's own ``file_path``
    by ``server._maybe_bind_from_tool_path``, which is gated on
    ``CODEVIRA_IDE=claude_desktop`` for exactly this purpose. The opt-in gate
    keeps it inert outside opted-in projects — its docstring says so in as
    many words: "a single global MCP registration stays fully inert outside
    opted-in projects".
    """
    args = ["-m", "mcp_server"] if cmd_path == python_exe else []
    return {
        "type": "stdio",
        "command": cmd_path,
        "args": args,
        "env": {"CODEVIRA_IDE": ide},
    }


# ── discovery ────────────────────────────────────────────────────────────────
def _registered_paths(claude_json: dict) -> set[str]:
    out: set[str] = set()
    for proj, pd in (claude_json.get("projects") or {}).items():
        if any("codevira" in k.lower() for k in ((pd or {}).get("mcpServers") or {})):
            out.add(str(Path(proj)))
    return out


def _antigravity_registered_paths() -> set[str]:
    out: set[str] = set()
    for cfg in _antigravity_write_targets():
        for _k, v in (_read_json_safe(cfg).get("mcpServers") or {}).items():
            args = v.get("args") or []
            if "--project-dir" in args:
                i = args.index("--project-dir")
                if i + 1 < len(args):
                    out.add(str(Path(args[i + 1])))
    return out


def _auto_scan_roots(known: set[str], extra: list[str]) -> list[Path]:
    roots: set[Path] = set()
    for p in known:
        pp = Path(p)
        for anc in (pp.parent, pp.parent.parent):
            if anc and anc != anc.parent and anc.is_dir():
                roots.add(anc)
    for e in extra:
        ep = Path(e).expanduser().resolve()
        if ep.is_dir():
            roots.add(ep)
    return [
        r
        for r in roots
        if not any(r != o and str(r).startswith(str(o) + os.sep) for o in roots)
    ]


def _scan_for_stores(scan_roots: list[Path], max_depth: int = 6) -> set[str]:
    found: set[str] = set()
    for root in scan_roots:
        base = len(root.parts)
        for dirpath, dirnames, _ in os.walk(root):
            if len(Path(dirpath).parts) - base >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            if ".codevira" in dirnames:
                proj = str(Path(dirpath).resolve())
                if (Path(dirpath) / ".codevira").is_dir() and not _is_junk(proj + "/"):
                    found.add(proj)
    return found


@dataclass
class Discovery:
    projects: list[str]
    nested_excluded: list[str] = field(default_factory=list)
    scan_roots: list[str] = field(default_factory=list)


def discover_projects(extra_scan_roots: list[str] | None = None) -> Discovery:
    """Top-level codevira projects on this machine (nested sub-stores excluded)."""
    claude_json = _read_json_safe(_claude_global_config_path())
    known = _registered_paths(claude_json) | _antigravity_registered_paths()
    roots = _auto_scan_roots(known, extra_scan_roots or [])
    # $HOME and system tops are REFUSED here, not merely deprioritised.
    #
    # codevira's own global home is ~/.codevira, so $HOME always carries a
    # .codevira marker and looks like a project to a naive scan. Left in, it
    # sorts as top-level and every genuine project underneath is discarded as
    # a "nested sub-store" by the rule below — turning a self-heal command
    # into one that de-registers the whole machine. Measured on the
    # maintainer's box: discovery reported 1 project and excluded 11, with a
    # plan of "-2 old / +1 named"; removing the rogue $HOME entry took it to
    # 12. `paths.is_invalid_project_root` already refuses these for BINDING —
    # this is the same question, so it must be the same answer.
    candidates = sorted(
        p
        for p in (known | _scan_for_stores(roots))
        if Path(p).is_dir()
        and not _is_junk(p + "/")
        and is_invalid_project_root(Path(p)) is None
    )
    top = [
        p
        for p in candidates
        if not any(p != q and p.startswith(q + os.sep) for q in candidates)
    ]
    nested = [p for p in candidates if p not in top]
    return Discovery(
        projects=top, nested_excluded=nested, scan_roots=[str(r) for r in roots]
    )


# ── rewrite ──────────────────────────────────────────────────────────────────
def _strip_codevira(servers: dict) -> int:
    keys = [k for k in servers if "codevira" in k.lower()]
    for k in keys:
        servers.pop(k)
    return len(keys)


def _backup(path: Path) -> str:
    if path.is_file():
        b = path.with_name(
            path.name + f".bak-registerall-{time.strftime('%Y%m%d-%H%M%S')}"
        )
        shutil.copy(path, b)
        return b.name
    return "n/a"


#: How a surface's config scopes MCP entries. This is a THREE-way property;
#: it was a bool, and collapsing "flat" and "single-dynamic" into one `False`
#: is what put 12 codevira servers into Claude Desktop.
SCOPE_PER_PROJECT = "per-project"  # nested under projects.<path>.mcpServers
SCOPE_FLAT = "flat"  # one pinned entry per project, all loaded
SCOPE_SINGLE_DYNAMIC = "single-dynamic"  # ONE entry, binds per tool call


def _rewrite_surface(
    path: Path,
    projects: list[str],
    cmd_path: str,
    python_exe: str,
    ide: str,
    *,
    scope: str,
    dry_run: bool,
) -> dict:
    data = _read_json_safe(path)
    removed = _strip_codevira(data.setdefault("mcpServers", {}))
    for pd in (data.get("projects") or {}).values():
        if isinstance(pd, dict):
            removed += _strip_codevira(pd.setdefault("mcpServers", {}))

    added = 0
    if scope == SCOPE_PER_PROJECT:
        projmap = data.setdefault("projects", {})
        for p in projects:
            servers = projmap.setdefault(p, {}).setdefault("mcpServers", {})
            servers[slug(p)] = _named_entry(cmd_path, python_exe, p, ide)
            added += 1
    elif scope == SCOPE_SINGLE_DYNAMIC:
        # Claude Desktop's mcpServers map is FLAT: every entry loads in EVERY
        # conversation. Fanning out one pinned entry per project therefore
        # spawned a server per project and advertised tool_count x projects
        # tools in every conversation — 12 servers and 432 tools on the
        # reference machine — with all but one bound to a project the user
        # was not in. Claude Code is nested and genuinely scoped, so the same
        # fan-out is correct there and stays.
        data["mcpServers"]["codevira"] = _dynamic_entry(cmd_path, python_exe, ide)
        added = 1
    else:  # SCOPE_FLAT — pinned entries, for clients with no dynamic binding
        servers = data["mcpServers"]
        for p in projects:
            servers[slug(p)] = _named_entry(cmd_path, python_exe, p, ide)
            added += 1

    backup_name = "n/a"
    if not dry_run and path.parent.exists():
        backup_name = _backup(path)
        _write_json_safe(path, data)
    return {"removed": removed, "added": added, "backup": backup_name}


@dataclass
class SurfaceResult:
    label: str
    path: str
    exists: bool
    removed: int = 0
    added: int = 0
    backup: str = "n/a"


def register_all(
    *, dry_run: bool = False, extra_scan_roots: list[str] | None = None
) -> dict:
    """Zero every codevira* entry across all detected IDE surfaces, then register
    one named MCP per discovered project. Returns a structured report."""
    disc = discover_projects(extra_scan_roots)
    cmd_path, python_exe = _resolve_command()

    surfaces = [
        # Claude Code nests under projects.<path>.mcpServers, so only the
        # matching project's server loads — fan-out is correct here.
        ("Claude Code", _claude_global_config_path(), "claude_code", SCOPE_PER_PROJECT),
        # Claude Desktop's map is flat AND it is the one client with
        # per-tool-call project binding, so it gets exactly one entry.
        (
            "Claude Desktop",
            _claude_desktop_config_path(),
            "claude_desktop",
            SCOPE_SINGLE_DYNAMIC,
        ),
    ]
    for cfg in _antigravity_write_targets():
        # Antigravity is flat too, but has no dynamic binding
        # (_maybe_bind_from_tool_path is gated on claude_desktop), so a single
        # entry would leave it unable to resolve any project. Pinned fan-out
        # stays until it gains a binding signal.
        surfaces.append(
            (f"Antigravity ({cfg.parent.name})", cfg, "antigravity", SCOPE_FLAT)
        )

    results: list[SurfaceResult] = []
    for label, path, ide, scope in surfaces:
        if not path.is_file():
            results.append(SurfaceResult(label, str(path), exists=False))
            continue
        r = _rewrite_surface(
            path,
            disc.projects,
            cmd_path,
            python_exe,
            ide,
            scope=scope,
            dry_run=dry_run,
        )
        results.append(
            SurfaceResult(label, str(path), True, r["removed"], r["added"], r["backup"])
        )

    return {
        "dry_run": dry_run,
        "codevira_bin": cmd_path,
        "projects": [{"slug": slug(p), "path": p} for p in disc.projects],
        "nested_excluded": disc.nested_excluded,
        "scan_roots": disc.scan_roots,
        "surfaces": [r.__dict__ for r in results],
    }


def cmd_register_all(
    *, dry_run: bool = False, scan_root: list[str] | None = None
) -> int:
    """`codevira register-all` — CLI entrypoint. Returns 0 on success."""
    report = register_all(dry_run=dry_run, extra_scan_roots=scan_root)

    print(f"codevira binary : {report['codevira_bin']}")
    print(f"scan roots      : {', '.join(report['scan_roots']) or '(none)'}")
    if report["nested_excluded"]:
        print("excluded nested .codevira sub-stores (not top-level projects):")
        for p in report["nested_excluded"]:
            print(f"    ~ {p}")
    print(f"projects ({len(report['projects'])}):")
    for pr in report["projects"]:
        print(f"    {pr['slug']:34} -> {pr['path']}")

    verb = "would write" if dry_run else "wrote"
    print(f"\n── {'plan (dry-run)' if dry_run else 'result'} ──")
    for s in report["surfaces"]:
        if not s["exists"]:
            print(f"  – {s['label']}: missing, skipped")
            continue
        tail = "" if dry_run else f"  (backup: {s['backup']})"
        print(
            f"  ✓ {s['label']}: {verb} -{s['removed']} old / +{s['added']} named{tail}"
        )

    if dry_run:
        print("\n(dry-run) nothing written.")
    else:
        print("\n✓ Done — one codevira-<slug> MCP per project, all self-pinned.")
        print(
            "→ Fully quit & reopen every IDE (Claude app included) so servers relaunch."
        )
    return 0
