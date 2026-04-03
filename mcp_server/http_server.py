"""
http_server.py — HTTP/Streamable transport for Codevira MCP server.

Runs the same 36 MCP tools as stdio mode but over HTTP, enabling:
  - URL-based MCP registration in Claude Code, Cursor, Windsurf
  - HTTPS via mkcert for locally-trusted certificates (required by Claude.ai)
  - Parallel multi-client connections without spawning a process per client

Endpoint layout:
  POST /mcp   — Streamable HTTP (MCP 2025-03-26 spec, preferred)
  GET  /      — Health check → {"status": "ok", "transport": "streamable-http"}

Usage:
  codevira-mcp serve                       # HTTP on 127.0.0.1:7007
  codevira-mcp serve --port 7443 --https   # HTTPS on 127.0.0.1:7443
  codevira-mcp serve --host 0.0.0.0        # Expose on all interfaces (LAN)
"""
from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path
from typing import AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

# Import the Server instance — all tool handlers are registered at module level
# via decorators, so this is safe to import from both stdio and HTTP modes.
from mcp_server.server import server

logger = logging.getLogger(__name__)


def _certs_dir() -> Path:
    from mcp_server.paths import get_global_home
    return get_global_home() / "certs"


def _cert_file() -> Path:
    return _certs_dir() / "localhost.pem"


def _key_file() -> Path:
    return _certs_dir() / "localhost-key.pem"


# ---------------------------------------------------------------------------
# Certificate helpers
# ---------------------------------------------------------------------------

def _certs_exist() -> bool:
    return _cert_file().exists() and _key_file().exists()


def generate_mkcert_certs() -> tuple[Path, Path]:
    """
    Generate trusted localhost certs using mkcert.
    Requires mkcert to be installed and its CA to be in the system trust store
    (`mkcert -install` must have been run at least once).
    Returns (cert_path, key_path).
    Raises RuntimeError if mkcert is not found.
    """
    import shutil

    if not shutil.which("mkcert"):
        raise RuntimeError(
            "mkcert not found.\n"
            "  Install:  brew install mkcert\n"
            "  Trust CA: mkcert -install\n"
            "Then re-run: codevira-mcp serve --https"
        )

    cert_f = _cert_file()
    key_f = _key_file()
    _certs_dir().mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "mkcert",
            "-cert-file", str(cert_f),
            "-key-file", str(key_f),
            "localhost",
            "127.0.0.1",
            "::1",
        ],
        check=True,
    )
    return cert_f, key_f


# ---------------------------------------------------------------------------
# ASGI app factory
# ---------------------------------------------------------------------------

def create_app() -> Starlette:
    """
    Build and return the Starlette ASGI application.

    Routes:
      GET /           → health check (useful for uptime monitoring)
      POST /mcp       → MCP Streamable HTTP transport (MCP 2025-03-26)
      GET  /mcp       → MCP SSE stream (some clients open a GET first)
      DELETE /mcp     → session teardown
    """
    session_manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,        # no resumability — simpler for local single-user use
        json_response=False,   # SSE streaming (not JSON batching)
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    async def health(_req: Request) -> JSONResponse:
        return JSONResponse({"status": "ok", "transport": "streamable-http", "server": "codevira"})

    return Starlette(
        routes=[
            Route("/", endpoint=health, methods=["GET"]),
            Mount("/mcp", app=session_manager.handle_request),
        ],
        lifespan=lifespan,
    )


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

def run_http_server(
    host: str = "127.0.0.1",
    port: int = 7007,
    use_https: bool = False,
    project_dir: Path | None = None,
) -> None:
    """
    Start the HTTP MCP server.  Blocks until Ctrl+C.

    Args:
        host:       Bind address. Use "0.0.0.0" to expose on LAN.
        port:       TCP port to listen on.
        use_https:  If True, load (or auto-generate) mkcert certs and serve TLS.
        project_dir: Optional project root override (sets paths context).
    """
    import uvicorn

    # ---- Startup side effects (mirror server.py main()) ----
    try:
        from mcp_server.crash_logger import install_global_handler
        install_global_handler()
    except Exception as e:
        logger.warning("Could not install crash handler: %s", e)

    # v1.6: Auto-migrate legacy .codevira/ → ~/.codevira/projects/<key>/
    try:
        from mcp_server.migrate import detect_migration_needed, migrate_to_centralized
        from mcp_server.paths import get_project_root
        _proj_root = get_project_root()
        if detect_migration_needed(_proj_root):
            logger.info("Migrating legacy .codevira/ to centralized storage...")
            result = migrate_to_centralized(_proj_root)
            if result.get("migrated"):
                logger.info("Migration complete: %d files moved to %s",
                            result.get("files_copied", 0), result.get("new_path", ""))
    except Exception as e:
        logger.warning("Could not run storage migration: %s", e)

    try:
        from indexer.index_codebase import start_background_watcher
        start_background_watcher(quiet=True)
        logger.info("Live file watcher active")
    except Exception as e:
        logger.warning("Could not start background watcher: %s", e)
        try:
            from mcp_server.crash_logger import log_crash
            log_crash(e, context="http serve: background watcher")
        except Exception:
            pass

    try:
        from indexer.outcome_tracker import analyze_session_outcomes
        from indexer.rule_learner import run_rule_inference
        analyze_session_outcomes()
        run_rule_inference()
    except Exception as e:
        logger.warning("Could not run startup learning: %s", e)

    try:
        from mcp_server.global_sync import import_global_to_project
        import_global_to_project()
    except Exception as e:
        logger.warning("Could not sync global memory: %s", e)

    # ---- TLS certificate setup ----
    ssl_certfile: str | None = None
    ssl_keyfile: str | None = None

    if use_https:
        if not _certs_exist():
            print("  Generating localhost TLS certificate via mkcert ...")
            try:
                generate_mkcert_certs()
                print(f"  Certificate: {_cert_file()}")
                print(f"  Private key: {_key_file()}")
            except RuntimeError as e:
                print(f"\n  ERROR: {e}\n")
                return
        ssl_certfile = str(_cert_file())
        ssl_keyfile = str(_key_file())

    # ---- Print registration instructions ----
    scheme = "https" if use_https else "http"
    url = f"{scheme}://{host}:{port}/mcp"
    display_host = "localhost" if host in ("127.0.0.1", "::1") else host
    display_url = f"{scheme}://{display_host}:{port}/mcp"

    print()
    print("  Codevira MCP — HTTP Server")
    print("  " + "─" * 44)
    print(f"  Endpoint : {display_url}")
    print(f"  Transport: MCP Streamable HTTP (2025-03-26)")
    print()
    print("  ── Register in Claude Code ──────────────────")
    print("  Add to ~/.claude/settings.json (global) or")
    print("  .claude/settings.json (project):")
    print()
    print('  {')
    print('    "mcpServers": {')
    print('      "codevira": {')
    print(f'        "url": "{display_url}"')
    print('      }')
    print('    }')
    print('  }')
    print()
    if not use_https:
        print("  Tip: Use --https for a trusted HTTPS URL (required for Claude.ai)")
        print()
    print("  Press Ctrl+C to stop.")
    print()

    # ---- Run uvicorn ----
    app = create_app()

    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        log_level="warning",
    )
