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
        ".git", "pyproject.toml", "setup.py", "setup.cfg",
        "package.json", "Cargo.toml", "go.mod", "Makefile",
        "pom.xml", "build.gradle",
    ]
    return any((path / m).exists() for m in markers)


def cmd_init() -> None:
    """Initialize Codevira in the current project."""
    from mcp_server.paths import get_project_root, get_data_dir, get_package_data_dir
    import shutil
    import yaml

    cwd = get_project_root()
    data_dir = get_data_dir()

    print()
    print("  Codevira — Project Initialization")
    print("  " + "─" * 40)
    print()

    # Step 1: Validate project root
    if not _detect_project_root_markers(cwd):
        parent = cwd.parent
        if _detect_project_root_markers(parent):
            print(f"  Warning: It looks like you may be in a subdirectory.")
            print(f"  Project markers found in: {parent}")
            print(f"  Current directory:        {cwd}")
            print()
            answer = input("  Continue initializing here anyway? [y/N] ").strip().lower()
            if answer != "y":
                print("  Aborted. Run `codevira init` from your project root.")
                sys.exit(0)
            print()

    # Step 2a: Auto-migrate legacy .codevira/ if present
    git_dir = cwd / ".git"
    from mcp_server.migrate import detect_migration_needed, migrate_to_centralized
    if detect_migration_needed(cwd):
        print(f"  Migrating legacy .codevira/ to centralized storage ...", end="", flush=True)
        try:
            result = migrate_to_centralized(cwd)
            if result.get("migrated"):
                print(f" done ({result.get('files_copied', 0)} files → {result.get('new_path', '')})")
                # Re-evaluate data_dir after migration — now points to centralized path
                data_dir = get_data_dir()
            else:
                print(f" skipped ({result.get('reason', '')})")
        except Exception as e:
            print(f" failed ({e})")

    # Step 2b: Create centralized directory structure
    is_centralized = str(data_dir).startswith(str(Path.home() / ".codevira" / "projects"))
    if is_centralized:
        print(f"  Creating centralized data dir ...")
        print(f"    {data_dir}")
    else:
        print(f"  Creating .codevira/ in {cwd} ...")
    for subdir in ["graph/changesets", "codeindex", "logs"]:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)
    print(f"  Data directory ready ...                      done")

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
            print(f"  Adding .codevira/ to .gitignore ...          ", end="", flush=True)
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
    detected = auto_detect_project(cwd)

    # Apply CLI overrides if provided (parsed from args later)
    if hasattr(cmd_init, '_overrides'):
        overrides = cmd_init._overrides
        if overrides.get("name"): detected["name"] = overrides["name"]
        if overrides.get("language"): detected["language"] = overrides["language"]
        if overrides.get("dirs"): detected["watched_dirs"] = [d.strip() for d in overrides["dirs"].split(",")]
        if overrides.get("ext"): detected["file_extensions"] = [e.strip() for e in overrides["ext"].split(",")]

    print(f"  Auto-detected:")
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
    import contextlib, io
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
        try:
            from mcp_server.crash_logger import log_crash
            log_crash(e, context="codevira init: index build", project_path=str(cwd))
        except Exception: pass

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
        try:
            from mcp_server.crash_logger import log_crash
            log_crash(e, context="codevira init: graph stubs", project_path=str(cwd))
        except Exception: pass

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
        try:
            from mcp_server.crash_logger import log_crash
            log_crash(e, context="codevira init: roadmap bootstrap", project_path=str(cwd))
        except Exception: pass

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
            try:
                from mcp_server.crash_logger import log_crash
                log_crash(e, context="codevira init: git hook", project_path=str(cwd))
            except Exception: pass

    # Step 9: Auto-inject IDE configurations
    print()
    print("  " + "─" * 60)
    print(f"  ✓  Codevira initialized in {data_dir}")
    print()

    no_inject = getattr(cmd_init, '_no_inject', False)
    if not no_inject:
        print("  Configuring AI tools ...              ", end="", flush=True)
        try:
            from mcp_server.ide_inject import inject_ide_config
            results = inject_ide_config(cwd, project_name=detected["name"])
            if results:
                print("done")
                for ide_name, config_path in results.items():
                    print(f"    ✓ {ide_name}: {config_path}")
            else:
                print("no AI tools detected")
        except Exception as e:
            print(f"skipped ({e})")
            try:
                from mcp_server.crash_logger import log_crash
                log_crash(e, context="codevira init: IDE inject", project_path=str(cwd))
            except Exception: pass

    # Step 10: Register in global memory (with git_remote for rename-resilient lookup)
    try:
        from mcp_server.global_sync import import_global_to_project
        from mcp_server.paths import get_global_db_path, _get_git_remote_url
        from indexer.global_db import GlobalDB
        from mcp_server.auto_init import _write_metadata

        git_remote = _get_git_remote_url(cwd)
        gdb = GlobalDB(get_global_db_path())
        gdb.register_project(str(data_dir), detected["name"], detected["language"], git_remote=git_remote)
        proj_count = gdb.get_project_count()
        gdb.close()
        if proj_count > 1:
            print(f"  Registered in global memory ({proj_count} projects)")

        # Write metadata.json for centralized storage marker
        _write_metadata(data_dir, cwd)
    except Exception as e:
        print(f"  Global memory registration skipped ({e})")
        try:
            from mcp_server.crash_logger import log_crash
            log_crash(e, context="codevira init: global memory register", project_path=str(cwd))
        except Exception: pass

    # Print config for undetected tools — use the resolved binary path,
    # not the Python interpreter, so users get a clean command.
    from mcp_server.ide_inject import _resolve_command
    cmd_path, python_exe = _resolve_command()
    project_path = str(cwd)

    is_python_fallback = (cmd_path == python_exe)
    print()
    print("  For other AI tools, add this to their MCP config:")
    print()
    print('  {')
    print('    "mcpServers": {')
    print('      "codevira": {')
    if is_python_fallback:
        print(f'        "command": "{python_exe}",')
        print(f'        "args": ["-m", "mcp_server", "--project-dir", "{project_path}"]')
    else:
        print(f'        "command": "{cmd_path}",')
        print(f'        "args": ["--project-dir", "{project_path}"]')
    print('      }')
    print('    }')
    print('  }')
    print()
    print("  Verify: ask your agent to call get_roadmap()")
    print()


