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
  codevira serve                       # HTTP on 127.0.0.1:7007
  codevira serve --port 7443 --https   # HTTPS on 127.0.0.1:7443
  codevira serve --host 0.0.0.0        # Expose on all interfaces (LAN)
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import subprocess
from pathlib import Path
from typing import AsyncIterator

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse
from starlette.routing import Mount, Route

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

# Import the Server instance — all tool handlers are registered at module level
# via decorators, so this is safe to import from both stdio and HTTP modes.
from mcp_server.server import server

logger = logging.getLogger(__name__)

# Bearer token file — auto-generated on first non-loopback serve
_TOKEN_FILE_NAME = "http_bearer_token"

# Landing page shown when a browser hits GET / — avoids the confusing
# JSON-RPC "Not Acceptable" error users see when they open the server URL.
_LANDING_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Codevira MCP Server</title>
<style>
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 680px; margin: 60px auto; padding: 0 20px; color: #24292f; }
h1 { color: #0969da; }
code { background: #f6f8fa; padding: 2px 6px; border-radius: 4px; font-size: 0.9em; }
pre { background: #f6f8fa; padding: 16px; border-radius: 8px; overflow-x: auto; }
.ok { color: #1a7f37; font-weight: 600; }
a { color: #0969da; }
.note { background: #fff8c5; border-left: 4px solid #d4a72c; padding: 12px 16px;
        border-radius: 4px; margin: 20px 0; }
</style>
</head>
<body>
<h1>Codevira MCP Server</h1>
<p class="ok">✓ Server is running (streamable-http transport)</p>

<h2>This is not a web app</h2>
<p>Codevira is a <strong>Model Context Protocol</strong> server. It's designed to
be consumed by AI coding tools (Claude Code, Cursor, Windsurf), not visited in a browser.</p>

<div class="note">
<strong>Note:</strong> The <code>/mcp</code> endpoint requires <code>Accept: text/event-stream</code>
headers. Browsers can't speak MCP directly — you'll see a JSON-RPC error if you visit it.
</div>

<h2>Using this server</h2>
<p>Add this to your AI tool's MCP config:</p>
<pre>{
  "mcpServers": {
    "codevira": {
      "url": "http://localhost:7007/mcp"
    }
  }
}</pre>

<h2>Endpoints</h2>
<ul>
  <li><code>GET /</code> — this page (or JSON with <code>Accept: application/json</code>)</li>
  <li><code>POST /mcp</code> — MCP Streamable HTTP transport (JSON-RPC)</li>
</ul>

<p>See <a href="https://github.com/sachinshelke/codevira">github.com/sachinshelke/codevira</a>
for documentation.</p>
</body>
</html>
"""


def _certs_dir() -> Path:
    from mcp_server.paths import get_global_home

    return get_global_home() / "certs"


def _cert_file() -> Path:
    return _certs_dir() / "localhost.pem"


def _key_file() -> Path:
    return _certs_dir() / "localhost-key.pem"


# ---------------------------------------------------------------------------
# Bearer token auth (required when binding to non-loopback addresses)
# ---------------------------------------------------------------------------


def _get_or_create_token() -> str:
    """Return the bearer token, creating one if it doesn't exist.

    Token is stored in ~/.codevira/http_bearer_token so it persists across
    restarts but is NOT committed to any project repo.
    """
    from mcp_server.paths import get_global_home

    token_path = get_global_home() / _TOKEN_FILE_NAME
    if token_path.exists():
        token = token_path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(32)
    # v3.0.0 round-3: atomic write so a crash mid-write doesn't leave
    # the server with an empty or partial token file (which would fail
    # auth on every subsequent request). The mode kwarg ensures the
    # final file is 0o600 — secret-handling site.
    from mcp_server.storage.atomic import atomic_write_text

    atomic_write_text(token_path, token + "\n", mode=0o600)
    return token


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Reject requests without a valid Bearer token.

    Applied only when the server binds to a non-loopback address (0.0.0.0, LAN IP, etc.)
    to prevent unauthenticated access from other machines on the network.
    The health endpoint (GET /) is exempt so uptime monitors still work.
    """

    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        # Allow health check without auth
        if request.url.path == "/" and request.method == "GET":
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {self._token}":
            return JSONResponse(
                {
                    "error": "Unauthorized",
                    "hint": "Set Authorization: Bearer <token> header",
                },
                status_code=401,
            )
        return await call_next(request)


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
            "Then re-run: codevira serve --https"
        )

    cert_f = _cert_file()
    key_f = _key_file()
    _certs_dir().mkdir(parents=True, exist_ok=True)

    subprocess.run(
        [
            "mkcert",
            "-cert-file",
            str(cert_f),
            "-key-file",
            str(key_f),
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


def create_app(bearer_token: str | None = None) -> Starlette:
    """
    Build and return the Starlette ASGI application.

    Args:
        bearer_token: If set, all requests (except GET /) must include
                      an ``Authorization: Bearer <token>`` header.
                      Used when binding to non-loopback addresses.

    Routes:
      GET /           → health check (useful for uptime monitoring)
      POST /mcp       → MCP Streamable HTTP transport (MCP 2025-03-26)
      GET  /mcp       → MCP SSE stream (some clients open a GET first)
      DELETE /mcp     → session teardown
    """
    # H1: this server object is shared across all HTTP requests/sessions.
    # The roots-binding hook pins a single process-global project, which
    # would cross-contaminate sessions, so disable it under HTTP transport.
    import mcp_server.server as _server_mod

    _server_mod._is_http_transport = True

    session_manager = StreamableHTTPSessionManager(
        app=server,
        stateless=True,  # no resumability — simpler for local single-user use
        json_response=False,  # SSE streaming (not JSON batching)
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with session_manager.run():
            yield

    async def health(req: Request):
        # Return HTML for browsers, JSON for API clients
        accept = req.headers.get("accept", "")
        if "text/html" in accept:
            return HTMLResponse(_LANDING_HTML)
        return JSONResponse(
            {
                "status": "ok",
                "transport": "streamable-http",
                "server": "codevira",
                "mcp_endpoint": "/mcp",
            }
        )

    middleware = []
    if bearer_token:
        middleware.append(Middleware(_BearerAuthMiddleware, token=bearer_token))

    return Starlette(
        routes=[
            Route("/", endpoint=health, methods=["GET"]),
            Mount("/mcp", app=session_manager.handle_request),
        ],
        lifespan=lifespan,
        middleware=middleware,
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
    import sys
    import uvicorn

    # If a project directory was passed explicitly, set it so all path
    # resolution in this process uses it.  The CLI also calls set_project_dir()
    # globally, but a direct caller of run_http_server() may not have done so.
    if project_dir is not None:
        from mcp_server.paths import set_project_dir

        set_project_dir(project_dir)

    # v1.8.1: refuse to start the HTTP server with $HOME / system root.
    # Symmetric with mcp_server.server.main()'s stdio guard. cmd_serve
    # already checks this at the CLI layer, but run_http_server is also
    # callable directly (e.g. by tests, third-party launchers, or future
    # CLI entry points). Defense-in-depth: a $HOME-bound HTTP server
    # would fire start_background_watcher below and walk
    # ~/Library/Group Containers/... — the original v1.8.0 crash mode.
    try:
        from mcp_server.paths import get_project_root, is_invalid_project_root

        _early_root = get_project_root()
        _rejection = is_invalid_project_root(_early_root)
        if _rejection:
            print(f"Error: {_rejection}", file=sys.stderr)
            print(
                "  The HTTP MCP server cannot start with this project root.\n"
                "  Pass --project-dir <real-project-path> or run from inside\n"
                "  a real project directory.",
                file=sys.stderr,
            )
            sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        logger.warning("Project-root validation failed (continuing): %s", e)

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
                logger.info(
                    "Migration complete: %d files moved to %s",
                    result.get("files_copied", 0),
                    result.get("new_path", ""),
                )
    except Exception as e:
        logger.warning("Could not run storage migration: %s", e)

    # v3.0 escape hatch: CODEVIRA_NO_WATCHER=1 skips the watcher.
    # See stdio server equivalent for rationale.
    import os as _os

    if _os.environ.get("CODEVIRA_NO_WATCHER", "0") == "1":
        logger.info("Background watcher disabled via CODEVIRA_NO_WATCHER=1")
    else:
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

    # v3.0.0 audit cleanup: dropped `run_rule_inference()` (module
    # deleted in 2026-05-22 audit). Outcome tracking stays — feeds
    # AntiRegression + decision-confidence.
    #
    # v3.0 perf: runs in a daemon thread so HTTP `/` first response
    # isn't blocked by git subprocess fanout. Same fix as stdio server.
    def _run_startup_outcome_analysis() -> None:
        try:
            from indexer.outcome_tracker import analyze_session_outcomes

            analyze_session_outcomes()
        except Exception as e:
            logger.warning("Could not run startup outcome analysis: %s", e)

    import threading

    threading.Thread(
        target=_run_startup_outcome_analysis,
        name="codevira-startup-outcome-analysis",
        daemon=True,
    ).start()

    # v3.0 (2026-05-23 RC-audit follow-up): register HTTP MCP process in
    # the running-MCP registry so `codevira doctor` can detect stale
    # in-memory code vs the installed wheel.
    try:
        from mcp_server._mcp_registry import register, unregister
        from mcp_server.paths import get_project_root
        from mcp_server import __version__ as _ver
        import atexit as _atexit
        import os as _os

        try:
            _project_root_for_registry = get_project_root()
        except Exception:
            _project_root_for_registry = None
        register(transport="http", project_root=_project_root_for_registry)
        _atexit.register(unregister)
        logger.info(
            "Codevira MCP server v%s starting (pid %d, http)",
            _ver,
            _os.getpid(),
        )
    except Exception as _reg_err:
        logger.warning("MCP registry write skipped: %s", _reg_err)

    # v3.0.0: project registration (was: bidirectional preference /
    # rule sync). Best-effort; doesn't block startup.
    try:
        from mcp_server.global_sync import register_current_project

        register_current_project()
    except Exception as e:
        logger.warning("Could not register project in global inventory: %s", e)

    # v1.7: Enforce logs.retention_days (opt-in, default 0 = keep forever)
    try:
        from mcp_server.log_retention import enforce_retention

        enforce_retention()
    except Exception as e:
        logger.warning("Log retention cleanup failed: %s", e)

    # v2.2.0: prewarm_embedding_model removed (no chromadb / sentence-transformers / torch).
    # MCP server startup is <100ms; no native deps to warm.

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
    display_host = "localhost" if host in ("127.0.0.1", "::1") else host
    display_url = f"{scheme}://{display_host}:{port}/mcp"

    print()
    print("  Codevira MCP — HTTP Server")
    print("  " + "─" * 44)
    print(f"  Endpoint : {display_url}")
    print("  Transport: MCP Streamable HTTP (2025-03-26)")
    print()
    print("  ── Register in Claude Code ──────────────────")
    print("  Add to ~/.claude/settings.json (global) or")
    print("  .claude/settings.json (project):")
    print()
    print("  {")
    print('    "mcpServers": {')
    print('      "codevira": {')
    print(f'        "url": "{display_url}"')
    print("      }")
    print("    }")
    print("  }")
    print()
    if not use_https:
        print("  Tip: Use --https for a trusted HTTPS URL (required for Claude.ai)")
        print()
    print("  Press Ctrl+C to stop.")
    print()

    # ---- Bearer token auth (required for non-loopback binds) ----
    bearer_token: str | None = None
    is_loopback = host in ("127.0.0.1", "::1", "localhost")
    if not is_loopback:
        bearer_token = _get_or_create_token()
        print("  ── Auth (non-loopback) ──────────────────────")
        print(f"  Bearer token: {bearer_token}")
        print("  All /mcp requests require: Authorization: Bearer <token>")
        print(f"  Token stored in: ~/.codevira/{_TOKEN_FILE_NAME}")
        print()

    # ---- Run uvicorn ----
    app = create_app(bearer_token=bearer_token)

    uvicorn.run(
        app,
        host=host,
        port=port,
        ssl_certfile=ssl_certfile,
        ssl_keyfile=ssl_keyfile,
        log_level="warning",
    )
