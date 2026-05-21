"""
cli.py — Entry point for the `codevira` command.

Dispatches subcommands:
  codevira                      → start MCP server (default)
  codevira init                 → initialize project in centralized storage
  codevira register             → one-time global IDE registration (v1.6)
  codevira index                → run incremental index update
  codevira index --full         → full index rebuild
  codevira status               → show index health and stats
  codevira report               → show recent crash logs
  codevira report --clear       → clear the crash log
  codevira serve                → start MCP HTTP server
  codevira serve --install-service   → install macOS launchd auto-start
  codevira serve --uninstall-service → remove macOS launchd service

Global flags:
  --project-dir <path>          → override project directory (for Google Antigravity,
                                   which doesn't support `cwd` in its MCP config)
"""

from __future__ import annotations

# IMPORTANT: fork-safety must run BEFORE any code path can transitively
# import chromadb / sentence-transformers / torch. Importing the indexer
# package triggers ``indexer/_fork_safety.py`` which sets the macOS env
# vars + multiprocessing start method. Bug 7 fix (v2.0-rc.3).
import indexer  # noqa: F401  — fork-safety side-effect import

import argparse
import sys
from pathlib import Path


def _set_project_dir_early(args: list[str]) -> Path | None:
    """
    Parse --project-dir before any subcommand handling so that
    paths.set_project_dir() is called before any module-level path resolution.
    """
    for i, arg in enumerate(args):
        if arg == "--project-dir" and i + 1 < len(args):
            return Path(args[i + 1]).resolve()
        if arg.startswith("--project-dir="):
            return Path(arg.split("=", 1)[1]).resolve()
    return None


def _detect_project_root_markers(path: Path) -> bool:
    """Return True if the given path looks like a project root."""
    markers = [
        ".git",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "Makefile",
        "pom.xml",
        "build.gradle",
    ]
    return any((path / m).exists() for m in markers)


