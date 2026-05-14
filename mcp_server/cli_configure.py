"""
`codevira configure` — pick which folders/extensions Codevira indexes.

New in v1.8.0. Scans the project (gitignore-aware via the existing
:func:`mcp_server.gitignore.discover_source_files`), shows discovered dirs +
extensions with file counts, lets the user pick via a numbered-list prompt,
writes the choices back to ``.codevira/config.yaml``, and offers to re-index.

Designed to fix the AgentStore-style failure mode where ``auto_detect_project``
mis-guesses a project's layout and ``codevira index --full`` silently reports
``0 chunks indexed``. Pair with the zero-chunks safety hint in
``indexer/index_codebase.py`` which points users at this command.

CLI entry point: :func:`cmd_configure` (wired in ``mcp_server/cli.py``).
"""
from __future__ import annotations

import logging
import sys
from collections import defaultdict
from pathlib import Path

import yaml

from mcp_server.paths import get_data_dir, get_project_root

logger = logging.getLogger(__name__)


class NonInteractiveError(RuntimeError):
    """Raised when an interactive prompt is required but stdin is not a TTY.

    Caller (:func:`cmd_configure`) maps this to a clean CLI error instructing
    the user to pass ``--dirs``/``--extensions`` flags instead.
    """


# ---------------------------------------------------------------------------
# Input normalization
# ---------------------------------------------------------------------------

def _normalize_extensions(raw: str | list[str]) -> list[str]:
    """Accept ``"py,ts"``/``".py,.ts"``/``["py", ".ts"]``.

    Returns a deterministically sorted list with each entry lowercased and
    dot-prefixed: ``[".py", ".ts"]``. Empty inputs return ``[]``.
    """
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
    else:
        parts = [str(p).strip() for p in (raw or []) if str(p).strip()]
    out: set[str] = set()
    for p in parts:
        p = p.lower()
        if not p.startswith("."):
            p = "." + p
        out.add(p)
    return sorted(out)


def _split_csv(raw: str | None) -> list[str]:
    """Split comma-separated CLI arg into stripped, de-duped, order-preserving list."""
    if not raw:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for part in raw.split(","):
        s = part.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------

def scan_project(project_root: Path, current_config: dict) -> dict:
    """Discover source dirs + extensions for an interactive prompt.

    Reuses :func:`mcp_server.gitignore.discover_source_files` (gitignore-aware
    with the 25-entry safety-net skip list). Groups found files by top-level
    directory and by extension.

    Returns::

        {
          "dirs_discovered": [
              {"path": "apps", "files": 6431, "exts": {".ts", ".tsx"}, "on_disk": True},
              ...
          ],
          "exts_discovered": [
              {"ext": ".py", "files": 1291},
              ...
          ],
          "dirs_now":     ["src"],            # from current_config
          "exts_now":     [".py", ".ts"],     # from current_config
          "dirs_missing": ["src"],            # in config but not on disk
        }
    """
    from mcp_server.gitignore import discover_source_files

    # Honor user's explicit skip_dirs from config.yaml (in addition to the
    # 25-entry safety net baked into discover_source_files). We do NOT pass
    # watched_dirs/file_extensions overrides — the scan must show EVERYTHING
    # the user could potentially watch so they can pick. skip_dirs is
    # different: when the user has said "never look at vendor/", we respect
    # that so they don't see it every time they run configure.
    overrides: dict = {}
    skip_dirs_from_config = current_config.get("skip_dirs") or []
    if skip_dirs_from_config:
        overrides["skip_dirs"] = list(skip_dirs_from_config)

    all_files = discover_source_files(project_root, config_overrides=overrides or None)

    # Group by top-level dir (relative to project_root). Files at the root
    # bucket into "." so they can be explicitly watched if desired.
    dir_files: dict[str, int] = defaultdict(int)
    dir_exts: dict[str, set[str]] = defaultdict(set)
    ext_files: dict[str, int] = defaultdict(int)

    for fpath in all_files:
        try:
            rel = fpath.relative_to(project_root)
        except ValueError:
            continue
        parts = rel.parts
        top = parts[0] if len(parts) > 1 else "."
        ext = fpath.suffix.lower()
        dir_files[top] += 1
        dir_exts[top].add(ext)
        ext_files[ext] += 1

    # Current config values (normalize defensively — config may be partially written)
    dirs_now = list(current_config.get("watched_dirs") or [])
    exts_now = list(current_config.get("file_extensions") or [])

    # Which currently-configured dirs don't exist on disk?
    dirs_missing = [d for d in dirs_now if not (project_root / d).exists()]

    dirs_discovered = [
        {
            "path": d,
            "files": dir_files[d],
            "exts": set(dir_exts[d]),
            "on_disk": True,
        }
        for d in dir_files
    ]
    # Inject missing-on-disk dirs so the UI shows them (with on_disk=False).
    for d in dirs_missing:
        dirs_discovered.append({"path": d, "files": 0, "exts": set(), "on_disk": False})

    # Deterministic display order: file-count desc, then path asc.
    dirs_discovered.sort(key=lambda r: (-r["files"], r["path"]))

    exts_discovered = [
        {"ext": e, "files": ext_files[e]} for e in ext_files
    ]
    exts_discovered.sort(key=lambda r: (-r["files"], r["ext"]))

    return {
        "dirs_discovered": dirs_discovered,
        "exts_discovered": exts_discovered,
        "dirs_now": dirs_now,
        "exts_now": exts_now,
        "dirs_missing": dirs_missing,
    }


