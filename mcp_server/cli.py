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

    # Step 4: Interactive config
    print()
    project_name = cwd.name
    user_name = input(f"  Project name [{project_name}]: ").strip() or project_name

    language_default = "python"
    language = input(f"  Language [{language_default}]: ").strip() or language_default

    dirs_default = "src"
    dirs_input = input(f"  Source directories (comma-separated) [{dirs_default}]: ").strip() or dirs_default
    watched_dirs = [d.strip() for d in dirs_input.split(",") if d.strip()]

    ext_default = ".py"
    ext_input = input(f"  File extensions (comma-separated) [{ext_default}]: ").strip() or ext_default
    file_extensions = [e.strip() if e.strip().startswith(".") else f".{e.strip()}"
                       for e in ext_input.split(",") if e.strip()]

    # Write config.yaml
    config = {
        "project": {
            "name": user_name,
            "language": language,
            "collection_name": user_name.lower().replace("-", "_").replace(" ", "_"),
            "watched_dirs": watched_dirs,
            "file_extensions": file_extensions,
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

    # Step 9: Print MCP config
    project_path = str(cwd)

    # Detect the full path to codevira-mcp so MCP hosts that don't inherit
    # the user's PATH (Claude Desktop, Cursor, Windsurf on macOS) can find it.
    import shutil as _shutil
    import sys as _sys
    exe_full = _shutil.which("codevira-mcp")
    if exe_full:
        cmd_name = exe_full          # e.g. /Users/sachin/Library/Python/3.12/bin/codevira-mcp
    else:
        cmd_name = "codevira-mcp"    # fallback — may not work in all MCP hosts

    # python -m mcp_server always works as long as `python` resolves correctly.
    python_exe = _sys.executable     # absolute path to the current interpreter

    print()
    print("  " + "─" * 60)
    print(f"  ✓  Codevira initialized in {data_dir}")
    print()
    print("  Add this to your AI tool's MCP config:")
    print()

    base_config = (
        '  {\n'
        '    "mcpServers": {\n'
        '      "codevira": {\n'
        f'        "command": "{cmd_name}",\n'
        f'        "cwd": "{project_path}"\n'
        '      }\n'
        '    }\n'
        '  }'
    )

    antigravity_config = (
        '  {\n'
        '    "mcpServers": {\n'
        '      "codevira": {\n'
        f'        "command": "{cmd_name}",\n'
        f'        "args": ["--project-dir", "{project_path}"]\n'
        '      }\n'
        '    }\n'
        '  }'
    )

    # python -m fallback (most reliable when PATH is not inherited)
    python_fallback_config = (
        '  {\n'
        '    "mcpServers": {\n'
        '      "codevira": {\n'
        f'        "command": "{python_exe}",\n'
        f'        "args": ["-m", "mcp_server", "--project-dir", "{project_path}"]\n'
        '      }\n'
        '    }\n'
        '  }'
    )

    print("  ── Claude Code (.claude/settings.json) " + "─" * 22)
    print(base_config)
    print()
    print("  ── Cursor / Windsurf (settings → MCP) " + "─" * 22)
    print(base_config)
    print()
    print("  ── Google Antigravity (~/.gemini/antigravity/mcp_config.json)")
    print(antigravity_config)
    print()
    if not exe_full:
        print("  ⚠  codevira-mcp not found in PATH — use the python -m fallback below.")
        print()
    print("  ── Fallback (if codevira-mcp is not in your MCP host's PATH) " + "─" * 10)
    print(python_fallback_config)
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
    import asyncio
    asyncio.run(server_main())


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
    subparsers.add_parser("init", help="Initialize Codevira in the current project")

    # index
    index_parser = subparsers.add_parser("index", help="Run the code indexer")
    index_parser.add_argument("--full", action="store_true", help="Full rebuild from scratch")
    index_parser.add_argument("--quiet", action="store_true", help="Suppress output (used by git hook)")

    # status
    subparsers.add_parser("status", help="Show index health and statistics")

    args = parser.parse_args(raw_args)

    if args.command == "init":
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