def cmd_init() -> None:
    """Initialize Codevira in the current project."""
    from mcp_server.paths import (
        get_project_root,
        get_data_dir,
        get_package_data_dir,
        is_invalid_project_root,
    )
    import shutil
    import yaml

    cwd = get_project_root()

    # v1.8.1: refuse $HOME and system top-levels. Treating $HOME as a
    # project caused 41 production crashes via the watcher walking
    # ~/Library/Group Containers/... — see CHANGELOG v1.8.1.
    rejection = is_invalid_project_root(cwd)
    if rejection:
        print(f"Error: {rejection}", file=sys.stderr)
        print(
            "  → cd into a project directory (one with .git, pyproject.toml, "
            "package.json, or similar marker) and re-run `codevira init`.",
            file=sys.stderr,
        )
        sys.exit(1)

    data_dir = get_data_dir()

    print()
    print("  Codevira — Project Initialization")
    print("  " + "─" * 40)
    print()

    # Step 1: Validate project root
    if not _detect_project_root_markers(cwd):
        parent = cwd.parent
        if _detect_project_root_markers(parent):
            print("  Warning: It looks like you may be in a subdirectory.")
            print(f"  Project markers found in: {parent}")
            print(f"  Current directory:        {cwd}")
            print()
            # Bug 22 (rc.4): use shared confirm() helper for retry-on-bad-input + flush.
            from mcp_server._prompts import confirm

            if not confirm("Continue initializing here anyway?", default=False):
                print("  Aborted. Run `codevira init` from your project root.")
                sys.exit(0)
            print()

    # Step 2a: Auto-migrate legacy .codevira/ if present
    git_dir = cwd / ".git"
    from mcp_server.migrate import detect_migration_needed, migrate_to_centralized

    if detect_migration_needed(cwd):
        print(
            "  Migrating legacy .codevira/ to centralized storage ...",
            end="",
            flush=True,
        )
        try:
            result = migrate_to_centralized(cwd)
            if result.get("migrated"):
                print(
                    f" done ({result.get('files_copied', 0)} files → {result.get('new_path', '')})"
                )
                # Re-evaluate data_dir after migration — now points to centralized path
                data_dir = get_data_dir()
            else:
                print(f" skipped ({result.get('reason', '')})")
        except Exception as e:
            print(f" failed ({e})")

    # Step 2b: Create centralized directory structure
    is_centralized = str(data_dir).startswith(
        str(Path.home() / ".codevira" / "projects")
    )
    if is_centralized:
        print("  Creating centralized data dir ...")
        print(f"    {data_dir}")
    else:
        print(f"  Creating .codevira/ in {cwd} ...")
    for subdir in ["graph/changesets", "codeindex", "logs"]:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)
    print("  Data directory ready ...                      done")

    # Step 3: For new centralized projects, no .gitignore entry needed.
    # For legacy mode (in-project), add .codevira/ to .gitignore.
    if not is_centralized and git_dir.exists():
        gitignore = cwd / ".gitignore"
        entry = ".codevira/"
        needs_add = True
        if gitignore.exists():
            content = gitignore.read_text()
            if ".codevira" in content:
                needs_add = False
        if needs_add:
            print("  Adding .codevira/ to .gitignore ...          ", end="", flush=True)
            with open(gitignore, "a") as f:
                if gitignore.exists() and gitignore.stat().st_size > 0:
                    existing = gitignore.read_text()
                    if not existing.endswith("\n"):
                        f.write("\n")
                f.write(f"\n# Codevira — auto-generated, do not commit\n{entry}\n")
            print("done")

    # Step 4: Zero-config auto-detection (no interactive prompts)
    print()
    from mcp_server.detect import auto_detect_project

    # rc.5 (P1-2): default is the union of all known source extensions so
    # polyglot projects don't lose .yaml / .md / .html silently. Pass
    # --single-language on the CLI to restore legacy narrowing.
    single_lang = getattr(cmd_init, "_single_language", False)
    detected = auto_detect_project(cwd, single_language=single_lang)

    # Apply CLI overrides if provided (parsed from args later)
    if hasattr(cmd_init, "_overrides"):
        overrides = cmd_init._overrides
        if overrides.get("name"):
            detected["name"] = overrides["name"]
        if overrides.get("language"):
            detected["language"] = overrides["language"]
        if overrides.get("dirs"):
            detected["watched_dirs"] = [d.strip() for d in overrides["dirs"].split(",")]
        if overrides.get("ext"):
            detected["file_extensions"] = [
                e.strip() for e in overrides["ext"].split(",")
            ]

    print("  Auto-detected:")
    print(f"    Project:     {detected['name']}")
    print(f"    Language:    {detected['language']}")
    print(f"    Source dirs: {', '.join(detected['watched_dirs'])}")
    print(f"    Extensions:  {', '.join(detected['file_extensions'])}")

    # Write config.yaml
    config = {
        "project": {
            "name": detected["name"],
            "language": detected["language"],
            "collection_name": detected["collection_name"],
            "watched_dirs": detected["watched_dirs"],
            "file_extensions": detected["file_extensions"],
        }
    }

    # Try to copy example config as base, then merge project settings
    pkg_config_example = get_package_data_dir() / "config.example.yaml"
    config_path = data_dir / "config.yaml"
    if pkg_config_example.exists():
        shutil.copy(pkg_config_example, config_path)
        # Merge project section on top
        with open(config_path) as f:
            base = yaml.safe_load(f) or {}
        base.update(config)
        with open(config_path, "w") as f:
            yaml.dump(base, f, default_flow_style=False, sort_keys=False)
    else:
        with open(config_path, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

    print()

    # Step 5: Run full index build — let rich progress bars render directly.
    # Suppress noisy HuggingFace/transformers output via env vars.
    import os as _os
    import contextlib
    import io

    print("  Building code index ...")
    try:
        from indexer.index_codebase import cmd_full_rebuild

        _os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
        _os.environ.setdefault("HF_HUB_VERBOSITY", "error")
        # Suppress stderr noise (BertModel LOAD REPORT, HF_TOKEN warnings)
        with contextlib.redirect_stderr(io.StringIO()):
            cmd_full_rebuild()
    except Exception as e:
        print(f"  skipped ({e})")
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(e, context="codevira init: index build", project_path=str(cwd))

    # Step 6: Generate graph stubs
    print("  Generating graph stubs ...            ", end="", flush=True)
    try:
        from indexer.index_codebase import cmd_generate_graph

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_generate_graph()
        output = buf.getvalue()
        nodes = "0"
        for line in output.splitlines():
            if "Nodes added:" in line:
                nodes = line.split(":")[-1].strip()
                break
        print(f"done ({nodes} nodes)")
    except Exception as e:
        print(f"skipped ({e})")
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(e, context="codevira init: graph stubs", project_path=str(cwd))

    # Step 7: Bootstrap roadmap
    print("  Bootstrapping roadmap ...             ", end="", flush=True)
    try:
        from indexer.index_codebase import cmd_bootstrap_roadmap
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_bootstrap_roadmap()
        print("done")
    except Exception as e:
        print(f"skipped ({e})")
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(
            e, context="codevira init: roadmap bootstrap", project_path=str(cwd)
        )

    # Step 8: Install git hook
    if git_dir.exists():
        print("  Installing git hook ...               ", end="", flush=True)
        try:
            hooks_dir = git_dir / "hooks"
            hooks_dir.mkdir(exist_ok=True)
            hook_path = hooks_dir / "post-commit"

            # Find codevira executable path using full resolution chain
            from mcp_server.ide_inject import _resolve_command

            resolved_cmd, _py = _resolve_command()
            # For git hooks, use the resolved binary if found; otherwise bare name
            # (git hooks inherit the user's shell PATH)
            cmd_path = resolved_cmd if resolved_cmd != _py else "codevira"

            hook_content = (
                "#!/bin/sh\n"
                "# Codevira post-commit hook — auto-reindex changed files\n"
                f'"{cmd_path}" index --quiet 2>/dev/null || true\n'
            )

            # Backup existing hook if it exists and is not ours
            if hook_path.exists():
                existing = hook_path.read_text()
                if "codevira" not in existing.lower():
                    hook_path.rename(hook_path.with_suffix(".bak"))

            hook_path.write_text(hook_content)
            hook_path.chmod(0o755)
            print("done")
        except Exception as e:
            print(f"skipped ({e})")
            from mcp_server._safe_crash import safe_log_crash

            safe_log_crash(e, context="codevira init: git hook", project_path=str(cwd))

    # Step 9: Auto-inject IDE configurations
    print()
    print("  " + "─" * 60)
    print(f"  ✓  Codevira initialized in {data_dir}")
    print()

    no_inject = getattr(cmd_init, "_no_inject", False)
    if not no_inject:
        print("  Configuring AI tools ...              ", end="", flush=True)
        try:
            from mcp_server.ide_inject import inject_ide_config

            results = inject_ide_config(cwd, project_name=detected["name"])
            if results:
                print("done")
                for ide_name, config_path in results.items():  # type: ignore[assignment]
                    print(f"    ✓ {ide_name}: {config_path}")
            else:
                print("no AI tools detected")
        except Exception as e:
            print(f"skipped ({e})")
            from mcp_server._safe_crash import safe_log_crash

            safe_log_crash(
                e, context="codevira init: IDE inject", project_path=str(cwd)
            )

    # Step 10: Register in global memory (with git_remote for rename-resilient lookup)
    try:
        from mcp_server.paths import get_global_db_path, _get_git_remote_url
        from indexer.global_db import GlobalDB
        from mcp_server.auto_init import _write_metadata

        git_remote = _get_git_remote_url(cwd)
        gdb = GlobalDB(get_global_db_path())
        # Bug 20 (rc.4): register under the project_root path (cwd), NOT the
        # storage dir (data_dir = ~/.codevira/projects/<slug>). Pre-fix this
        # produced duplicate rows for the same logical project — one row keyed
        # by data_dir (from cli.py + auto_init.py) and another keyed by
        # project_root (from global_sync.py). Downstream lookups by canonical
        # project path missed half the projects.
        gdb.register_project(
            str(cwd), detected["name"], detected["language"], git_remote=git_remote
        )
        proj_count = gdb.get_project_count()
        gdb.close()
        if proj_count > 1:
            print(f"  Registered in global memory ({proj_count} projects)")

        # Write metadata.json for centralized storage marker
        _write_metadata(data_dir, cwd)
    except Exception as e:
        print(f"  Global memory registration skipped ({e})")
        from mcp_server._safe_crash import safe_log_crash

        safe_log_crash(
            e, context="codevira init: global memory register", project_path=str(cwd)
        )

    # Print config for undetected tools — use the resolved binary path,
    # not the Python interpreter, so users get a clean command.
    from mcp_server.ide_inject import _resolve_command

    cmd_path, python_exe = _resolve_command()
    project_path = str(cwd)

    is_python_fallback = cmd_path == python_exe
    print()
    print("  For other AI tools, add this to their MCP config:")
    print()
    print("  {")
    print('    "mcpServers": {')
    print('      "codevira": {')
    if is_python_fallback:
        print(f'        "command": "{python_exe}",')
        print(
            f'        "args": ["-m", "mcp_server", "--project-dir", "{project_path}"]'
        )
    else:
        print(f'        "command": "{cmd_path}",')
        print(f'        "args": ["--project-dir", "{project_path}"]')
    print("      }")
    print("    }")
    print("  }")
    print()
    print("  Verify: ask your agent to call get_roadmap()")
    print()


def cmd_index(full: bool = False, quiet: bool = False, verbose: bool = False) -> None:
    """Run the indexer (incremental by default, or --full for complete rebuild).

    Args:
        full: rebuild from scratch instead of incremental.
        quiet: suppress all output (used by post-commit git hook).
        verbose: emit per-file decisions for debugging silent 0-chunk results
                 (Bug H fix, 2026-05-17). Cannot be combined with quiet.
    """
    from indexer.index_codebase import cmd_full_rebuild, cmd_incremental
    from mcp_server.paths import get_project_root, is_invalid_project_root

    # v1.8.1 hardening: cmd_full_rebuild/cmd_incremental both call
    # SQLiteGraph(get_data_dir()/"graph"/"graph.db") which mkdir's the
    # centralized path. Running `codevira index` from $HOME on v1.8.0 would
    # have created ~/.codevira/projects/<HOME_slug>/{graph,codeindex}/ as
    # dead-weight artefacts (no metadata.json -> not even cleanable via
    # --orphans). Guard at the CLI layer so the indexer can stay agnostic.
    rejection = is_invalid_project_root(get_project_root())
    if rejection:
        print(f"Error: {rejection}", file=sys.stderr)
        print(
            "  → cd into a project directory and re-run `codevira index`, "
            "or pass --project-dir <real-project-path>.",
            file=sys.stderr,
        )
        sys.exit(1)

    # P1 (helpful errors): refuse combination of --quiet + --verbose explicitly.
    if quiet and verbose:
        print("Error: --quiet and --verbose are mutually exclusive.", file=sys.stderr)
        sys.exit(1)

    if full:
        cmd_full_rebuild(verbose=verbose)
    else:
        cmd_incremental(quiet=quiet, verbose=verbose)


def cmd_status(check_stale: bool = False, show_global: bool = False) -> None:
    """Show index health and statistics."""
    from indexer.index_codebase import cmd_status as _cmd_status

    _cmd_status(check_stale=check_stale, show_global=show_global)


def cmd_report(limit: int = 20, clear: bool = False) -> None:
    """Show recent crash logs."""
    from mcp_server.crash_logger import read_recent_crashes, get_crash_log_path

    if clear:
        log_path = get_crash_log_path()
        if log_path.exists():
            log_path.unlink()
            print("  Crash log cleared.")
        else:
            print("  No crash log to clear.")
        return

    print()
    print("  Codevira — Crash Report")
    print("  " + "-" * 40)
    print()
    print(read_recent_crashes(limit=limit))
    print()


def cmd_server(project_dir: Path | None = None) -> None:
    """Start the MCP server (stdio transport)."""
    from mcp_server.server import main as server_main

    server_main()


def _print_http_preview_warning() -> None:
    """Warn that HTTP transport is single-project preview in v1.7."""
    print()
    print("  ⚠  HTTP/HTTPS transport is PREVIEW in v1.7 — single-project only.")
    print("     The server binds to one project at startup and cannot switch")
    print("     contexts per request. Multi-project HTTPS is planned for v1.8.")
    print("     For multi-project work, use stdio: `codevira register`.")
    print()


def cmd_serve(
    host: str = "127.0.0.1",
    port: int = 7007,
    use_https: bool = False,
    project_dir: Path | None = None,
    install_service: bool = False,
    uninstall_service: bool = False,
) -> None:
    """Start the MCP HTTP server (Streamable HTTP transport).

    PREVIEW (v1.7): Single-project only. The server binds to one project
    at startup and cannot switch contexts per request. Multi-project HTTPS
    (automatic routing based on the MCP initialize rootUri) is planned for
    v1.8. For multi-project work today, use stdio via `codevira register`.
    """
    if not uninstall_service:
        _print_http_preview_warning()

    # v1.8.1: refuse $HOME / system root for any cmd_serve invocation that
    # could persist a broken project_root (--install-service writes a
    # launchd plist; the regular path runs the HTTP server). --uninstall-
    # service is exempt — it removes existing state and should always
    # succeed regardless of where the user runs it from.
    if not uninstall_service:
        from mcp_server.paths import get_project_root, is_invalid_project_root

        # If --project-dir was passed explicitly, that's the candidate root;
        # otherwise fall back to cwd via get_project_root.
        candidate_root = (
            Path(project_dir).resolve() if project_dir else get_project_root()
        )
        rejection = is_invalid_project_root(candidate_root)
        if rejection:
            print(f"Error: {rejection}", file=sys.stderr)
            print(
                "  → cd into a project directory or pass "
                "--project-dir <real-project-path>.",
                file=sys.stderr,
            )
            sys.exit(1)

    if install_service:
        from mcp_server.launchd import install_launchd

        try:
            plist = install_launchd(
                port=port, use_https=use_https, host=host, project_dir=project_dir
            )
            print(f"  Launchd service installed: {plist}")
            print("  Codevira MCP server will start automatically on login.")
        except RuntimeError as e:
            print(f"  Error: {e}")
            sys.exit(1)
        return

    if uninstall_service:
        from mcp_server.launchd import uninstall_launchd

        try:
            removed = uninstall_launchd()
            if removed:
                print("  Launchd service removed.")
            else:
                print("  No launchd service was installed.")
        except RuntimeError as e:
            print(f"  Error: {e}")
            sys.exit(1)
        return

    from mcp_server.http_server import run_http_server

    run_http_server(host=host, port=port, use_https=use_https, project_dir=project_dir)


def cmd_register(
    global_mode: bool = True,
    claude_desktop: bool = False,
    http_url: str | None = None,
) -> None:
    """One-time global IDE registration (v1.6).

    Injects codevira into all detected AI tools' global configs so that
    every project the developer opens automatically has Codevira memory.

    Uses stdio transport — every project gets its own subprocess with its
    own memory. No ports, no background server. This is the recommended
    setup for solo developers working on multiple projects.
    """
    from mcp_server.paths import get_project_root, is_invalid_project_root
    from mcp_server.ide_inject import (
        _resolve_command,
        detect_installed_ides,
        inject_global_claude_code,
        inject_global_cursor,
        inject_global_windsurf,
        _inject_claude_desktop,
        inject_claude_http_url,
    )

    project_root = get_project_root()

    # v1.8.1: refuse $HOME / system dirs. cmd_register doesn't create the
    # data_dir itself, but it pins project_root into IDE configs (e.g.
    # Claude Desktop). A $HOME-pinned IDE config would later trigger the
    # auto_init guard on every MCP tool call — better to fail fast here
    # so the user gets a clear message instead of silently broken IDE
    # integration.
    rejection = is_invalid_project_root(project_root)
    if rejection:
        print(f"Error: {rejection}", file=sys.stderr)
        print(
            "  → cd into a project directory and re-run `codevira register` "
            "(or `codevira setup` for the v2.0 one-prompt installer).",
            file=sys.stderr,
        )
        sys.exit(1)

    cmd_path, python_exe = _resolve_command()

    from mcp_server import __version__

    print()
    print(f"  Codevira — Global IDE Registration (v{__version__})")
    print("  " + "─" * 44)
    print()

    if http_url:
        path = inject_claude_http_url(http_url)
        print(f"  ✓ Claude Code (HTTP URL): {path}")
        print()
        print(
            "  Tip: run `codevira configure` in a project to customize which folders are indexed."
        )
        print()
        return

    if claude_desktop:
        path = _inject_claude_desktop(project_root, cmd_path, python_exe)
        print(f"  ✓ Claude Desktop: {path}")
        print("  Note: Claude Desktop uses stdio — restart it to pick up changes.")
        print()
        print(
            "  Tip: run `codevira configure` in a project to customize which folders are indexed."
        )
        print()
        return

    # Global mode: inject into all detected IDEs
    ides = detect_installed_ides(project_root)
    results: dict[str, str] = {}

    for ide in ides:
        try:
            if ide == "claude":
                path = inject_global_claude_code(cmd_path, python_exe)
                if path:
                    results["Claude Code (global)"] = path
            elif ide == "cursor":
                path = inject_global_cursor(cmd_path, python_exe)
                if path:
                    results["Cursor (global)"] = path
            elif ide == "windsurf":
                path = inject_global_windsurf(cmd_path, python_exe)
                if path:
                    results["Windsurf (global)"] = path
            elif ide == "claude_desktop":
                path = _inject_claude_desktop(project_root, cmd_path, python_exe)
                if path:
                    results["Claude Desktop"] = path
            elif ide == "antigravity":
                from mcp_server.ide_inject import inject_global_antigravity

                path = inject_global_antigravity(cmd_path, python_exe)
                if path:
                    results["Antigravity (global)"] = path
        except Exception as e:
            print(f"  Warning: could not configure {ide}: {e}")

    if results:
        for ide_name, config_path in results.items():
            print(f"  ✓ {ide_name}: {config_path}")
        print()
        print("  Restart your AI tools to pick up the new configuration.")
        print("  Every project you open will now have Codevira memory automatically.")
        print()
        print(
            "  Tip: run `codevira configure` in a project to customize which folders are indexed."
        )
    else:
        print("  No AI tools detected. Install Claude Code, Cursor, or Windsurf first.")
    print()


def main() -> None:
    # Pre-parse --project-dir before argparse so we can initialize paths early.
    raw_args = sys.argv[1:]
    project_dir = _set_project_dir_early(raw_args)

    if project_dir is not None:
        from mcp_server.paths import set_project_dir

        set_project_dir(project_dir)

    # P2-8 (rc.5): scoped ArgumentParser for subcommands.
    # When argparse hits a bad arg on a SUBPARSER it normally walks up to
    # the root parser and prints the top-level usage line ("usage: codevira
    # [-h] [--version] [--project-dir PATH] {init,index,…17 subcommands…}
    # ..."). That's not useful when the user's mistake was in
    # `codevira doctor --bogus`. The override below makes each subparser
    # print its own usage when it errors.
    class _ScopedSubparser(argparse.ArgumentParser):
        def error(self, message):  # type: ignore[override]
            # Mirror argparse's default error path but using self.print_usage()
            # explicitly — keeps the usage scoped to the failing subcommand
            # instead of bubbling up to the root parser's combined usage.
            import sys as _sys

            self.print_usage(_sys.stderr)
            self.exit(2, f"{self.prog}: error: {message}\n")

    parser = argparse.ArgumentParser(
        prog="codevira",
        description="Codevira — AI context layer for your codebase",
    )
    # v2.0-rc.6 (Bug 17): standard --version flag. Every Python CLI
    # should expose one. Reads __version__ from mcp_server/__init__.py
    # so the bumper script can update one place.
    from mcp_server import __version__ as _codevira_version

    parser.add_argument(
        "--version",
        "-V",
        action="version",
        version=f"codevira {_codevira_version}",
    )
    parser.add_argument(
        "--project-dir",
        metavar="PATH",
        help="Project directory (alternative to cwd; useful for Google Antigravity)",
    )

    # P2-8: every subparser uses the scoped class so usage prints are
    # subcommand-local on error.
    subparsers = parser.add_subparsers(dest="command", parser_class=_ScopedSubparser)

    # init
    # init (P2-1 rc.5: added description; v2.2.0: updated for in-repo .codevira/)
    init_parser = subparsers.add_parser(
        "init",
        help="Initialize Codevira in the current project",
        description=(
            "Bootstrap codevira state for the current project. v2.2.0+ writes "
            "decisions, sessions, outcomes, and config to <repo>/.codevira/ "
            "(in-repo, git-committed). The cross-project tracking database "
            "(global.db) and crash log stay under ~/.codevira/. The rebuildable "
            "code-graph cache lives under <repo>/.codevira-cache/ (gitignored). "
            "Also updates .gitignore, regenerates AGENTS.md with the codevira "
            "marker block, and (unless --no-inject) injects MCP config + nudge "
            "files into detected IDEs. Equivalent to first-MCP-call auto-init "
            "but explicit. Use --dirs / --ext to override auto-detected values."
        ),
    )
    init_parser.add_argument("--name", help="Override project name")
    init_parser.add_argument("--language", help="Override detected language")
    init_parser.add_argument(
        "--dirs", help="Override source directories (comma-separated)"
    )
    init_parser.add_argument("--ext", help="Override file extensions (comma-separated)")
    init_parser.add_argument(
        "--no-inject", action="store_true", help="Skip auto-injecting IDE configs"
    )
    init_parser.add_argument(
        "--single-language",
        action="store_true",
        help=(
            "Index only the dominant language's extensions (legacy pre-rc.5 "
            "behavior). Default since rc.5 is to index every common source / "
            "config / docs extension so polyglot projects don't lose .yaml / "
            ".md / .html / etc. silently."
        ),
    )

    # index (P2-1 rc.5: added description)
    index_parser = subparsers.add_parser(
        "index",
        help="Run the code indexer",
        description=(
            "Build (or incrementally update) the codebase index for this project. "
            "Parses every watched source file with tree-sitter, refreshes the graph "
            "in graph/graph.db, and rebuilds the ChromaDB semantic-search index in "
            "codeindex/. Incremental by default — only re-processes files changed "
            "since the last index. Use --full to rebuild from scratch; --quiet to "
            "suppress progress output (used by the post-commit git hook)."
        ),
    )
    index_parser.add_argument(
        "--full", action="store_true", help="Full rebuild from scratch"
    )
    index_parser.add_argument(
        "--quiet", action="store_true", help="Suppress output (used by git hook)"
    )
    # 2026-05-17 Bug H fix (P10 observability): users hitting silent
    # 0-chunks had no way to see WHICH files were rejected and WHY.
    # --verbose emits per-file decisions (matched / skipped <reason>).
    index_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Emit per-file decisions (matched, skipped + reason). Use to diagnose silent 0-chunks.",
    )

    # status (P2-1 rc.5: added description)
    status_parser = subparsers.add_parser(
        "status",
        help="Show index health and statistics",
        description=(
            "Report this project's index health: graph node count, ChromaDB chunk "
            "count, and (with --check-stale) which source files have changed since "
            "the last index. With --global, also append a Global Status panel "
            "listing tracked projects + cross-project preferences/rules learned. "
            "Read-only — never modifies state."
        ),
    )
    status_parser.add_argument(
        "--check-stale",
        action="store_true",
        help="Scan source files to detect changes since last index (slower)",
    )
    status_parser.add_argument(
        "--global",
        dest="show_global",
        action="store_true",
        help="Also show cross-project memory stats and launchd service status",
    )

    # report (P2-1 rc.5: added description)
    report_parser = subparsers.add_parser(
        "report",
        help="Show recent crash logs",
        description=(
            "Print recent codevira crash log entries (~/.codevira/crash.log) with "
            "secret sanitization applied. Use this when the MCP server or watcher "
            "behaved unexpectedly; the file accumulates structured tracebacks + "
            "context. Use --clear to truncate the log after reading."
        ),
    )
    report_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of recent crashes to show (default: 20)",
    )
    report_parser.add_argument(
        "--clear", action="store_true", help="Clear the crash log"
    )

    # serve (P2-1 rc.5: added description)
    serve_parser = subparsers.add_parser(
        "serve",
        help="[Preview, v1.7] Start MCP HTTP server — single-project; multi-project HTTPS is v1.8",
        description=(
            "Run the codevira MCP server over HTTP/HTTPS instead of stdio. PREVIEW "
            "in v1.7+: the server binds to one project at startup; multi-project "
            "HTTPS arrives in v1.8. Most users should NOT use this — `codevira "
            "setup` configures every IDE with the standard stdio transport, which "
            "supports multi-project out of the box. Use --install-service to "
            "register a macOS launchd job that starts the server on login."
        ),
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=7007,
        help="TCP port to listen on (default: 7007)",
    )
    serve_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 for LAN access)",
    )
    serve_parser.add_argument(
        "--https",
        action="store_true",
        help="Enable HTTPS using mkcert certs from ~/.codevira/certs/",
    )
    serve_parser.add_argument(
        "--install-service",
        action="store_true",
        help="Install macOS launchd service so the server starts automatically on login",
    )
    serve_parser.add_argument(
        "--uninstall-service",
        action="store_true",
        help="Remove the macOS launchd service",
    )
    serve_parser.add_argument(
        "--project-dir",
        metavar="PATH",
        help="Project directory override (same as the global --project-dir flag)",
    )

    # register (v1.6: one-time global IDE registration)
    # setup (v2.0 — replaces register + folds in hooks + nudge files)
    setup_parser = subparsers.add_parser(
        "setup",
        help="One-prompt setup: configure every detected AI tool with Codevira",
        description=(
            "Detect every AI coding tool installed on this machine, then "
            "configure them all to use Codevira: MCP server entries, Claude "
            "Code lifecycle hooks, and per-IDE nudge files (CLAUDE.md, "
            "AGENTS.md, .cursor/rules/codevira.mdc, .windsurfrules, "
            "GEMINI.md, .github/copilot-instructions.md). Idempotent — "
            "re-run any time to re-sync."
        ),
    )
    setup_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (CI / scripted installs)",
    )
    setup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan; don't write anything",
    )
    setup_parser.add_argument(
        "--ide",
        action="append",
        metavar="IDE",
        help="Only configure this IDE (repeatable). One of: claude, cursor, "
        "windsurf, antigravity, codex, copilot, continue, aider",
    )
    setup_parser.add_argument(
        "--no-hooks",
        action="store_true",
        help="Skip Claude Code lifecycle hook installation",
    )
    setup_parser.add_argument(
        "--no-nudge-files",
        action="store_true",
        help="Skip CLAUDE.md / AGENTS.md / etc. generation",
    )
    setup_parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip MCP server config injection (just hooks + nudge files)",
    )

    register_parser = subparsers.add_parser(
        "register",
        help="[DEPRECATED — use `codevira setup`] One-time global IDE registration",
        description=(
            "DEPRECATED in v2.0 — will be REMOVED in v2.1. Use `codevira setup` "
            "instead: it does what `register` does plus installs Claude Code "
            "lifecycle hooks and writes per-IDE nudge files (CLAUDE.md, "
            "AGENTS.md, .cursor/rules/codevira.mdc, etc.) in one prompt. "
            "`register` still works for now (v2.0.x) but every invocation "
            "prints a deprecation banner. P2-7 (rc.5): named the removal "
            "version explicitly so users know when to migrate by."
        ),
    )
    register_parser.add_argument(
        "--claude-desktop",
        action="store_true",
        help="Only configure Claude Desktop (stdio mode)",
    )
    register_parser.add_argument(
        "--http-url",
        metavar="URL",
        help="Preview (v1.7, single-project): inject an HTTPS URL into Claude Code "
        "global config. HTTPS transport is single-project in v1.7 — "
        "multi-project HTTPS is planned for v1.8. For multi-project use, "
        "stick with the default stdio register.",
    )

    # configure (v1.8: interactive multi-select to pick watched_dirs + file_extensions)
    cfg_parser = subparsers.add_parser(
        "configure",
        help="Pick which folders/extensions Codevira indexes (interactive)",
        description=(
            "Pick which folders/extensions Codevira indexes. "
            "Run with no flags for interactive mode (numbered-list prompts). "
            "Use --dirs/--extensions for non-interactive (CI/scripts). "
            "Auto-bootstraps config.yaml + metadata.json + global.db "
            "registration on first run if they don't exist."
        ),
    )
    cfg_parser.add_argument(
        "--dirs",
        help="Comma-separated directories to watch (non-interactive)",
    )
    cfg_parser.add_argument(
        "--extensions",
        help="Comma-separated file extensions, e.g. '.py,.ts' (non-interactive)",
    )
    cfg_parser.add_argument(
        "--no-reindex",
        action="store_true",
        help="Skip the 'rebuild index now?' prompt after writing config",
    )
    cfg_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and print the proposed config; do not write",
    )

    # budget (v2.0 hero 6 — Token Budget Live View)
    budget_parser = subparsers.add_parser(
        "budget",
        help="Show token-spend per session (Hero 6)",
        description=(
            "Show this project's AI session token totals + per-source "
            "breakdown. Reads token_budget.jsonl populated by Hero 6's "
            "Stop hook. Run with no args for the most-recent session; "
            "use 'history' for a multi-session list."
        ),
    )
    budget_parser.add_argument(
        # P1-5 (rc.5): include None as a default-only sentinel via nargs="?"+default=None
        # but DO NOT list None in `choices=` — argparse renders it as the literal
        # string "None" in error messages, which users tried as a real value.
        "subaction",
        nargs="?",
        default=None,
        choices=["history"],
        help="Optional: 'history' to list multiple sessions",
    )
    budget_parser.add_argument(
        "--last",
        type=int,
        default=10,
        help="Number of sessions for 'history' (clamped 1-100)",
    )
    budget_parser.add_argument(
        "--full",
        action="store_true",
        help="Show full per-source breakdown (default: top-3 wasted only)",
    )
    budget_parser.add_argument(
        "--project",
        metavar="PATH",
        default=None,
        help="Read another project's log instead of cwd",
    )

    # doctor (v2.0 Pillar 1.3 — health-check)
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Run health checks; show ✓/⚠/✗ + exact fix commands",
        description=(
            "Diagnose codevira's state in this project: data dir, "
            "graph.db, global.db, detected IDEs, nudge files, "
            "engine kill-switch, crash log size. Read-only — never "
            "modifies anything; just tells you the exact command to "
            "run for each warning / failure. Exit code 0 if clean "
            "(or warnings only), 1 if any check failed."
        ),
    )
    doctor_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show extra details under each warning / failure",
    )

    # projects (Bug 21b, rc.4) — inventory of every tracked project on this machine
    projects_parser = subparsers.add_parser(
        "projects",
        help="List every project codevira is tracking on this machine",
        description=(
            "Inventory of ~/.codevira/projects/. Shows each project's "
            "completeness (config + metadata + global.db row), graph + "
            "index presence, and disk size. Use --ghosts-only to filter "
            "for incomplete dirs (Bug 21 — pair with `codevira clean` to "
            "remove them)."
        ),
    )
    projects_parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Emit machine-readable JSON instead of the rich-table view",
    )
    projects_parser.add_argument(
        "--ghosts-only",
        action="store_true",
        help="Show only project dirs that are missing config / metadata / global.db row",
    )
    # 2026-05-17 Bug G partial fix: project keys on disk are long hashes
    # (`Users_sachin_..._6d2f5d4d`) which users can't trivially navigate
    # to. `--paths` prints `<project_path>  →  <data_dir>` per line so
    # users can `cd $(codevira projects --paths | grep myproj | ...)`.
    # A full rename to short keys requires a migration we defer to v3.0.
    projects_parser.add_argument(
        "--paths",
        action="store_true",
        help="Show each project's source path + data dir path (pairs project basename with the ~/.codevira/projects/<key>/ dir)",
    )

    # agents (v2.0 Pillar 2.2 — universal nudge generator)
    agents_parser = subparsers.add_parser(
        "agents",
        help="Regenerate per-IDE nudge files (CLAUDE.md, AGENTS.md, etc.)",
        description=(
            "Regenerate the codevira nudge block in every detected IDE's "
            "config file (CLAUDE.md, AGENTS.md, .cursor/rules/codevira.mdc, "
            ".windsurfrules, GEMINI.md, .github/copilot-instructions.md). "
            "Idempotent: existing user content is preserved; only the "
            "<!-- codevira:start -->...<!-- codevira:end --> block is "
            "replaced. Subset of `codevira setup` — useful when you want "
            "to refresh nudge files without re-running full setup."
        ),
    )
    agents_parser.add_argument(
        # P1-5 (rc.5): drop None from `choices=`; default=None still works because
        # argparse uses `default` independently of `choices`. Previously listed
        # None and users tried `--ide None` as a literal string.
        # P1-1 (rc.5): default now renders nudge files for DETECTED IDEs only
        # (aligned with `setup`). Pass --ide=all to render for every supported
        # IDE regardless of whether it's installed.
        "--ide",
        default=None,
        choices=sorted(
            {
                "claude",
                "cursor",
                "windsurf",
                "antigravity",
                "codex",
                "copilot",
                "agents_md",
                "all",
            }
        ),
        help=(
            "Render nudge files only for this IDE (default: every IDE detected "
            "on this machine). Use 'all' to render for every supported IDE "
            "regardless of detection."
        ),
    )
    agents_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without modifying any files",
    )
    agents_parser.add_argument(
        "--project",
        metavar="PATH",
        default=None,
        help="Generate nudge files for another project (validated)",
    )

    # hooks (v2.0 Pillar 2.3 — Claude Code lifecycle hook installer)
    hooks_parser = subparsers.add_parser(
        "hooks",
        help="Install Claude Code lifecycle hooks (Pillar 2.3)",
        description=(
            "Install / refresh codevira's Claude Code lifecycle hooks "
            "(SessionStart, PreToolUse, PostToolUse, UserPromptSubmit, "
            "Stop). Subset of `codevira setup` — useful when Claude Code "
            "got re-installed and its global hook config was reset."
        ),
    )
    hooks_subparsers = hooks_parser.add_subparsers(
        dest="hooks_subcommand",
        required=True,
    )
    hooks_install = hooks_subparsers.add_parser(
        "install",
        help="Install / refresh Claude Code lifecycle hooks",
    )
    hooks_install.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be installed without modifying any files",
    )
    hooks_install.add_argument(
        "--project",
        metavar="PATH",
        default=None,
        help="Operate on another project (validated)",
    )
    # P2-6 (rc.5): list + uninstall subcommands. The previous surface only
    # supported `install`; to remove hooks the user had to delete files +
    # edit ~/.claude/settings.json by hand.
    hooks_subparsers.add_parser(
        "list",
        help="List installed Claude Code hook scripts + their state",
        description=(
            "Show every codevira-* hook script under ~/.claude/hooks/, its "
            "size, last-modified mtime, and whether it's registered in "
            "~/.claude/settings.json. Read-only."
        ),
    )
    hooks_uninstall = hooks_subparsers.add_parser(
        "uninstall",
        help="Remove all codevira-* hook scripts and unregister from settings.json",
        description=(
            "Remove every codevira-* hook script from ~/.claude/hooks/ AND "
            "drop the codevira entries from ~/.claude/settings.json's hooks "
            "block. Other entries in those files are preserved."
        ),
    )
    hooks_uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting anything",
    )
    hooks_uninstall.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )

    # replay (v2.0 hero 8 — Decision Replay)
    replay_parser = subparsers.add_parser(
        "replay",
        help="Browse the decisions timeline (Hero 8)",
        description=(
            "Browse this project's decision history with outcomes + session "
            "context. Three output formats: terminal (default), markdown, html. "
            "Use --query to filter by substring; --since to widen lookback."
        ),
    )
    replay_parser.add_argument(
        "--query",
        default=None,
        help="Filter decisions/files/context by substring",
    )
    replay_parser.add_argument(
        "--since",
        default="30d",
        help="Lookback window: e.g. 7d, 30d, 90d (default 30d, max 365d)",
    )
    replay_parser.add_argument(
        "--top",
        type=int,
        default=20,
        help="Max decisions to render (clamped 1-200, default 20)",
    )
    replay_parser.add_argument(
        "--format",
        default="terminal",
        choices=["terminal", "markdown", "html"],
        help="Output format (default: terminal)",
    )
    replay_parser.add_argument(
        "--project",
        metavar="PATH",
        default=None,
        help="Read another project's decisions (validated)",
    )
    replay_parser.add_argument(
        "--ascii",
        action="store_true",
        help="Use ASCII fallbacks instead of unicode badges in terminal mode",
    )
    replay_parser.add_argument(
        "--out",
        metavar="FILE",
        default=None,
        help="Write to FILE instead of stdout (e.g. --format html --out timeline.html)",
    )

    # v2.2.0+: `insights` CLI command removed (it surfaced Hero 10's
    # promotion score, which was deleted along with preferences /
    # learned_rules per the 2026-05-22 surface-cut audit).

    # clean (P2-1 + P2-10 rc.5: added description + self-contained flag help)
    clean_parser = subparsers.add_parser(
        "clean",
        help="Remove all Codevira data, IDE configs, and services",
        description=(
            "Uninstall codevira's machine-wide state: wipe ~/.codevira/ (all "
            "project data, learned preferences/rules, decisions), remove "
            "mcpServers.codevira from every detected IDE config, and remove any "
            "installed launchd service. Use --all to also remove per-project "
            "artifacts (legacy .codevira/ directories committed into repos, git "
            "post-commit hooks, per-project IDE config files). Always preview "
            "with --dry-run first."
        ),
    )
    clean_parser.add_argument(
        "--all",
        action="store_true",
        help="Also clean per-project artifacts (legacy .codevira/, git hooks, per-project IDE configs)",
    )
    clean_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be removed without deleting anything",
    )
    clean_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip confirmation prompt",
    )
    clean_parser.add_argument(
        "--legacy",
        action="store_true",
        help="Only remove .codevira.migrated/ backup directories from project "
        "repos. These directories are created when an older in-repo "
        ".codevira/ layout is auto-migrated to centralised storage on "
        "first run; they're kept around for one cycle as a safety net.",
    )
    clean_parser.add_argument(
        "--orphans",
        action="store_true",
        help="Only remove project data dirs whose original_path is no longer "
        "a valid project root — covers projects that were registered at "
        "$HOME or system top-levels (a v1.8.0-era bug fixed in v1.8.1) "
        "and projects whose repo directory was deleted.",
    )
    clean_parser.add_argument(
        "--ghosts",
        action="store_true",
        help="P2-4 (rc.5): only remove dirs classified as 'ghost' by "
        "`codevira projects` — present on disk but missing config.yaml "
        "or metadata.json (created as side effect of MCP tool calls "
        "without a full init). Surgical cleanup; preserves tracked "
        "projects and their indexes.",
    )

    # engine — internal subcommand invoked by Claude Code lifecycle hook
    # scripts. Not user-facing; surfaces here so `codevira engine handle
    # PreToolUse` works from data/hooks/*.sh. (P2-1 rc.5: added description)
    # 2026-05-17: codevira heal — self-service recovery (P7 reversible
    # operations). Pairs with the HNSW self-heal detection in
    # _check_chroma_health. Users hitting corrupted Chroma stores no
    # longer need to `rm -rf` by hand and remember the data dir path.
    heal_parser = subparsers.add_parser(
        "heal",
        help="Self-service recovery: wipe + rebuild corrupted state",
        description=(
            "Diagnose and fix common corruption issues without manual "
            "intervention. Pairs with the startup self-diagnose layer — "
            "when codevira detects a corrupted Chroma store, stale graph, "
            "or split config, this command provides the recovery path. "
            "Each --target is scoped: --vectors wipes the Chroma collection "
            "for the current project; --graph wipes graph.db; --all wipes "
            "everything for this project (preserves global.db + other "
            "projects' data)."
        ),
    )
    heal_parser.add_argument(
        "--vectors",
        action="store_true",
        help="Wipe + rebuild this project's Chroma vector store (fixes HNSW corruption)",
    )
    heal_parser.add_argument(
        "--graph",
        action="store_true",
        help="Wipe + rebuild this project's graph.db (fixes graph corruption)",
    )
    heal_parser.add_argument(
        "--decisions",
        action="store_true",
        help="Embed ALL existing decisions into the semantic index (v2.0→v2.1 upgrade backfill, non-destructive)",
    )
    heal_parser.add_argument(
        "--all",
        action="store_true",
        help="Wipe ALL of this project's data (codeindex + graph + sessions)",
    )
    heal_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    heal_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip confirmation prompt",
    )

    # 2026-05-18 v2.1.2 Item 3b: `codevira reset` — destructive operations
    # move OUT of `heal` (whose name implies fix-in-place). `heal`
    # retains only the non-destructive `--decisions` backfill.
    reset_parser = subparsers.add_parser(
        "reset",
        help="DESTRUCTIVE: wipe + rebuild this project's vector store / graph (auto-backs-up decisions first)",
        description=(
            "Destructive recovery operations. Each flag wipes a specific "
            "part of this project's local state and rebuilds it. Decisions, "
            "outcomes, preferences, and learned rules are AUTO-EXPORTED to "
            "`<data_dir>/exports/<timestamp>-pre-<target>.json` before "
            "any wipe (pass --no-backup to skip). This command was split "
            "from `codevira heal` in v2.1.2 — heal's name implied 'fix in "
            "place,' but the implementation always wiped + rebuilt. "
            "`reset` is the honest name."
        ),
    )
    reset_parser.add_argument(
        "--vectors",
        action="store_true",
        help="Wipe + rebuild this project's Chroma vector store (fixes HNSW corruption)",
    )
    reset_parser.add_argument(
        "--graph",
        action="store_true",
        help="Wipe + rebuild this project's graph.db (DESTROYS decisions, outcomes, preferences, rules unless --no-backup is omitted)",
    )
    reset_parser.add_argument(
        "--all",
        action="store_true",
        help="Wipe ALL of this project's local state (vectors + graph + sessions)",
    )
    reset_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the automatic export-before-destroy backup (use with caution)",
    )
    reset_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes",
    )
    reset_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Skip the typed confirmation prompt (script use)",
    )

    # 2026-05-18 v2.1.2 Item 3e: `codevira export decisions` — standalone
    # backup command. Closes Report 1 §7 gap ("Is there an export tool we're
    # missing?"). Shares its implementation with the auto-backup in reset.
    export_parser = subparsers.add_parser(
        "export",
        help="Export this project's decisions / state to JSON or SQL",
        description=(
            "Export this project's local state to a portable file. The "
            "default target 'decisions' writes decisions + sessions + "
            "outcomes + preferences + learned_rules + phases — everything "
            "you'd want to back up before a destructive operation OR "
            "carry to another machine. Target 'all' adds nodes / edges / "
            "symbols / call_edges / file_hashes (the full graph state). "
            "Format defaults to JSON (human-readable, jq-friendly); SQL "
            "dump preserves schema + FK relationships for full restoration."
        ),
    )
    export_parser.add_argument(
        "target",
        nargs="?",
        default="decisions",
        choices=["decisions", "all"],
        help="What to export (default: decisions)",
    )
    export_parser.add_argument(
        "--format",
        dest="format",
        default="json",
        choices=["json", "sql"],
        help="Output format (default: json)",
    )
    export_parser.add_argument(
        "--out",
        default=None,
        help="Output file path (default: <data_dir>/exports/<timestamp>-<target>.<ext>)",
    )
    export_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing",
    )

    # 2026-05-19 v2.2.0 Phase D: `codevira sync` — regenerate AGENTS.md
    # + manifest + digest + FTS5 from decisions.jsonl. Manual / recovery
    # path; every record_decision triggers this synchronously by default.
    sync_parser = subparsers.add_parser(
        "sync",
        help="Regenerate AGENTS.md + indexes from .codevira/decisions.jsonl",
        description=(
            "Regenerate derived state from the canonical "
            "`.codevira/decisions.jsonl`: rebuilds `.codevira/manifest.yaml`, "
            "`.codevira/digest.jsonl`, `.codevira-cache/fts5.sqlite`, and "
            "regenerates the codevira-managed block in `AGENTS.md` (5 KB "
            "cap, marker-bounded — content outside `<!-- codevira:begin -->` "
            "and `<!-- codevira:end -->` is preserved). Idempotent; safe "
            "to run any time. Normally not needed because every "
            "record_decision triggers regen synchronously."
        ),
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be regenerated without writing",
    )
    sync_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-step counts",
    )

    # v2.2.0 Phase F: `codevira observe-git` — git-observed outcome tracker.
    observe_parser = subparsers.add_parser(
        "observe-git",
        help="Classify decisions as kept/modified/reverted from git history",
        description=(
            "Scan git log since the last observation and classify each "
            "decision against HEAD: 'kept' (file unchanged since decision), "
            "'modified' (file changed but partial preservation), 'reverted' "
            "(file deleted OR materially changed). Appends events to "
            "`.codevira/outcomes.jsonl` and updates `digest.weight` so the "
            "relevance hook deprioritizes reverted decisions. Recommended: "
            "run after every commit batch or as a post-commit hook."
        ),
    )
    observe_parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print per-decision classifications",
    )

    # 2026-05-18 v2.1.2 Item 1: `codevira calibrate` — manual re-fit of
    # similarity thresholds. Auto-recalibration also runs every 10
    # decisions added, but power users may want explicit control.
    calibrate_parser = subparsers.add_parser(
        "calibrate",
        help="Re-fit semantic similarity thresholds from this project's positive samples",
        description=(
            "Re-fit search_decisions similarity thresholds from this "
            "project's positive samples (decisions marked do_not_revert=True "
            "and decisions confirmed kept via outcome tracking). The "
            "calibrator finds the 10 nearest neighbours for each positive "
            "and sets the threshold at the 75th percentile of those "
            "distances. Clamped to [0.35, 0.80] for safety. Stored at "
            "<data_dir>/calibration.json. Auto-recalibration also runs "
            "every 10 decisions added in the background."
        ),
    )
    calibrate_parser.add_argument(
        "target",
        nargs="?",
        default="decisions",
        choices=["decisions"],
        help="What to calibrate (default: decisions; only target supported in v2.1.2)",
    )
    calibrate_parser.add_argument(
        "--decisions",
        action="store_true",
        help="Equivalent to passing 'decisions' as target",
    )
    calibrate_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be set without writing calibration.json",
    )

    engine_parser = subparsers.add_parser(
        "engine",
        help="Internal: lifecycle-hook engine entry (called by hook scripts)",
        description=(
            "INTERNAL — invoked by codevira's Claude Code lifecycle hook "
            "scripts (~/.claude/hooks/codevira-*.sh). Reads a Claude Code "
            "event JSON from stdin, runs the heroes (Decision Lock, "
            "Anti-Regression, Scope Contract, etc.) registered for that "
            "event, and writes the protocol-compatible response on stdout. "
            "Set CODEVIRA_ENGINE=0 to disable all policies machine-wide "
            "(the kill switch). End users normally never invoke this directly."
        ),
    )
    engine_sub = engine_parser.add_subparsers(dest="engine_action")
    handle_parser = engine_sub.add_parser(
        "handle",
        help="Process a Claude Code lifecycle hook event from stdin",
    )
    handle_parser.add_argument(
        "event_type",
        help="Claude Code event name (PreToolUse, PostToolUse, SessionStart, "
        "UserPromptSubmit, Stop)",
    )

    args = parser.parse_args(raw_args)

    # P0-6 (rc.5): self-heal ghost dirs from CLI invocations too. Before this
    # rc, only MCP tool dispatch fired the Bug-21a repair. So a CLI-only user
    # who got a ghost dir from a stale Claude Code session could never recover
    # via codevira commands — only via `codevira clean` (which wipes
    # everything). Now every codevira invocation (except commands that have
    # their own bootstrap logic like `init`, `setup`, `clean`, `engine`) runs
    # the cheap synchronous repair first.
    _NO_HEAL_COMMANDS = {"init", "setup", "clean", "engine", "register", "configure"}
    if args.command and args.command not in _NO_HEAL_COMMANDS:
        try:
            from mcp_server.paths import (
                get_project_root,
                is_invalid_project_root,
                get_data_dir,
            )

            project_root = get_project_root()
            if (
                project_root is not None
                and is_invalid_project_root(project_root) is None
            ):
                from mcp_server._repair_init import repair_incomplete_init

                data_dir = get_data_dir()
                # Only repair if there's already SOME state on disk — never
                # bootstrap from nothing as a side effect of a read-only CLI.
                if data_dir.is_dir():
                    repair_incomplete_init(data_dir, project_root)
        except Exception:
            # Self-heal is best-effort — must never break a CLI invocation.
            pass

    if args.command == "init":
        # v2.2.0: init also scaffolds .codevira/ in the repo + updates
        # AGENTS.md / .gitignore. The legacy cmd_init (project bootstrap +
        # IDE registration) still runs first; cli_init.cmd_init adds the
        # new in-repo storage layout on top.
        cmd_init._overrides = {  # type: ignore[attr-defined]
            "name": getattr(args, "name", None),
            "language": getattr(args, "language", None),
            "dirs": getattr(args, "dirs", None),
            "ext": getattr(args, "ext", None),
        }
        cmd_init._no_inject = getattr(args, "no_inject", False)  # type: ignore[attr-defined]
        cmd_init._single_language = getattr(args, "single_language", False)  # type: ignore[attr-defined]
        cmd_init()
        # v2.2.0: scaffold .codevira/ (in-repo storage layer).
        try:
            from mcp_server.cli_init import cmd_init as cmd_init_v22

            cmd_init_v22(yes=True, dry_run=False)
        except Exception as e:
            print(f"  ⚠ v2.2.0 .codevira/ scaffold failed: {e}", file=sys.stderr)
    elif args.command == "index":
        cmd_index(full=args.full, quiet=args.quiet, verbose=args.verbose)
    elif args.command == "status":
        cmd_status(
            check_stale=getattr(args, "check_stale", False),
            show_global=getattr(args, "show_global", False),
        )
    elif args.command == "report":
        cmd_report(limit=args.limit, clear=args.clear)
    elif args.command == "serve":
        # --project-dir may appear after "serve" — merge with pre-parsed value
        sub_project_dir = getattr(args, "project_dir", None)
        if sub_project_dir and project_dir is None:
            from mcp_server.paths import set_project_dir

            project_dir = Path(sub_project_dir).resolve()
            set_project_dir(project_dir)
        cmd_serve(
            host=args.host,
            port=args.port,
            use_https=args.https,
            project_dir=project_dir,
            install_service=getattr(args, "install_service", False),
            uninstall_service=getattr(args, "uninstall_service", False),
        )
    elif args.command == "setup":
        from mcp_server.setup_wizard import cmd_setup

        only_ides_arg = getattr(args, "ide", None)
        only_ides_tuple = tuple(only_ides_arg) if only_ides_arg else None
        rc = cmd_setup(
            yes=getattr(args, "yes", False),
            dry_run=getattr(args, "dry_run", False),
            only_ides=only_ides_tuple,
            install_mcp=not getattr(args, "no_mcp", False),
            install_hooks=not getattr(args, "no_hooks", False),
            write_nudge_files=not getattr(args, "no_nudge_files", False),
        )
        sys.exit(rc)
    elif args.command == "register":
        # v2.0 deprecation: redirect users to `codevira setup` while
        # preserving the old behaviour for any scripts that pin it.
        print(
            "[deprecated] `codevira register` is deprecated in v2.0. "
            "Use `codevira setup` instead — it does this plus hooks + "
            "nudge files in one prompt.",
            file=sys.stderr,
        )
        print(file=sys.stderr)
        cmd_register(
            claude_desktop=getattr(args, "claude_desktop", False),
            http_url=getattr(args, "http_url", None),
        )
    elif args.command == "configure":
        from mcp_server.cli_configure import cmd_configure

        try:
            rc = cmd_configure(
                interactive=(
                    args.dirs is None and args.extensions is None and not args.dry_run
                ),
                dirs_arg=args.dirs,
                exts_arg=args.extensions,
                reindex=not args.no_reindex,
                dry_run=args.dry_run,
            )
        except KeyboardInterrupt:
            # Outer-level guard: Ctrl+C anywhere cmd_configure didn't handle
            # internally (e.g. during scan_project, during bootstrap, during
            # auto_detect_project). Prompt-level and reindex-level handlers
            # catch Ctrl+C with context-specific messages; this catches
            # everything else and exits cleanly with the POSIX SIGINT code.
            print()
            print("Aborted.")
            sys.exit(130)
        sys.exit(rc)
    elif args.command == "budget":
        # Hero 6 — Token Budget Live View. Reads token_budget.jsonl
        # populated by the Stop hook + TokenBudgetPersist policy.
        from mcp_server.cli_budget import cmd_budget

        project_arg = getattr(args, "project", None)
        rc = cmd_budget(
            show_history=(getattr(args, "subaction", None) == "history"),
            last=getattr(args, "last", 10),
            full=getattr(args, "full", False),
            project=Path(project_arg) if project_arg else None,
        )
        sys.exit(rc)
    elif args.command == "doctor":
        # Pillar 1.3 — health check
        from mcp_server.doctor import cmd_doctor

        rc = cmd_doctor(verbose=getattr(args, "verbose", False))
        sys.exit(rc)
    elif args.command == "projects":
        # Bug 21b (rc.4) — project inventory
        from mcp_server.cli_projects import cmd_projects

        rc = cmd_projects(
            output_json=getattr(args, "output_json", False),
            ghosts_only=getattr(args, "ghosts_only", False),
            show_paths=getattr(args, "paths", False),  # 2026-05-17 Bug G
        )
        sys.exit(rc)
    elif args.command == "agents":
        # Pillar 2.2 — regenerate per-IDE nudge files
        from mcp_server.cli_agents import cmd_agents

        project_arg = getattr(args, "project", None)
        rc = cmd_agents(
            ide=getattr(args, "ide", None),
            dry_run=getattr(args, "dry_run", False),
            project=Path(project_arg) if project_arg else None,
        )
        sys.exit(rc)
    elif args.command == "hooks":
        # Pillar 2.3 — install/list/uninstall Claude Code lifecycle hooks
        # (P2-6 rc.5: added list + uninstall)
        sub = getattr(args, "hooks_subcommand", None)
        if sub == "install":
            from mcp_server.cli_agents import cmd_hooks_install

            project_arg = getattr(args, "project", None)
            rc = cmd_hooks_install(
                project=Path(project_arg) if project_arg else None,
                dry_run=getattr(args, "dry_run", False),
            )
            sys.exit(rc)
        elif sub == "list":
            from mcp_server.cli_hooks_admin import cmd_hooks_list

            sys.exit(cmd_hooks_list())
        elif sub == "uninstall":
            from mcp_server.cli_hooks_admin import cmd_hooks_uninstall

            sys.exit(
                cmd_hooks_uninstall(
                    dry_run=getattr(args, "dry_run", False),
                    yes=getattr(args, "yes", False),
                )
            )
        else:
            print("Error: unknown hooks subcommand", file=sys.stderr)
            print(
                "  → run `codevira hooks --help` for the available "
                "subcommands (install | list | uninstall).",
                file=sys.stderr,
            )
            sys.exit(2)
    elif args.command == "replay":
        # Hero 8 — Decision Replay. Browses decisions timeline.
        from mcp_server.cli_replay import cmd_replay

        project_arg = getattr(args, "project", None)
        out_arg = getattr(args, "out", None)
        rc = cmd_replay(
            query=getattr(args, "query", None),
            since=getattr(args, "since", "30d"),
            top=getattr(args, "top", 20),
            format=getattr(args, "format", "terminal"),
            project=Path(project_arg) if project_arg else None,
            ascii_mode=getattr(args, "ascii", False),
            out_file=Path(out_arg) if out_arg else None,
        )
        sys.exit(rc)
    elif args.command == "clean":
        cmd_clean(
            clean_all=getattr(args, "all", False),
            dry_run=getattr(args, "dry_run", False),
            yes=getattr(args, "yes", False),
            legacy_only=getattr(args, "legacy", False),
            orphans_only=getattr(args, "orphans", False),
            ghosts_only=getattr(args, "ghosts", False),
        )
    elif args.command == "heal":
        rc = cmd_heal(
            vectors=getattr(args, "vectors", False),
            graph=getattr(args, "graph", False),
            decisions=getattr(args, "decisions", False),
            heal_all=getattr(args, "all", False),
            dry_run=getattr(args, "dry_run", False),
            yes=getattr(args, "yes", False),
        )
        sys.exit(rc)
    elif args.command == "reset":
        # 2026-05-18 v2.1.2 Item 3b: destructive operations split from heal.
        rc = cmd_reset(
            vectors=getattr(args, "vectors", False),
            graph=getattr(args, "graph", False),
            reset_all=getattr(args, "all", False),
            no_backup=getattr(args, "no_backup", False),
            dry_run=getattr(args, "dry_run", False),
            yes=getattr(args, "yes", False),
        )
        sys.exit(rc)
    elif args.command == "export":
        # 2026-05-18 v2.1.2 Item 3e: standalone export command.
        from mcp_server.cli_export import cmd_export

        rc = cmd_export(
            target=getattr(args, "target", "decisions"),
            fmt=getattr(args, "format", "json"),
            out=getattr(args, "out", None),
            dry_run=getattr(args, "dry_run", False),
        )
        sys.exit(rc)
    elif args.command == "sync":
        # 2026-05-19 v2.2.0 Phase D: regenerate AGENTS.md + indexes.
        from mcp_server.cli_sync import cmd_sync

        rc = cmd_sync(
            dry_run=getattr(args, "dry_run", False),
            verbose=getattr(args, "verbose", False),
        )
        sys.exit(rc)
    elif args.command == "observe-git":
        # 2026-05-19 v2.2.0 Phase F: classify decision outcomes from git.
        from mcp_server.storage.outcomes_writer import cmd_observe_git

        rc = cmd_observe_git(verbose=getattr(args, "verbose", False))
        sys.exit(rc)
    elif args.command == "calibrate":
        # v2.2.0: calibrate removed (no semantic thresholds — FTS5 only).
        print(
            "Error: `codevira calibrate` was removed in v2.2.0.\n"
            "  v2.2.0 uses SQLite FTS5 + BM25 for decision search;\n"
            "  no learnable thresholds. Use `codevira sync` to rebuild\n"
            "  the FTS5 index instead.",
            file=sys.stderr,
        )
        sys.exit(1)
    elif args.command == "engine":
        # Internal — Claude Code hook scripts call us with `engine handle <event>`.
        if getattr(args, "engine_action", None) == "handle":
            # Register every Hero policy that ships enabled-by-default.
            # Without this, the hook runs the engine but ZERO policies
            # are registered → every edit gets ALLOW silently. (Week-4
            # R2 #5 caught this — same class of "silent wiring miss"
            # as Week-1 R3.)
            try:
                from mcp_server.engine import register_default_policies

                register_default_policies()
            except Exception:
                pass  # never let policy registration break the hook
            # Auto-register the engine sprint's demo policy when the
            # env var is set (acceptance-test harness; not used in prod).
            try:
                from mcp_server.engine.demo_policy import maybe_register as _maybe_demo

                _maybe_demo()
            except Exception:
                pass  # never let demo policy registration break the hook
            from mcp_server.engine.wiring.claude_code_hooks import (
                handle as engine_handle,
            )

            sys.exit(engine_handle(args.event_type))
        # Unknown engine action — print usage.
        engine_parser.print_help()
        sys.exit(2)
    else:
        # No subcommand. P1-11 (rc.5): MCP-server stdio mode is correct when
        # stdin is a pipe (Claude Code etc. spawn us this way), but when a
        # human runs `codevira` in a terminal we used to print one cryptic
        # line ("No valid watched_dirs found — watcher not started") and
        # exit. That made it look broken. Print help in interactive mode
        # instead; only enter server mode when stdin is piped from a real
        # MCP client.
        if sys.stdin.isatty():
            parser.print_help()
            print()
            print(
                "  Tip: codevira is normally launched by an AI tool (Claude Code, "
                "Cursor, etc.).\n"
                "       Run `codevira setup` to configure that, or `codevira "
                "--help`\n"
                "       for the subcommand list."
            )
            sys.exit(0)
        cmd_server()


