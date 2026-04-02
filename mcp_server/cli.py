"""
cli.py — Entry point for the `codevira-mcp` command.

Dispatches subcommands:
  codevira-mcp                  → start MCP server (default)
  codevira-mcp init             → initialize .codevira/ in the current project
  codevira-mcp index            → run incremental index update
  codevira-mcp index --full     → full index rebuild
  codevira-mcp status           → show index health and stats

Global flags:
  --project-dir <path>          → override project directory (for Google Antigravity,
                                   which doesn't support `cwd` in its MCP config)
"""
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
                print("  Aborted. Run `codevira-mcp init` from your project root.")
                sys.exit(0)
            print()

    # Step 2: Create .codevira/ directory structure
    print(f"  Creating .codevira/ in {cwd} ...")
    for subdir in ["graph/changesets", "codeindex", "logs"]:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)
    print(f"  Creating .codevira/ in {cwd} ...              done")

    # Step 3: Auto-add to .gitignore if git repo
    git_dir = cwd / ".git"
    if git_dir.exists():
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

    # Step 5: Run full index build
    print("  Building code index ...               ", end="", flush=True)
    try:
        from indexer.index_codebase import cmd_full_rebuild
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_full_rebuild()
        output = buf.getvalue()
        # Extract chunk count from output
        chunk_count = "?"
        for line in output.splitlines():
            if "chunks indexed" in line or "Total:" in line or "documents" in line.lower():
                chunk_count = line.strip()
                break
            if "Indexed" in line:
                chunk_count = line.strip()
                break
        print(f"done")
    except Exception as e:
        print(f"skipped ({e})")

    # Step 6: Generate graph stubs
    print("  Generating graph stubs ...            ", end="", flush=True)
    try:
        from indexer.index_codebase import cmd_generate_graph
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_generate_graph()
        output = buf.getvalue()
        nodes = "?"
        for line in output.splitlines():
            if "Nodes added:" in line:
                nodes = line.split(":")[-1].strip()
                break
        print(f"done ({nodes} nodes)")
    except Exception as e:
        print(f"skipped ({e})")

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

    # Step 8: Install git hook
    if git_dir.exists():
        print("  Installing git hook ...               ", end="", flush=True)
        try:
            hooks_dir = git_dir / "hooks"
            hooks_dir.mkdir(exist_ok=True)
            hook_path = hooks_dir / "post-commit"

            # Find codevira-mcp executable path
            import shutil as _shutil
            cmd_path = _shutil.which("codevira-mcp") or "codevira-mcp"

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

    # Step 10: Register in global memory
    try:
        from mcp_server.global_sync import import_global_to_project
        from mcp_server.paths import get_global_db_path
        from indexer.global_db import GlobalDB

        gdb = GlobalDB(get_global_db_path())
        gdb.register_project(str(cwd), detected["name"], detected["language"])
        proj_count = gdb.get_project_count()
        gdb.close()
        if proj_count > 1:
            print(f"  Registered in global memory ({proj_count} projects)")
    except Exception:
        pass

    # Print fallback for undetected tools
    import shutil as _shutil
    import sys as _sys
    python_exe = _sys.executable
    project_path = str(cwd)

    print()
    print("  For other AI tools, add this to their MCP config:")
    print()
    print('  {')
    print('    "mcpServers": {')
    print('      "codevira": {')
    print(f'        "command": "{python_exe}",')
    print(f'        "args": ["-m", "mcp_server", "--project-dir", "{project_path}"]')
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


def cmd_status() -> None:
    """Show index health and statistics."""
    from indexer.index_codebase import cmd_status as _cmd_status
    _cmd_status()


def cmd_server(project_dir: Path | None = None) -> None:
    """Start the MCP server."""
    from mcp_server.server import main as server_main
    server_main()


def main() -> None:
    # Pre-parse --project-dir before argparse so we can initialize paths early.
    raw_args = sys.argv[1:]
    project_dir = _set_project_dir_early(raw_args)

    if project_dir is not None:
        from mcp_server.paths import set_project_dir
        set_project_dir(project_dir)

    parser = argparse.ArgumentParser(
        prog="codevira-mcp",
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
    subparsers.add_parser("status", help="Show index health and statistics")

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
        cmd_status()
    else:
        # No subcommand → start MCP server
        cmd_server()


if __name__ == "__main__":
    main()