def cmd_index(full: bool = False, quiet: bool = False) -> None:
    """Run the indexer (incremental by default, or --full for complete rebuild)."""
    from indexer.index_codebase import cmd_full_rebuild, cmd_incremental

    if full:
        cmd_full_rebuild()
    else:
        cmd_incremental(quiet=quiet)


def cmd_status(check_stale: bool = False) -> None:
    """Show index health and statistics."""
    from indexer.index_codebase import cmd_status as _cmd_status
    _cmd_status(check_stale=check_stale)


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


def cmd_serve(
    host: str = "127.0.0.1",
    port: int = 7007,
    use_https: bool = False,
    project_dir: Path | None = None,
    install_service: bool = False,
    uninstall_service: bool = False,
) -> None:
    """Start the MCP HTTP server (Streamable HTTP transport)."""
    if install_service:
        from mcp_server.launchd import install_launchd
        try:
            plist = install_launchd(port=port, use_https=use_https, host=host, project_dir=project_dir)
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
    """
    from mcp_server.paths import get_project_root
    from mcp_server.ide_inject import (
        _resolve_command, detect_installed_ides,
        inject_global_claude_code, inject_global_cursor, inject_global_windsurf,
        _inject_claude_desktop, inject_claude_http_url,
    )

    project_root = get_project_root()
    cmd_path, python_exe = _resolve_command()

    print()
    print("  Codevira — Global IDE Registration (v1.6)")
    print("  " + "─" * 44)
    print()

    if http_url:
        path = inject_claude_http_url(http_url)
        print(f"  ✓ Claude Code (HTTP URL): {path}")
        print()
        return

    if claude_desktop:
        path = _inject_claude_desktop(project_root, cmd_path, python_exe)
        print(f"  ✓ Claude Desktop: {path}")
        print("  Note: Claude Desktop uses stdio — restart it to pick up changes.")
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

    parser = argparse.ArgumentParser(
        prog="codevira",
        description="Codevira — AI context layer for your codebase",
    )
    parser.add_argument(
        "--project-dir",
        metavar="PATH",
        help="Project directory (alternative to cwd; useful for Google Antigravity)",
    )

    subparsers = parser.add_subparsers(dest="command")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize Codevira in the current project")
    init_parser.add_argument("--name", help="Override project name")
    init_parser.add_argument("--language", help="Override detected language")
    init_parser.add_argument("--dirs", help="Override source directories (comma-separated)")
    init_parser.add_argument("--ext", help="Override file extensions (comma-separated)")
    init_parser.add_argument("--no-inject", action="store_true", help="Skip auto-injecting IDE configs")

    # index
    index_parser = subparsers.add_parser("index", help="Run the code indexer")
    index_parser.add_argument("--full", action="store_true", help="Full rebuild from scratch")
    index_parser.add_argument("--quiet", action="store_true", help="Suppress output (used by git hook)")

    # status
    status_parser = subparsers.add_parser("status", help="Show index health and statistics")
    status_parser.add_argument(
        "--check-stale",
        action="store_true",
        help="Scan source files to detect changes since last index (slower)",
    )

    # report
    report_parser = subparsers.add_parser("report", help="Show recent crash logs")
    report_parser.add_argument("--limit", type=int, default=20, help="Number of recent crashes to show (default: 20)")
    report_parser.add_argument("--clear", action="store_true", help="Clear the crash log")

    # serve
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start MCP HTTP server (Streamable HTTP transport — register via URL)",
    )
    serve_parser.add_argument(
        "--port", type=int, default=7007,
        help="TCP port to listen on (default: 7007)",
    )
    serve_parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 for LAN access)",
    )
    serve_parser.add_argument(
        "--https", action="store_true",
        help="Enable HTTPS using mkcert certs from ~/.codevira/certs/",
    )
    serve_parser.add_argument(
        "--install-service", action="store_true",
        help="Install macOS launchd service so the server starts automatically on login",
    )
    serve_parser.add_argument(
        "--uninstall-service", action="store_true",
        help="Remove the macOS launchd service",
    )
    serve_parser.add_argument(
        "--project-dir",
        metavar="PATH",
        help="Project directory override (same as the global --project-dir flag)",
    )

    # register (v1.6: one-time global IDE registration)
    register_parser = subparsers.add_parser(
        "register",
        help="One-time global IDE registration — inject Codevira into all detected AI tools",
    )
    register_parser.add_argument(
        "--claude-desktop", action="store_true",
        help="Only configure Claude Desktop (stdio mode)",
    )
    register_parser.add_argument(
        "--http-url",
        metavar="URL",
        help="Inject an HTTP URL config into Claude Code (e.g. https://localhost:7443/mcp)",
    )

    # clean
    clean_parser = subparsers.add_parser(
        "clean",
        help="Remove all Codevira data, IDE configs, and services",
    )
    clean_parser.add_argument(
        "--all", action="store_true",
        help="Also clean per-project artifacts (legacy .codevira/, git hooks, per-project IDE configs)",
    )
    clean_parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be removed without deleting anything",
    )
    clean_parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip confirmation prompt",
    )

    args = parser.parse_args(raw_args)

    if args.command == "init":
        # Pass overrides via function attribute (avoids changing signature)
        cmd_init._overrides = {
            "name": getattr(args, "name", None),
            "language": getattr(args, "language", None),
            "dirs": getattr(args, "dirs", None),
            "ext": getattr(args, "ext", None),
        }
        cmd_init._no_inject = getattr(args, "no_inject", False)
        cmd_init()
    elif args.command == "index":
        cmd_index(full=args.full, quiet=args.quiet)
    elif args.command == "status":
        cmd_status(check_stale=getattr(args, "check_stale", False))
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
    elif args.command == "register":
        cmd_register(
            claude_desktop=getattr(args, "claude_desktop", False),
            http_url=getattr(args, "http_url", None),
        )
    elif args.command == "clean":
        cmd_clean(
            clean_all=getattr(args, "all", False),
            dry_run=getattr(args, "dry_run", False),
            yes=getattr(args, "yes", False),
        )
    else:
        # No subcommand → start MCP server (stdio transport)
        cmd_server()


# ---------------------------------------------------------------------------
# cmd_clean
# ---------------------------------------------------------------------------

def cmd_clean(clean_all: bool = False, dry_run: bool = False, yes: bool = False) -> None:
    """Remove all Codevira data, IDE configs, and services."""
    import shutil

    from mcp_server.paths import get_global_home
    from mcp_server.ide_inject import (
        _claude_global_config_path,
        _claude_desktop_config_path,
        _cursor_global_config_path,
        _windsurf_global_config_path,
        _antigravity_config_path,
        remove_codevira_from_config,
    )

    actions: list[tuple[str, callable]] = []
    print()
    print("  Codevira — Clean Setup")
    print("  " + "─" * 40)
    print()

    # 1. Global data directory
    global_home = get_global_home()
    if global_home.exists():
        # Count projects and size
        projects_dir = global_home / "projects"
        project_count = len(list(projects_dir.iterdir())) if projects_dir.exists() else 0
        try:
            total_size = sum(f.stat().st_size for f in global_home.rglob("*") if f.is_file())
            size_str = f"{total_size / 1024 / 1024:.1f} MB"
        except Exception:
            size_str = "unknown size"
        print(f"    • ~/.codevira/ ({project_count} projects, {size_str})")
        actions.append(("Removed ~/.codevira/", lambda: shutil.rmtree(global_home, ignore_errors=True)))

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
            has_codevira = any(k == "codevira" or k.startswith("codevira-") for k in servers)
            if has_codevira:
                print(f"    • {ide_name} config (mcpServers.codevira)")
                actions.append(
                    (f"Removed codevira from {ide_name}",
                     lambda p=config_path: remove_codevira_from_config(p))
                )

    # 3. Launchd service
    plist_path = Path.home() / "Library" / "LaunchAgents" / "com.codevira.mcp-serve.plist"
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
        print(f"    • ~/Library/Logs/codevira.log")
        actions.append(("Removed server log", lambda: log_path.unlink(missing_ok=True)))

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
        answer = input("  Remove all of the above? [y/N] ").strip().lower()
        if answer != "y":
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
    print("    To reinstall: pipx install codevira && codevira register")
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
        actions.append((f"Removed {name}/.codevira/", lambda p=legacy: shutil.rmtree(p, ignore_errors=True)))

    # Migration backup
    migrated = project_path / ".codevira.migrated"
    if migrated.exists():
        print(f"    • {name}/.codevira.migrated/")
        actions.append((f"Removed {name}/.codevira.migrated/", lambda p=migrated: shutil.rmtree(p, ignore_errors=True)))

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
                    actions.append((f"Restored {name} git hook from backup",
                                    lambda h=hook, b=backup: b.rename(h)))
                else:
                    actions.append((f"Removed {name} git hook",
                                    lambda h=hook: h.unlink(missing_ok=True)))
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
                    (f"Removed codevira from {name}/{ide_name}",
                     lambda p=config_path: remove_codevira_from_config(p))
                )


if __name__ == "__main__":
    main()