# ---------------------------------------------------------------------------
# cmd_clean
# ---------------------------------------------------------------------------


def cmd_reset(
    vectors: bool = False,
    graph: bool = False,
    reset_all: bool = False,
    no_backup: bool = False,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """2026-05-18 v2.1.2 Item 3b: destructive recovery operations.

    Replaces the destructive flags of `codevira heal`. Each flag wipes
    a specific part of local project state. Decisions / outcomes /
    preferences / learned_rules are AUTO-EXPORTED to
    `<data_dir>/exports/<timestamp>-pre-<target>.json` BEFORE any wipe
    of graph/ unless `--no-backup` is passed.

    Confirmation: typed (user must type 'reset' or the target name).
    `--yes` skips for scripts.

    Returns:
        0 success, 1 error, 2 nothing to do.

    P-principles satisfied:
      P1: emit reason + remediation if no target specified
      P3: rename-style atomic deletion (rename-then-delete)
      P7: scoped recovery — never touches OTHER projects or global.db
      P8: every output line says WHAT + WHY + (next) FIX
    """
    import shutil
    from mcp_server.paths import get_data_dir, get_project_root, is_invalid_project_root
    from mcp_server.cli_export import auto_export_before_destructive
    from mcp_server._prompts import confirm_typed

    # P1: require at least one target.
    if not (vectors or graph or reset_all):
        print(
            "Error: nothing to reset. Pass one of:\n"
            "  --vectors   wipe Chroma vector store (fixes HNSW corruption)\n"
            "  --graph     wipe graph.db (DESTROYS decisions unless --no-backup is omitted)\n"
            "  --all       wipe ALL local state (vectors + graph + sessions)\n"
            "\n"
            "Add --no-backup to skip auto-export (use with caution).\n"
            "Add --dry-run to preview, --yes to skip typed confirmation.",
            file=sys.stderr,
        )
        return 1

    # Guard against $HOME / system dirs.
    rejection = is_invalid_project_root(get_project_root())
    if rejection:
        print(f"Error: {rejection}", file=sys.stderr)
        return 1

    try:
        data_dir = get_data_dir()
    except Exception as e:
        print(f"Error: cannot resolve data dir: {e}", file=sys.stderr)
        return 1

    project_root = get_project_root()

    # Build the list of targets up front so the user sees the FULL plan.
    targets: list[tuple[str, Path]] = []
    if vectors or reset_all:
        codeindex = data_dir / "codeindex"
        if codeindex.exists():
            try:
                size_mb = (
                    sum(f.stat().st_size for f in codeindex.rglob("*") if f.is_file())
                    / 1024
                    / 1024
                )
                targets.append((f"vector store (Chroma) — {size_mb:.1f} MB", codeindex))
            except Exception:
                targets.append(("vector store (Chroma)", codeindex))
    if graph or reset_all:
        graph_path = data_dir / "graph"
        if graph_path.exists():
            targets.append(
                ("graph database (decisions / outcomes / prefs / rules)", graph_path)
            )
    if reset_all:
        sessions = data_dir / "sessions"
        if sessions.exists():
            targets.append(("session logs", sessions))

    # Count what's about to be lost so the confirm prompt can scream.
    decision_count = 0
    outcome_count = 0
    rule_count = 0
    if (graph or reset_all) and (data_dir / "graph" / "graph.db").is_file():
        try:
            import sqlite3 as _sqlite3

            conn = _sqlite3.connect(
                f"file:{data_dir / 'graph' / 'graph.db'}?mode=ro", uri=True
            )
            try:
                for tbl, var in (
                    ("decisions", "decision_count"),
                    ("outcomes", "outcome_count"),
                    ("learned_rules", "rule_count"),
                ):
                    try:
                        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                        if tbl == "decisions":
                            decision_count = n
                        elif tbl == "outcomes":
                            outcome_count = n
                        elif tbl == "learned_rules":
                            rule_count = n
                    except Exception:
                        pass
            finally:
                conn.close()
        except Exception:
            pass

    print()
    print("  Codevira — Reset")
    print(f"  Project: {project_root}")
    print("  " + "─" * 60)
    print()

    if not targets:
        print("  Nothing to reset — none of the targeted state exists on disk.")
        return 2

    # Render the destructive-op summary so the user sees WHAT vanishes.
    print("  ⚠  DESTRUCTIVE OPERATION — will remove:")
    for label, _path in targets:
        print(f"    • {label}")
    if (graph or reset_all) and (decision_count + outcome_count + rule_count) > 0:
        print()
        print("    Inside graph/:")
        if decision_count:
            print(f"      • {decision_count} decision(s)")
        if outcome_count:
            print(f"      • {outcome_count} outcome(s)")
        if rule_count:
            print(f"      • {rule_count} learned rule(s)")
    print()
    if no_backup:
        print("  Auto-backup: OFF (--no-backup). Decisions WILL be lost.")
    else:
        print("  Auto-backup: ON — decisions exported BEFORE wipe.")
    print()

    if dry_run:
        print("  [dry-run] No changes made.")
        return 0

    # Typed confirmation unless --yes.
    if not yes:
        target_word = "all" if reset_all else ("graph" if graph else "vectors")
        ok = confirm_typed(
            f"Type '{target_word}' to confirm this destructive operation.",
            target_word,
        )
        if not ok:
            print("  Aborted.")
            return 0

    # Auto-export before any wipe of graph/.
    if (graph or reset_all) and not no_backup:
        target_kind = "all" if reset_all else "graph"
        backup_path = auto_export_before_destructive(target_kind)
        if backup_path is not None:
            print(f"    ✓ Backed up decisions → {backup_path}")
        else:
            print("    ⚠ Backup attempted but failed (see stderr above).")
            print("      Pass --no-backup to skip this step explicitly,")
            print("      OR fix the backup issue and retry.")
            return 1

    # Execute the wipes — rename-then-delete for atomicity.
    print()
    failures = 0
    for label, path in targets:
        try:
            backup_name = path.with_name(path.name + ".resetting")
            if backup_name.exists():
                shutil.rmtree(backup_name, ignore_errors=True)
            path.rename(backup_name)
            shutil.rmtree(backup_name, ignore_errors=True)
            print(f"    ✓ Removed {label}")
        except Exception as e:
            print(f"    ✗ Failed to remove {label}: {e}")
            failures += 1

    print()
    if failures:
        print(f"  ⚠ {failures} target(s) failed. Check permissions and retry.")
        return 1

    print("  ✓ Reset complete.")
    print()
    print("  Next steps:")
    if vectors or reset_all:
        print("    • Run `codevira index --full` to rebuild the vector store")
    if graph or reset_all:
        print("    • Run `codevira index --full` to rebuild the graph")
        if not no_backup:
            print(
                "    • Decisions backup is at <data_dir>/exports/ — restore via SQLite if needed"
            )
    print()
    return 0


def cmd_heal(
    vectors: bool = False,
    graph: bool = False,
    decisions: bool = False,
    heal_all: bool = False,
    dry_run: bool = False,
    yes: bool = False,
) -> int:
    """Self-service recovery (v2.1.2: non-destructive only — `decisions`).

    History: prior to v2.1.2 this command also handled --vectors / --graph
    / --all (destructive). Those moved to `codevira reset` because
    "heal" implied fix-in-place but the implementation always wiped +
    rebuilt. Field-test reports flagged the naming as the #1 data-loss
    footgun.

    Args:
        vectors / graph / heal_all: DEPRECATED — forwarded to cmd_reset
            with a one-time deprecation warning. Will be removed in v2.2.
        decisions: NON-DESTRUCTIVE backfill — embed every existing decision
            into the semantic search index. Used for v2.0/v2.1.0 →
            v2.1.x upgrades.
        dry_run: print what would be done, don't touch anything
        yes: skip the confirmation prompt
    """
    # 2026-05-18 v2.1.2 Item 3c: deprecation cycle for destructive flags.
    # Forward to cmd_reset with a one-time warning. Remove in v2.2.
    if vectors or graph or heal_all:
        which = []
        if vectors:
            which.append("--vectors")
        if graph:
            which.append("--graph")
        if heal_all:
            which.append("--all")
        print(
            f"\n⚠  DEPRECATED in v2.1.2: `codevira heal {' '.join(which)}` is\n"
            f"   destructive and the name is misleading. Forwarding to:\n"
            f"     codevira reset {' '.join(which)}\n"
            f"   `heal` will accept only non-destructive flags (currently\n"
            f"   only --decisions) starting in v2.2. Update your scripts.\n",
            file=sys.stderr,
        )
        return cmd_reset(
            vectors=vectors,
            graph=graph,
            reset_all=heal_all,
            no_backup=False,  # safety: deprecated path keeps backup ON
            dry_run=dry_run,
            yes=yes,
        )

    import shutil
    from mcp_server.paths import get_data_dir, get_project_root, is_invalid_project_root

    # P1 (helpful errors): require at least one target.
    if not (vectors or graph or decisions or heal_all):
        print(
            "Error: nothing to heal. Pass one of:\n"
            "  --vectors    wipe + rebuild the Chroma vector store (HNSW corruption)\n"
            "  --graph      wipe + rebuild graph.db\n"
            "  --decisions  embed existing decisions into semantic index (v2.0→v2.1 upgrade backfill, non-destructive)\n"
            "  --all        wipe ALL local state for this project\n"
            "Run `codevira doctor` to see what needs healing.",
            file=sys.stderr,
        )
        return 1

    # v2.2.0: --decisions backfill removed (no semantic embeddings to
    # backfill; FTS5 rebuilds from JSONL via `codevira sync`).
    if decisions and not (vectors or graph or heal_all):
        print(
            "v2.2.0: `codevira heal --decisions` removed. ChromaDB embeddings\n"
            "are gone in v2.2.0; decisions live in `.codevira/decisions.jsonl`\n"
            "and the FTS5 index is rebuilt by `codevira sync`.",
            file=sys.stderr,
        )
        return 1

    # Guard against running from $HOME / system dirs (same protection as cmd_index).
    rejection = is_invalid_project_root(get_project_root())
    if rejection:
        print(f"Error: {rejection}", file=sys.stderr)
        return 1

    try:
        data_dir = get_data_dir()
    except Exception as e:
        print(
            f"Error: cannot resolve data dir for current project: {e}", file=sys.stderr
        )
        return 1

    project_root = get_project_root()
    print()
    print("  Codevira — Heal")
    print(f"  Project:    {project_root}")
    print(f"  Data dir:   {data_dir}")
    print("  " + "─" * 50)
    print()

    targets: list[tuple[str, Path]] = []
    if vectors or heal_all:
        codeindex = data_dir / "codeindex"
        if codeindex.exists():
            try:
                size_mb = (
                    sum(f.stat().st_size for f in codeindex.rglob("*") if f.is_file())
                    / 1024
                    / 1024
                )
                targets.append((f"vector store (Chroma) — {size_mb:.1f} MB", codeindex))
            except Exception:
                targets.append(("vector store (Chroma) — size unknown", codeindex))
        else:
            print("    · vector store: nothing to do (codeindex/ doesn't exist)")
    if graph or heal_all:
        graph_db = data_dir / "graph"
        if graph_db.exists():
            targets.append(("graph database (graph.db)", graph_db))
        else:
            print("    · graph: nothing to do (graph/ doesn't exist)")
    if heal_all:
        sessions = data_dir / "sessions"
        if sessions.exists():
            targets.append(("session logs", sessions))

    if not targets:
        print("\n  Nothing to heal — no corrupted state found.")
        print("  If the issue persists, run `codevira doctor` to diagnose.")
        return 2

    print("  Will remove:")
    for label, _ in targets:
        print(f"    • {label}")
    print()

    if dry_run:
        print("  [dry-run] no changes made.")
        return 0

    if not yes:
        # Bug 22 (rc.4): use shared confirm() helper.
        try:
            from mcp_server._prompts import confirm

            if not confirm("Proceed with heal?", default=False):
                print("  Aborted.")
                return 0
        except Exception:
            # P9 (graceful): if the prompt helper isn't available, fall
            # back to a basic confirmation rather than failing the heal.
            response = input("  Proceed? [y/N]: ").strip().lower()
            if response not in ("y", "yes"):
                print("  Aborted.")
                return 0

    # P3 (atomic-ish): rename-then-delete pattern so a Ctrl-C mid-heal
    # doesn't leave a half-deleted directory that confuses subsequent
    # operations. Rename → then shutil.rmtree the renamed copy.
    print()
    failures = 0
    for label, path in targets:
        try:
            backup = path.with_name(path.name + ".healing")
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
            path.rename(backup)
            shutil.rmtree(backup, ignore_errors=True)
            print(f"    ✓ Removed {label}")
        except Exception as e:
            print(f"    ✗ Failed to remove {label}: {e}")
            failures += 1

    print()
    if failures:
        print(f"  ⚠ {failures} target(s) failed. Check permissions and retry.")
        return 1

    print("  ✓ Heal complete.")
    print()
    print("  Next steps:")
    if vectors or heal_all:
        print("    • Run `codevira index --full` to rebuild the vector store")
    elif graph or heal_all:
        print("    • Run `codevira index --full` to rebuild the graph")
    print()
    return 0


def cmd_clean(
    clean_all: bool = False,
    dry_run: bool = False,
    yes: bool = False,
    legacy_only: bool = False,
    orphans_only: bool = False,
    ghosts_only: bool = False,
) -> None:
    """Remove all Codevira data, IDE configs, and services.

    With --legacy, ONLY removes .codevira.migrated/ backup directories from
    project repos (left over from the v1.5 → v1.6 storage migration).

    With --orphans (v1.8.1), ONLY removes project data dirs whose
    ``original_path`` is no longer a valid project root ($HOME, system
    dir, or deleted directory). This is the recovery path for users who
    accidentally bootstrapped a project at $HOME on v1.8.0.

    With --ghosts (rc.5 / P2-4), ONLY removes project data dirs classified
    as 'ghost' by the canonical inventory helper — present on disk but
    missing config.yaml or metadata.json (created as a side effect of MCP
    tool calls that didn't complete the full init). Surgical cleanup;
    preserves tracked projects and their indexes.
    """
    import shutil

    if ghosts_only:
        _cmd_clean_ghosts(dry_run=dry_run, yes=yes)
        return
    if orphans_only:
        _cmd_clean_orphans(dry_run=dry_run, yes=yes)
        return

    if legacy_only:
        _cmd_clean_legacy_only(dry_run=dry_run, yes=yes)
        return

    from mcp_server.paths import get_global_home
    from mcp_server.ide_inject import (
        _claude_global_config_path,
        _claude_desktop_config_path,
        _cursor_global_config_path,
        _windsurf_global_config_path,
        _antigravity_config_path,
        remove_codevira_from_config,
    )

    from typing import Callable as _Callable

    actions: list[tuple[str, _Callable[[], object]]] = []
    print()
    print("  Codevira — Clean Setup")
    print("  " + "─" * 40)
    print()

    # 1. Global data directory
    # P0-3 (rc.5): use the canonical inventory helper so the count here
    # agrees with `status --global` and `codevira projects` summary.
    global_home = get_global_home()
    if global_home.exists():
        try:
            from mcp_server._project_inventory import enumerate_projects, summarize

            inv = summarize(enumerate_projects())
            count_breakdown = (
                f"{inv['tracked']} tracked"
                + (f", {inv['ghost']} ghost" if inv["ghost"] else "")
                + (f", {inv['orphan']} orphan" if inv["orphan"] else "")
                + (f", {inv['stale']} stale" if inv["stale"] else "")
            )
        except Exception:
            count_breakdown = "(count unavailable)"
        try:
            total_size = sum(
                f.stat().st_size for f in global_home.rglob("*") if f.is_file()
            )
            size_str = f"{total_size / 1024 / 1024:.1f} MB"
        except Exception:
            size_str = "unknown size"
        print(f"    • ~/.codevira/ ({count_breakdown}; {size_str})")
        actions.append(
            (
                "Removed ~/.codevira/",
                lambda: shutil.rmtree(global_home, ignore_errors=True),
            )
        )

    # 2. IDE configs
    ide_configs = [
        ("Claude Code global", _claude_global_config_path()),
        ("Claude Desktop", _claude_desktop_config_path()),
        ("Cursor global", _cursor_global_config_path()),
        ("Windsurf global", _windsurf_global_config_path()),
        ("Antigravity", _antigravity_config_path()),
    ]
    for ide_name, config_path in ide_configs:
        if config_path.exists():
            from mcp_server.ide_inject import _read_json_safe

            data = _read_json_safe(config_path)
            servers = data.get("mcpServers", {})
            has_codevira = any(
                k == "codevira" or k.startswith("codevira-") for k in servers
            )
            if has_codevira:
                print(f"    • {ide_name} config (mcpServers.codevira)")
                _cp: Path = config_path

                def _remove_codevira(p: Path = _cp) -> object:
                    return remove_codevira_from_config(p)

                actions.append(
                    (
                        f"Removed codevira from {ide_name}",
                        _remove_codevira,
                    )
                )

    # 3. Launchd service
    plist_path = (
        Path.home() / "Library" / "LaunchAgents" / "com.codevira.mcp-serve.plist"
    )
    if plist_path.exists():
        print("    • Launchd service (com.codevira.mcp-serve)")

        def _unload_launchd():
            try:
                from mcp_server.launchd import uninstall_launchd

                uninstall_launchd()
            except Exception:
                plist_path.unlink(missing_ok=True)

        actions.append(("Unloaded launchd service", _unload_launchd))

    # 4. Server log
    log_path = Path.home() / "Library" / "Logs" / "codevira.log"
    if log_path.exists():
        print("    • ~/Library/Logs/codevira.log")
        actions.append(("Removed server log", lambda: log_path.unlink(missing_ok=True)))

    # 4b. Claude Code lifecycle hooks
    # 2026-05-17 Bug A (P7 reversible operations): `codevira clean` was
    # leaving orphaned `~/.claude/hooks/codevira-*.sh` scripts behind
    # AND stale entries in `~/.claude/settings.json` hooks block.
    # `setup` installs them; `clean` now removes them. Single complete
    # uninstall path.
    hooks_dir = Path.home() / ".claude" / "hooks"
    if hooks_dir.is_dir():
        codevira_hooks = sorted(hooks_dir.glob("codevira-*.sh"))
        if codevira_hooks:
            print(
                f"    • ~/.claude/hooks/codevira-*.sh ({len(codevira_hooks)} script(s))"
            )

            def _remove_hooks():
                # Delegate to the canonical uninstaller — it removes the
                # scripts AND drops the codevira entries from
                # ~/.claude/settings.json's hooks block (which raw rm
                # would leave stale).
                try:
                    from mcp_server.cli_hooks_admin import cmd_hooks_uninstall

                    cmd_hooks_uninstall(dry_run=False, yes=True)
                except Exception as e:
                    # P9 graceful: if the canonical path fails, fall back
                    # to raw rm so the user isn't blocked.
                    import logging

                    logging.getLogger(__name__).warning(
                        "cmd_hooks_uninstall failed (%s) — falling back to rm", e
                    )
                    for h in codevira_hooks:
                        h.unlink(missing_ok=True)

            actions.append(
                ("Removed Claude Code hooks + settings entries", _remove_hooks)
            )

    # 5. Per-project artifacts (only with --all)
    if clean_all and global_home.exists():
        projects_dir = global_home / "projects"
        if projects_dir.exists():
            for meta_file in projects_dir.glob("*/metadata.json"):
                try:
                    import json

                    meta = json.loads(meta_file.read_text())
                    project_path = Path(meta.get("original_path", ""))
                    if project_path.exists():
                        _collect_project_cleanup(project_path, actions)
                except Exception:
                    continue

    if not actions:
        print("    Nothing to clean — Codevira is not installed.")
        print()
        return

    print()

    # Confirmation
    if dry_run:
        print("  [dry-run] No changes made.")
        print()
        return

    if not yes:
        # Bug 22 (rc.4): shared confirm() helper.
        from mcp_server._prompts import confirm

        if not confirm("Remove all of the above?", default=False):
            print("  Aborted.")
            print()
            return

    print()
    for label, action in actions:
        try:
            action()
            print(f"    {label:<45} done")
        except Exception as e:
            print(f"    {label:<45} FAILED ({e})")

    print()
    print("  ✓ Codevira fully removed.")
    # 2026-05-17 Bug B: post-clean text said "codevira register" which is
    # the deprecated v1.x command. v2.0+ uses `codevira setup` as the
    # one-shot install/configure entry point.
    print("    To reinstall: pipx install codevira && codevira setup")
    print()


def _collect_project_cleanup(project_path: Path, actions: list) -> None:
    """Collect per-project cleanup actions."""
    import shutil
    from mcp_server.ide_inject import remove_codevira_from_config

    name = project_path.name

    # Legacy .codevira/ dir
    legacy = project_path / ".codevira"
    if legacy.exists():
        print(f"    • {name}/.codevira/")
        actions.append(
            (
                f"Removed {name}/.codevira/",
                lambda p=legacy: shutil.rmtree(p, ignore_errors=True),
            )
        )

    # Migration backup
    migrated = project_path / ".codevira.migrated"
    if migrated.exists():
        print(f"    • {name}/.codevira.migrated/")
        actions.append(
            (
                f"Removed {name}/.codevira.migrated/",
                lambda p=migrated: shutil.rmtree(p, ignore_errors=True),
            )
        )

    # Git hook
    hook = project_path / ".git" / "hooks" / "post-commit"
    if hook.exists():
        try:
            content = hook.read_text()
            if "codevira" in content.lower() or "Codevira" in content:
                print(f"    • {name}/.git/hooks/post-commit")
                # Check if hook has backup
                backup = hook.with_suffix(".bak")
                if backup.exists():
                    actions.append(
                        (
                            f"Restored {name} git hook from backup",
                            lambda h=hook, b=backup: b.rename(h),
                        )
                    )
                else:
                    actions.append(
                        (
                            f"Removed {name} git hook",
                            lambda h=hook: h.unlink(missing_ok=True),
                        )
                    )
        except Exception:
            pass

    # Per-project IDE configs
    for ide_name, config_path in [
        ("claude", project_path / ".claude" / "settings.json"),
        ("cursor", project_path / ".cursor" / "mcp.json"),
        ("windsurf", project_path / ".windsurf" / "mcp.json"),
    ]:
        if config_path.exists():
            from mcp_server.ide_inject import _read_json_safe

            data = _read_json_safe(config_path)
            if "codevira" in data.get("mcpServers", {}):
                print(f"    • {name}/.{ide_name} config")
                actions.append(
                    (
                        f"Removed codevira from {name}/{ide_name}",
                        lambda p=config_path: remove_codevira_from_config(p),
                    )
                )


def _cmd_clean_ghosts(dry_run: bool = False, yes: bool = False) -> None:
    """P2-4 (rc.5): remove only the dirs classified as 'ghost' by the
    canonical inventory helper. Pairs with ``codevira projects --ghosts-only``
    so the user can list first, then delete.

    A ghost is a ``~/.codevira/projects/<slug>/`` that exists on disk but is
    missing ``config.yaml`` or ``metadata.json`` — i.e. has SOME state from
    an MCP tool call but the bookkeeping never completed.

    2026-05-18 v2.1.2 Item 14: ALSO catches truly-empty data dirs
    (status='stale' but disk has only a graph/ shell or similar
    bookkeeping skeleton with no real content). Sachin's machine had 3
    such dirs from sub-bootstrap that fell through to 'stale' and were
    never cleaned. We promote them to ghost candidates if the dir
    exists, is small (<10 KB), and contains no decisions / nodes.
    """
    import shutil
    from mcp_server._project_inventory import enumerate_projects

    ghosts = [e for e in enumerate_projects() if e.status == "ghost" and e.slug]

    # 2026-05-18 v2.1.2 Item 14: pick up empty-dir 'stale' entries too.
    empty_stale: list = []
    for e in enumerate_projects():
        if e.status != "stale" or not e.slug or not e.has_data_dir:
            continue
        # Heuristic: truly empty = directory is small (<10 KB) AND
        # contains no real graph data (zero decisions OR no graph.db).
        try:
            if e.size_bytes > 10 * 1024:
                continue
        except Exception:
            continue
        empty_stale.append(e)

    all_candidates = ghosts + empty_stale
    if not all_candidates:
        print("✓ No ghost projects or empty data dirs on this machine.")
        return

    print()
    print("  Codevira — Clean Ghost / Empty Projects")
    print("  " + "─" * 42)
    print()
    if ghosts:
        print(f"  Found {len(ghosts)} ghost dir(s):")
        for e in ghosts:
            size_kb = e.size_bytes // 1024
            print(f"    • {e.slug}  ({size_kb:,} KB)")
    if empty_stale:
        print(f"  Found {len(empty_stale)} empty stale data dir(s):")
        for e in empty_stale:
            size_kb = e.size_bytes // 1024
            print(f"    • {e.slug}  ({size_kb:,} KB)")
    print()

    if dry_run:
        print("  [dry-run] No changes made.")
        return

    if not yes:
        # Reuse the shared confirm helper added in Bug 22 / rc.4.
        from mcp_server._prompts import confirm

        if not confirm(
            f"Remove {len(all_candidates)} dir(s) "
            f"({len(ghosts)} ghost, {len(empty_stale)} empty)?",
            default=False,
        ):
            print("  Aborted.")
            return

    print()
    try:
        from mcp_server.paths import get_global_home

        projects_root = get_global_home() / "projects"
    except Exception as exc:
        print(f"  ✗ could not resolve projects directory: {exc}")
        return

    removed = 0
    for e in all_candidates:
        if e.slug is None:
            continue
        target = projects_root / e.slug
        try:
            shutil.rmtree(target, ignore_errors=True)
            print(f"  ✓ removed {e.slug}")
            removed += 1
        except Exception as exc:
            print(f"  ✗ {e.slug}: {exc}")
    print()
    print(f"  Done: removed {removed} of {len(all_candidates)} dir(s).")


def _cmd_clean_orphans(dry_run: bool = False, yes: bool = False) -> None:
    """Remove project data dirs whose ``original_path`` is no longer a
    valid project root.

    "Orphan" definition (v1.8.1):
      1. ``original_path`` is rejected by ``is_invalid_project_root``
         (it's $HOME, /, /Users, /tmp, /var, /etc, /opt, etc.).
      2. ``original_path`` no longer exists on disk (project moved or
         deleted; the data_dir is now dead weight).

    For each orphan: the data dir under ``~/.codevira/projects/<slug>/``
    is removed, AND the matching row is deleted from
    ``~/.codevira/global.db`` so cross-project intelligence doesn't keep
    referencing a defunct path.

    This recovers users who accidentally bootstrapped a project at $HOME
    on v1.8.0 — the production-crash failure mode this entire release
    addresses. Without this command they would need to ``rm -rf`` and run
    raw sqlite by hand.
    """
    import json
    import shutil
    import sqlite3
    from mcp_server.paths import (
        get_global_home,
        get_global_db_path,
        is_invalid_project_root,
    )

    print()
    print("  Codevira — Clean Orphan Project Data")
    print("  " + "─" * 38)
    print()

    global_home = get_global_home()
    projects_dir = global_home / "projects"

    # (data_dir, original_path, reason)
    found: list[tuple[Path, str, str]] = []
    if projects_dir.exists():
        for meta_file in sorted(projects_dir.glob("*/metadata.json")):
            try:
                meta = json.loads(meta_file.read_text())
                original_path = meta.get("original_path", "")
                if not original_path:
                    continue
                op = Path(original_path)
                rejection = is_invalid_project_root(op)
                if rejection:
                    found.append((meta_file.parent, original_path, rejection))
                    continue
                # Resolve safely; if it raises, treat as missing (the
                # filesystem said something weird and we don't want to mask).
                try:
                    exists = op.exists()
                except (OSError, RuntimeError):
                    exists = False
                if not exists:
                    found.append(
                        (
                            meta_file.parent,
                            original_path,
                            f"original_path no longer exists: {original_path}",
                        )
                    )
            except Exception:
                # Unreadable metadata.json is its own orphan-shaped problem,
                # but we don't auto-delete unreadable state — surface it for
                # manual inspection rather than risk eating real data.
                continue

    # 2026-05-18 v2.1.2 Item 13: also scan global.db.projects for rows whose
    # path doesn't exist on disk AND has no matching data dir. These bare
    # rows are invisible to the per-data-dir loop above and pollute
    # `codevira projects` output forever (Sachin's machine had 13 such rows).
    bare_global_rows: list[tuple[str, str]] = []  # (path, reason)
    db_path = get_global_db_path()
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path), timeout=30)
            try:
                cur = conn.execute("SELECT path FROM projects")
                for (gpath,) in cur.fetchall():
                    if not gpath:
                        continue
                    p = Path(gpath)
                    # If path corresponds to a current data dir we already
                    # processed above, skip it.
                    if any(str(d) == gpath for d, _o, _r in found):
                        continue
                    try:
                        exists = p.exists()
                    except (OSError, RuntimeError):
                        exists = False
                    if exists:
                        continue
                    # Bare row: global.db references a path that doesn't exist
                    # and there's no data dir on disk for it.
                    bare_global_rows.append(
                        (gpath, "global.db row points at missing path; no data dir")
                    )
            finally:
                conn.close()
        except Exception as exc:
            print(f"  (warning: could not scan global.db for bare rows: {exc})")

    total_orphans = len(found) + len(bare_global_rows)
    if total_orphans == 0:
        print("  No orphan project data directories or bare global.db rows found.")
        print()
        return

    print(
        f"  Found {total_orphans} orphan(s): {len(found)} data dir(s), "
        f"{len(bare_global_rows)} bare global.db row(s):"
    )
    for data_dir, op, reason in found:  # type: ignore[assignment]
        print(f"    • {data_dir}")
        print(f"        original_path: {op}")
        print(f"        reason: {reason}")
    for gpath, reason in bare_global_rows:
        print(f"    • [global.db row] {gpath}")
        print(f"        reason: {reason}")
    print()

    if dry_run:
        print("  [dry-run] No changes made.")
        print()
        return

    if not yes:
        answer = (
            input(
                f"  Remove {len(found)} orphan data dir(s) and "
                f"{len(bare_global_rows)} bare global.db row(s)? [y/N] "
            )
            .strip()
            .lower()
        )
        if answer != "y":
            print("  Aborted.")
            print()
            return

    print()
    removed_dirs = 0
    removed_rows = 0
    # First: data-dir cleanup loop (existing behavior).
    for data_dir, _op, _reason in found:
        # 1. Remove the centralized data dir
        try:
            shutil.rmtree(data_dir, ignore_errors=False)
            removed_dirs += 1
            print(f"    ✓ Removed {data_dir}")
        except Exception as e:
            print(f"    ✗ {data_dir}  FAILED ({e})")
            continue

        # 2. Delete the matching row from global.db (path key = data_dir str)
        try:
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), timeout=30)
                try:
                    cur = conn.execute(
                        "DELETE FROM projects WHERE path = ?",
                        (str(data_dir),),
                    )
                    conn.commit()
                    if cur.rowcount > 0:
                        removed_rows += cur.rowcount
                finally:
                    conn.close()
        except Exception as e:
            print(f"      (warning: could not delete global.db row: {e})")

    # 2026-05-18 v2.1.2 Item 13: also delete bare global.db rows.
    for gpath, _reason in bare_global_rows:
        try:
            if db_path.exists():
                conn = sqlite3.connect(str(db_path), timeout=30)
                try:
                    cur = conn.execute("DELETE FROM projects WHERE path = ?", (gpath,))
                    conn.commit()
                    if cur.rowcount > 0:
                        removed_rows += cur.rowcount
                        print(f"    ✓ Removed bare global.db row {gpath}")
                finally:
                    conn.close()
        except Exception as e:
            print(f"    ✗ [global.db row] {gpath}  FAILED ({e})")

    print()
    print(
        f"  ✓ Removed {removed_dirs} of {len(found)} orphan data dir(s); "
        f"{removed_rows} global.db row(s) deleted."
    )
    print()