# ---------------------------------------------------------------------------
# Multi-select prompt
# ---------------------------------------------------------------------------

def prompt_multi_select(
    title: str,
    items: list[dict],
    preselected: set[str],
    key_field: str,
    label_fn,
) -> set[str] | None:
    """Numbered-list prompt with comma-separated input.

    Returns a set of selected keys, or ``None`` if the user typed ``q``
    (clean abort). See module docstring / plan for the full input grammar.
    """
    if not sys.stdin.isatty():
        raise NonInteractiveError(
            "This prompt requires an interactive terminal. "
            "For non-interactive use, pass --dirs and/or --extensions flags."
        )

    if not items:
        print(f"\n{title}: (nothing discovered)")
        return set()

    # `items` is already sorted by the caller (scan_project sorts once).
    print(f"\n{title}")
    for idx, item in enumerate(items, start=1):
        key = item[key_field]
        marker = "[x]" if key in preselected else "[ ]"
        print(f"  {idx:>2}  {marker}  {label_fn(item)}")

    prompt_text = (
        "\nEnter numbers (e.g. '1,3,5'), 'all', 'none', "
        "<Enter> to keep current, 'q' to abort:\n> "
    )

    while True:
        try:
            raw = input(prompt_text)
        except EOFError:
            # stdin closed mid-session — treat like 'q'
            return None
        except KeyboardInterrupt:
            # Ctrl+C — treat as clean abort (not a crash). Caller converts None
            # to exit 0 with "Aborted." message.
            print()  # terminal left mid-line; newline for clean prompt return
            return None
        s = raw.strip().lower()

        if s == "q":
            return None
        if s == "":
            return set(preselected)
        if s == "all":
            # "all" excludes missing-on-disk dirs by design
            return {
                item[key_field]
                for item in items
                if item.get("on_disk", True)
            }
        if s == "none":
            return set()

        # Parse comma-separated indices
        try:
            indices = [int(p.strip()) for p in s.split(",") if p.strip()]
        except ValueError:
            print("  Could not parse input. Example: '1,3,5' | all | none | q | <Enter>")
            continue

        bad = [i for i in indices if i < 1 or i > len(items)]
        if bad:
            print(f"  Out of range: {bad}. Valid: 1..{len(items)}")
            continue

        return {items[i - 1][key_field] for i in indices}


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via tempfile + rename.

    ``Path.write_text`` opens-truncates-writes, which is a 3-step sequence
    that a concurrent reader can observe mid-way (seeing an empty file).
    On POSIX, ``os.replace`` is atomic — readers either see the old content
    or the new content, never a partial/empty state. Critical when a
    running MCP server periodically reloads config.yaml via ``_load_config``
    while the user runs ``codevira configure`` in a separate terminal.

    Uses ``tempfile.mkstemp`` for a unique per-call tmp name so concurrent
    writers on the same ``path`` don't collide. (A fixed ``.tmp`` suffix
    would race: writer A renames its tmp onto ``path``, writer B then tries
    to rename a tmp that no longer exists and gets ``FileNotFoundError``.)

    On write failure the tmp is cleaned up so stale ``.tmp`` files don't
    accumulate in the data directory.
    """
    import os
    import tempfile
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(parent), prefix=path.name + ".", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_config_patch(data_dir: Path, dirs: set[str] | list[str], exts: set[str] | list[str]) -> None:
    """Rewrite ``.codevira/config.yaml`` with new watched_dirs + file_extensions.

    Deterministic output:
      * ``watched_dirs`` → ``sorted(unique)``
      * ``file_extensions`` → ``_normalize_extensions(...)`` (lowercase, dot-prefixed, sorted)

    Atomic: tempfile + ``os.replace`` so a concurrent MCP server reading
    config.yaml never observes an empty/partial file.

    Preserves every other key in the file (``logs``, ``skip_dirs``, etc.).
    Does NOT touch ``metadata.json`` — that file stores identity/version
    info only (verified against ``auto_init.py:_write_metadata``).
    """
    cfg_path = data_dir / "config.yaml"
    # is_file() instead of exists(): treat "config.yaml is a directory / broken
    # symlink / device file" as "no config, start fresh" rather than crash on read.
    if cfg_path.is_file():
        try:
            raw = yaml.safe_load(cfg_path.read_text()) or {}
        except (UnicodeDecodeError, yaml.YAMLError):
            # Corrupted bytes or broken YAML — start fresh. Caller (cmd_configure)
            # already printed an error before reaching write_config_patch if the
            # caller wanted to abort; by this point we're being asked to WRITE,
            # so treat existing corrupted content as "gone" and let user's new
            # input replace it.
            raw = {}
    else:
        raw = {}
    # Defensive: if the file is malformed (top-level is a list, scalar, or
    # the `project` key holds a non-dict value), normalize before writing.
    # `setdefault` would otherwise return the existing wrong-type value and
    # crash on subsequent `proj["..."] = ...` assignments.
    if not isinstance(raw, dict):
        raw = {}
    if not isinstance(raw.get("project"), dict):
        raw["project"] = {}
    proj = raw["project"]
    proj["watched_dirs"] = sorted(set(dirs))
    proj["file_extensions"] = _normalize_extensions(list(exts))
    _atomic_write_text(cfg_path, yaml.safe_dump(raw, sort_keys=False))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _load_current_config(data_dir: Path) -> dict:
    """Load the ``project`` sub-dict from config.yaml. Raises on malformed YAML.

    Always returns a dict — guards against ``project: null`` or a top-level
    non-dict YAML document that would otherwise propagate None downstream
    and break ``scan_project``'s ``.get()`` calls.

    Uses ``is_file()`` (not ``exists()``) to treat "config.yaml is a
    directory / broken symlink / device file" as "not a config" and let
    the bootstrap path heal. Catches ``UnicodeDecodeError`` for binary
    garbage files (filesystem corruption, bad encoding) and re-raises
    as ``yaml.YAMLError`` so the caller's single existing malformed-YAML
    branch handles both cases.
    """
    cfg_path = data_dir / "config.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        text = cfg_path.read_text()
    except UnicodeDecodeError as e:
        raise yaml.YAMLError(f"config.yaml is not valid UTF-8: {e}") from e
    raw = yaml.safe_load(text) or {}
    if not isinstance(raw, dict):
        return {}
    sub = raw.get("project", raw)
    return sub if isinstance(sub, dict) else {}


def _dir_label(item: dict) -> str:
    path = item["path"]
    if not item.get("on_disk", True):
        return f"{path:<15} missing  (in your config but not on disk)"
    files = item["files"]
    exts = " ".join(sorted(item["exts"]))
    return f"{path:<15} {files:>7,} files   {exts}"


def _ext_label(item: dict) -> str:
    return f"{item['ext']:<6} {item['files']:>7,} files"


def cmd_configure(
    interactive: bool,
    dirs_arg: str | None,
    exts_arg: str | None,
    reindex: bool,
    dry_run: bool,
) -> int:
    """Entry point for ``codevira configure``.

    Returns a CLI exit code (see module docstring / plan):
      * 0 — success, or clean user-abort (``q``)
      * 1 — system error (missing .codevira/, corrupt config.yaml)
      * 2 — user error (empty project, empty selection in non-interactive)
    """
    try:
        data_dir = get_data_dir()
    except Exception as e:
        print(f"Error: could not resolve .codevira/: {e}", file=sys.stderr)
        print(
            "  → run `codevira doctor` to diagnose, "
            "or `codevira setup` to re-init.",
            file=sys.stderr,
        )
        return 1

    # Project root is NOT necessarily data_dir.parent — in v1.6+ centralized
    # mode, data_dir lives under ~/.codevira/projects/<slug>/, completely
    # decoupled from the actual project tree. Use paths.get_project_root()
    # which handles --project-dir override, cwd discovery, and project markers.
    project_root = get_project_root()

    # v1.8.1: refuse $HOME and system top-levels as a project root. Treating
    # these as a project causes the watcher to walk huge unrelated trees
    # (~/Library/..., /var/log) — see crash-log analysis 2026-04-24 for the
    # production failure that motivated this guard.
    from mcp_server.paths import is_invalid_project_root
    rejection = is_invalid_project_root(project_root)
    if rejection:
        print(f"Error: {rejection}", file=sys.stderr)
        print(
            "  → cd into a project directory or pass "
            "--project-dir <real-project-path>.",
            file=sys.stderr,
        )
        return 1

    # Note: we deliberately do NOT require data_dir to exist. `codevira
    # register` only writes MCP client configs — it doesn't create
    # ~/.codevira/projects/<slug>/. The data_dir is normally created by
    # `codevira init`, `codevira index --full`, or lazily by auto_init on
    # the first MCP tool call. A user who runs `register` → `configure`
    # without any of those in between is a valid flow: bootstrap everything
    # from scratch (like cmd_init does).

    config_path = data_dir / "config.yaml"
    # Detect the pathological case where `config.yaml` exists but isn't a
    # regular file (dir, broken symlink, device node). Without this check,
    # the later write would crash with IsADirectoryError or similar. Bail
    # out early with a clear message — we can't safely auto-heal this
    # without destroying whatever is there.
    if config_path.exists() and not config_path.is_file() and not config_path.is_symlink():
        print(
            f"Error: {config_path} exists but is not a regular file "
            f"({'directory' if config_path.is_dir() else 'special file'}). "
            f"Remove it by hand and re-run.",
            file=sys.stderr,
        )
        return 1
    if not config_path.is_file():
        # is_file() = False means: file missing OR broken symlink. Both
        # warrant a fresh bootstrap.
        # Ensure data_dir + subdirs exist (matches cmd_init's step 2b).
        # BUT respect --dry-run: we want a truly read-only dry-run so users
        # can preview what would happen without creating anything on disk.
        if not dry_run:
            try:
                for subdir in ["graph/changesets", "codeindex", "logs"]:
                    (data_dir / subdir).mkdir(parents=True, exist_ok=True)
            except (PermissionError, OSError) as e:
                print(
                    f"Error: cannot create {data_dir}: {e}",
                    file=sys.stderr,
                )
                print(
                    f"  → check permissions on {data_dir.parent} "
                    f"(`ls -la {data_dir.parent}`) and ensure your user "
                    f"can write there.",
                    file=sys.stderr,
                )
                return 1
        # `codevira register` creates the data_dir infrastructure but does NOT
        # write config.yaml — that normally happens lazily on the first MCP
        # tool call (via auto_init) or explicitly on `codevira init`. A user
        # running `codevira configure` right after `register` would hit a
        # broken "no config" error. Bootstrap the file ourselves using the
        # same detection pipeline auto_init uses, then fall through to the
        # normal interactive flow. In --dry-run mode we hold the bootstrap
        # in memory only so we don't modify disk.
        from mcp_server.detect import auto_detect_project
        try:
            detected = auto_detect_project(project_root)
        except Exception as e:
            # auto_detect_project should never raise (it's defensive), but if
            # it does (broken project structure, unsupported edge case, etc.)
            # don't crash configure. Fall back to a minimal "unknown" stub
            # so the user can still pick dirs + extensions via the prompt
            # or flags. Users running with --dirs/--extensions get exactly
            # what they asked for; interactive users see the discovery list
            # which doesn't depend on detect.
            logger.warning(
                "auto_detect_project failed: %s. Falling back to empty defaults.", e,
            )
            detected = {
                "name": project_root.name,
                "language": "unknown",
                "watched_dirs": [],
                "file_extensions": [],
                "collection_name": project_root.name.lower().replace("-", "_").replace(" ", "_"),
            }
        bootstrap_cfg = {
            "name": detected["name"],
            "language": detected["language"],
            "watched_dirs": detected["watched_dirs"],
            "file_extensions": detected["file_extensions"],
            "collection_name": detected.get("collection_name"),
        }
        if dry_run:
            print(
                f"(config.yaml missing — would bootstrap for language="
                f"{detected['language']}; --dry-run so nothing written)"
            )
            current = {
                "watched_dirs": bootstrap_cfg["watched_dirs"],
                "file_extensions": bootstrap_cfg["file_extensions"],
            }
        else:
            try:
                # Atomic write — see _atomic_write_text docstring.
                _atomic_write_text(
                    config_path,
                    yaml.safe_dump({"project": bootstrap_cfg}, sort_keys=False),
                )
            except (PermissionError, OSError) as e:
                print(
                    f"Error: cannot write {config_path}: {e}",
                    file=sys.stderr,
                )
                return 1
            print(
                f"Bootstrapped default config.yaml for language={detected['language']} "
                f"(was missing; created now so you can configure it)."
            )
            try:
                current = _load_current_config(data_dir)
            except yaml.YAMLError as e:
                print(
                    f"Error: just-written config.yaml re-read as malformed: {e}",
                    file=sys.stderr,
                )
                return 1
    else:
        try:
            current = _load_current_config(data_dir)
        except yaml.YAMLError as e:
            print(
                f"Error: config.yaml is malformed: {e}. "
                "Fix by hand or re-run `codevira register`.",
                file=sys.stderr,
            )
            return 1

    # Heal parity with auto_init regardless of whether we bootstrapped or
    # loaded existing config. Users coming from v1.5/v1.6 migrations might
    # have config.yaml but no metadata.json (rename-resilient lookup
    # broken) or not yet be in global.db (cross-project intelligence
    # degraded). Write metadata if missing, register in global.db (idempotent
    # INSERT OR REPLACE). Dry-run skips the disk write; both calls
    # defensively wrapped so failures don't break configure.
    if not dry_run:
        from mcp_server.auto_init import _write_metadata, _register_global
        try:
            if not (data_dir / "metadata.json").exists():
                _write_metadata(data_dir, project_root)
        except Exception as e:
            logger.warning("configure: metadata.json heal failed: %s", e)
        try:
            # Use current config's language if available; fall back to "unknown"
            reg_detected = {
                "name": current.get("name") or project_root.name,
                "language": current.get("language") or "unknown",
            }
            _register_global(data_dir, project_root, reg_detected)
        except Exception as e:
            logger.warning("configure: global.db heal failed: %s", e)

    # Legacy detection: if data_dir points to an in-project `.codevira/`
    # (pre-v1.6 layout) rather than the centralized `~/.codevira/projects/
    # <slug>/`, let the user know migration is available. We don't auto-
    # migrate — that's `codevira init`'s job — but users who only ran
    # `codevira configure` might not know cmd_init exists.
    try:
        data_resolved = data_dir.resolve()
        project_resolved = project_root.resolve()
        if (project_resolved in data_resolved.parents
                and data_resolved.name == ".codevira"):
            print(
                "\nℹ  You're using the legacy in-project `.codevira/` layout.",
                file=sys.stderr,
            )
            print(
                "   Run `codevira init` to migrate to centralized "
                "~/.codevira/projects/<slug>/ storage.",
                file=sys.stderr,
            )
    except (OSError, ValueError):
        pass  # Don't let the hint block the main flow

    scan = scan_project(project_root, current)

    print(f"\nScanning {project_root.name} (respecting .gitignore)...\n")
    print("Current config:")
    print(f"  watched_dirs:    {scan['dirs_now']!r}")
    if scan["dirs_missing"]:
        print(f"    [!] missing on disk: {scan['dirs_missing']!r}")
    print(f"  file_extensions: {scan['exts_now']!r}")

    # "No source files" is truly when NO on-disk files were found. dirs_discovered
    # can contain injected missing-on-disk entries, so use exts_discovered as the
    # real empty-project signal (every discovered file contributes an extension).
    if not scan["exts_discovered"]:
        print(
            "\nError: no source files discovered in this project. "
            "Is this the right directory?",
            file=sys.stderr,
        )
        return 2

    # Always print a compact scan summary so dry-run and non-interactive users
    # see what was discovered. Interactive mode ALSO prints this; its prompt
    # will later re-list dirs with numbers, but the summary here is
    # non-redundant (different layout, top-10 cap) and helpful orientation.
    print("\nDiscovered source directories (top 10 by file count):")
    for item in scan["dirs_discovered"][:10]:
        if not item.get("on_disk", True):
            print(f"  - {item['path']:<20} (missing on disk)")
        else:
            exts = " ".join(sorted(item["exts"]))
            print(f"  - {item['path']:<20} {item['files']:>7,} files   {exts}")
    if len(scan["dirs_discovered"]) > 10:
        print(f"  ... (+{len(scan['dirs_discovered']) - 10} more)")

    print("\nDiscovered file extensions (top 10 by file count):")
    for item in scan["exts_discovered"][:10]:
        # P1-3 (rc.5): files without an extension previously rendered as a
        # blank cell ("- " followed by the count). Show "(none)" so the user
        # can tell what's being counted (Dockerfile, Makefile, LICENSE, etc.).
        ext_display = item["ext"] if item["ext"] else "(none)"
        print(f"  - {ext_display:<6} {item['files']:>7,} files")
    if len(scan["exts_discovered"]) > 10:
        print(f"  ... (+{len(scan['exts_discovered']) - 10} more)")

    # ---- Determine dirs + exts ---------------------------------------------
    if interactive:
        preselected_dirs = set(scan["dirs_now"])
        try:
            chosen_dirs = prompt_multi_select(
                "Discovered source directories:",
                scan["dirs_discovered"],
                preselected_dirs,
                key_field="path",
                label_fn=_dir_label,
            )
        except NonInteractiveError as e:
            print(f"Error: {e}", file=sys.stderr)
            print(
                "  → run `codevira configure --dirs DIR1,DIR2 "
                "--extensions .py,.ts` for non-interactive mode.",
                file=sys.stderr,
            )
            return 1

        if chosen_dirs is None:
            print("Aborted.")
            return 0
        if not chosen_dirs:
            # Re-prompt once with a clear message — writing empty would recreate the zero-chunks bug.
            print("  Select at least one directory (or 'q' to abort).")
            try:
                chosen_dirs = prompt_multi_select(
                    "Discovered source directories:",
                    scan["dirs_discovered"],
                    preselected_dirs,
                    key_field="path",
                    label_fn=_dir_label,
                )
            except NonInteractiveError:
                return 1
            if chosen_dirs is None:
                print("Aborted.")
                return 0
            if not chosen_dirs:
                print("Error: no directories selected. Aborting.", file=sys.stderr)
                print(
                    "  → run `codevira configure --dirs <list>` to specify "
                    "directories non-interactively.",
                    file=sys.stderr,
                )
                return 2

        preselected_exts = set(scan["exts_now"])
        try:
            chosen_exts = prompt_multi_select(
                "Discovered file extensions:",
                scan["exts_discovered"],
                preselected_exts,
                key_field="ext",
                label_fn=_ext_label,
            )
        except NonInteractiveError as e:
            print(f"Error: {e}", file=sys.stderr)
            print(
                "  → run `codevira configure --extensions .py,.ts,.js` "
                "for non-interactive mode.",
                file=sys.stderr,
            )
            return 1
        if chosen_exts is None:
            print("Aborted.")
            return 0
        # Empty exts breaks indexing the same way empty dirs do — writing
        # file_extensions: [] matches zero files downstream. Re-prompt once,
        # exit 2 if user still picks empty.
        if not chosen_exts:
            print("  Select at least one extension (or 'q' to abort).")
            try:
                chosen_exts = prompt_multi_select(
                    "Discovered file extensions:",
                    scan["exts_discovered"],
                    preselected_exts,
                    key_field="ext",
                    label_fn=_ext_label,
                )
            except NonInteractiveError:
                return 1
            if chosen_exts is None:
                print("Aborted.")
                return 0
            if not chosen_exts:
                print("Error: no extensions selected. Aborting.", file=sys.stderr)
                print(
                    "  → run `codevira configure --extensions .py,.ts` to "
                    "specify extensions non-interactively.",
                    file=sys.stderr,
                )
                return 2
    else:
        # Non-interactive: flags drive the selection, unspecified => keep current.
        if dirs_arg is not None:
            requested = _split_csv(dirs_arg)
            if not requested:
                print(
                    "Error: --dirs was empty. Provide at least one directory "
                    "(or omit the flag to keep current).",
                    file=sys.stderr,
                )
                return 2
            # Warn about non-existent dirs but still write them (forward-compat).
            for d in requested:
                if not (project_root / d).exists():
                    print(f"  warning: '{d}' does not exist on disk (writing anyway)")
            chosen_dirs = set(requested)
        else:
            chosen_dirs = set(scan["dirs_now"])

        if exts_arg is not None:
            chosen_exts = set(_normalize_extensions(exts_arg))
            if not chosen_exts:
                print(
                    "Error: --extensions was empty. Provide at least one extension "
                    "(or omit the flag to keep current).",
                    file=sys.stderr,
                )
                return 2
        else:
            chosen_exts = set(scan["exts_now"])

        if not chosen_dirs:
            print(
                "Error: no directories would be watched. "
                "Pass --dirs to set at least one.",
                file=sys.stderr,
            )
            return 2
        if not chosen_exts:
            print(
                "Error: no file extensions would be watched. "
                "Pass --extensions to set at least one "
                "(writing an empty list would match zero files).",
                file=sys.stderr,
            )
            return 2

    # ---- Compute removals (for user-visible confirmation) ------------------
    removed = sorted(set(scan["dirs_now"]) - chosen_dirs)
    if removed:
        tagged = [
            f"{d} (missing)" if d in scan["dirs_missing"] else d
            for d in removed
        ]
        print(f"\nRemoving from watched_dirs: {tagged}")

    dirs_final = sorted(chosen_dirs)
    exts_final = _normalize_extensions(list(chosen_exts))
    print("\nConfig to write:")
    print(f"  watched_dirs:    {dirs_final}")
    print(f"  file_extensions: {exts_final}")

    if dry_run:
        print("\n(--dry-run; no file written)")
        return 0

    try:
        write_config_patch(data_dir, chosen_dirs, chosen_exts)
    except (PermissionError, OSError) as e:
        print(
            f"Error: cannot write {data_dir / 'config.yaml'}: {e}",
            file=sys.stderr,
        )
        return 1
    print("\n✓ Wrote .codevira/config.yaml")

    # ---- Reindex ------------------------------------------------------------
    should_reindex = False
    if reindex:
        if not sys.stdin.isatty():
            print("(non-interactive: skipping reindex. Run `codevira index --full` yourself.)")
        else:
            # Bug 22 (rc.4): use shared confirm() helper. Previous code returned
            # False on any non-matching input (including paste artifacts) — looked
            # like the prompt rejected legitimate input. confirm() loops on bad
            # input and handles EOFError / KeyboardInterrupt cleanly (both return
            # False, so we fall through to the no-reindex branch — config is
            # already written; user can run `codevira index --full` later).
            from mcp_server._prompts import confirm
            print()  # preserve the blank line the old prompt prepended
            should_reindex = confirm("Rebuild index now?", default=True)

    if should_reindex:
        from indexer.index_codebase import cmd_full_rebuild
        try:
            cmd_full_rebuild()
        except KeyboardInterrupt:
            # User hit Ctrl+C mid-rebuild (e.g. embedding was too slow).
            # Config was already written — the rebuild is recoverable by
            # running `codevira index --full` later.
            print()  # newline after ^C
            print("Rebuild interrupted. Config is saved.")
            print("Run `codevira index --full` later to build the semantic index.")
            return 0
        except Exception as e:
            # cmd_full_rebuild failed (chromadb disk full, model download
            # failed, permission denied, etc.). Don't mask the user's saved
            # config behind a traceback — print a friendly error with the
            # specific reason and let them retry manually.
            print()
            print(f"Rebuild failed: {e}", file=sys.stderr)
            print("Your config IS saved. Run `codevira index --full` to retry the rebuild.",
                  file=sys.stderr)
            return 1
        print("\nNote: restart AI tools and any running watcher to pick up the new config.")
    else:
        print("\nTip: run `codevira index --full` to rebuild the semantic index.")
        print("Note: restart AI tools and any running watcher to pick up the new config.")

    return 0