def _cmd_clean_legacy_only(dry_run: bool = False, yes: bool = False) -> None:
    """Remove .codevira.migrated/ backup dirs from all known projects.

    These directories are created by the v1.5 → v1.6 storage migration as
    safety-net backups. They're harmless but accumulate over time.
    """
    import json
    from mcp_server.paths import get_global_home
    from mcp_server.migrate import cleanup_legacy_dir

    print()
    print("  Codevira — Clean Legacy Migration Backups")
    print("  " + "─" * 44)
    print()

    global_home = get_global_home()
    projects_dir = global_home / "projects"

    found: list[Path] = []
    if projects_dir.exists():
        for meta_file in projects_dir.glob("*/metadata.json"):
            try:
                meta = json.loads(meta_file.read_text())
                project_path = Path(meta.get("original_path", ""))
                if project_path.exists():
                    backup = project_path / ".codevira.migrated"
                    if backup.exists():
                        found.append(project_path)
            except Exception:
                continue

    if not found:
        print("  No legacy backup directories found. Nothing to clean.")
        print()
        return

    print(f"  Found {len(found)} project(s) with .codevira.migrated/ backups:")
    for p in found:
        try:
            size_kb = (
                sum(
                    f.stat().st_size
                    for f in (p / ".codevira.migrated").rglob("*")
                    if f.is_file()
                )
                / 1024
            )
            print(f"    • {p}/.codevira.migrated/  ({size_kb:.0f} KB)")
        except Exception:
            print(f"    • {p}/.codevira.migrated/")

    print()
    if dry_run:
        print("  [dry-run] No changes made.")
        print()
        return

    if not yes:
        # Bug 22 (rc.4): shared confirm() helper.
        from mcp_server._prompts import confirm

        if not confirm(f"Delete {len(found)} backup dir(s)?", default=False):
            print("  Aborted.")
            print()
            return

    print()
    removed = 0
    for project_path in found:
        try:
            if cleanup_legacy_dir(project_path):
                removed += 1
                print(f"    ✓ Removed {project_path.name}/.codevira.migrated/")
        except Exception as e:
            print(f"    ✗ {project_path.name}/.codevira.migrated/  FAILED ({e})")

    print()
    print(f"  ✓ Removed {removed} of {len(found)} legacy backup directories.")
    print()


if __name__ == "__main__":
    main()
