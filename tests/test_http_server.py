"""
Tests for mcp_server/http_server.py -- HTTP transport components.

Covers:
  - _get_or_create_token: creation, persistence, file permissions
  - _BearerAuthMiddleware: auth enforcement, health endpoint bypass
  - create_app: Starlette app creation with and without bearer_token
  - generate_mkcert_certs: subprocess calls, missing mkcert
  - _certs_exist: cert + key file checks
  - Token determinism and 0o600 permissions
"""
from __future__ import annotations

import os
import stat
import sys
import types
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Pre-seed sys.modules with mock mcp and tree-sitter packages so that
# mcp_server.http_server (which imports mcp_server.server) can load without
# the real mcp package installed.
# ---------------------------------------------------------------------------

_modules_to_mock = [
    "mcp",
    "mcp.server",
    "mcp.server.stdio",
    "mcp.server.streamable_http_manager",
    "mcp.types",
    "tree_sitter_language_pack",
    "tree_sitter",
]

for _mod_name in _modules_to_mock:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = types.ModuleType(_mod_name)

# Provide symbols expected by server.py
_mock_text_content = type("TextContent", (), {
    "__init__": lambda self, **kw: self.__dict__.update(kw),
})
_mock_tool = type("Tool", (), {
    "__init__": lambda self, **kw: self.__dict__.update(kw),
})
_mock_server_cls = MagicMock()
_mock_server_instance = MagicMock()
_mock_server_cls.return_value = _mock_server_instance
_mock_server_instance.call_tool.return_value = lambda fn: fn
_mock_server_instance.list_tools.return_value = lambda fn: fn
_mock_server_instance.list_prompts.return_value = lambda fn: fn
_mock_server_instance.get_prompt.return_value = lambda fn: fn

sys.modules["mcp.server"].Server = _mock_server_cls
sys.modules["mcp.types"].Tool = _mock_tool
sys.modules["mcp.types"].TextContent = _mock_text_content
sys.modules["mcp.server.streamable_http_manager"].StreamableHTTPSessionManager = MagicMock()

# tree-sitter stubs — only when the real module isn't already loaded.
# Overwriting real attributes here would corrupt test_treesitter_parser.py
# tests that run later in the same session (they use the real library).
_ts_lp = sys.modules["tree_sitter_language_pack"]
if not hasattr(_ts_lp, "get_language"):
    _ts_lp.get_language = MagicMock(return_value=None)
if not hasattr(_ts_lp, "get_parser"):
    _ts_lp.get_parser = MagicMock(return_value=None)
_ts = sys.modules["tree_sitter"]
if not hasattr(_ts, "Node"):
    _ts.Node = type("Node", (), {})

# Now safe to import
from mcp_server.http_server import (  # noqa: E402
    _get_or_create_token,
    _BearerAuthMiddleware,
    create_app,
    generate_mkcert_certs,
    _certs_exist,
)

from starlette.applications import Starlette  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import mcp_server.paths as paths  # noqa: E402


# ---------------------------------------------------------------------------
# _get_or_create_token
# ---------------------------------------------------------------------------

class TestGetOrCreateToken:
    def test_creates_token_file(self, tmp_path, monkeypatch):
        """Token file is created when it doesn't exist."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        token = _get_or_create_token()
        token_path = tmp_path / "http_bearer_token"
        assert token_path.exists()
        assert len(token) > 20  # urlsafe token should be long

    def test_reads_existing_token(self, tmp_path, monkeypatch):
        """If the token file already exists, the same token is returned."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        token_path = tmp_path / "http_bearer_token"
        token_path.write_text("my-preset-token\n", encoding="utf-8")
        token = _get_or_create_token()
        assert token == "my-preset-token"

    def test_token_is_deterministic(self, tmp_path, monkeypatch):
        """Reading the same file twice returns the same token."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        token1 = _get_or_create_token()
        token2 = _get_or_create_token()
        assert token1 == token2

    def test_token_file_permissions_0600(self, tmp_path, monkeypatch):
        """Token file is created with 0o600 permissions (owner read/write only)."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        _get_or_create_token()
        token_path = tmp_path / "http_bearer_token"
        mode = stat.S_IMODE(os.stat(token_path).st_mode)
        assert mode == 0o600

    def test_empty_token_file_regenerates(self, tmp_path, monkeypatch):
        """If the token file exists but is empty, a new token is generated."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        token_path = tmp_path / "http_bearer_token"
        token_path.write_text("", encoding="utf-8")
        token = _get_or_create_token()
        assert len(token) > 20
        # File should now contain the new token
        stored = token_path.read_text(encoding="utf-8").strip()
        assert stored == token


# ---------------------------------------------------------------------------
# _BearerAuthMiddleware
# ---------------------------------------------------------------------------

def _make_authed_app(token: str) -> Starlette:
    """Build a minimal Starlette app with BearerAuthMiddleware for testing."""

    async def health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def mcp_endpoint(request: Request) -> JSONResponse:
        return JSONResponse({"tool": "dispatched"})

    return Starlette(
        routes=[
            Route("/", endpoint=health, methods=["GET"]),
            Route("/mcp", endpoint=mcp_endpoint, methods=["POST"]),
        ],
        middleware=[Middleware(_BearerAuthMiddleware, token=token)],
    )


class TestBearerAuthMiddleware:
    def test_rejects_without_token(self):
        """POST /mcp without Authorization header returns 401."""
        app = _make_authed_app("secret-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/mcp", json={"tool": "get_roadmap"})
        assert resp.status_code == 401
        data = resp.json()
        assert "Unauthorized" in data["error"]

    def test_rejects_wrong_token(self):
        """POST /mcp with wrong token returns 401."""
        app = _make_authed_app("correct-token")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/mcp",
            json={"tool": "get_roadmap"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401

    def test_allows_valid_token(self):
        """POST /mcp with correct Bearer token returns 200."""
        app = _make_authed_app("my-secret")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/mcp",
            json={},
            headers={"Authorization": "Bearer my-secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["tool"] == "dispatched"

    def test_health_endpoint_no_auth_required(self):
        """GET / (health check) should work without any auth header."""
        app = _make_authed_app("my-secret")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_endpoint_ignores_bad_token(self):
        """GET / works even with an invalid Authorization header."""
        app = _make_authed_app("my-secret")
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/", headers={"Authorization": "Bearer totally-wrong"})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------

class TestCreateApp:
    def test_returns_starlette_app_without_token(self):
        """create_app() without bearer_token returns a Starlette instance."""
        app = create_app()
        assert isinstance(app, Starlette)

    def test_returns_starlette_app_with_token(self):
        """create_app() with bearer_token returns a Starlette instance with middleware."""
        app = create_app(bearer_token="test-token-123")
        assert isinstance(app, Starlette)

    def test_health_route_works_without_token(self):
        """Health endpoint returns ok on the app created without auth."""
        app = create_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["transport"] == "streamable-http"


# ---------------------------------------------------------------------------
# generate_mkcert_certs
# ---------------------------------------------------------------------------

class TestGenerateMkcertCerts:
    def test_calls_mkcert_subprocess(self, tmp_path, monkeypatch):
        """generate_mkcert_certs calls mkcert with correct arguments."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        with patch("shutil.which", return_value="/usr/local/bin/mkcert"), \
             patch("subprocess.run") as mock_run:
            cert, key = generate_mkcert_certs()
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "mkcert"
        assert "localhost" in call_args
        assert "127.0.0.1" in call_args
        assert "::1" in call_args

    def test_raises_when_mkcert_not_found(self, tmp_path, monkeypatch):
        """generate_mkcert_certs raises RuntimeError if mkcert is not installed."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError, match="mkcert not found"):
                generate_mkcert_certs()

    def test_creates_certs_dir(self, tmp_path, monkeypatch):
        """generate_mkcert_certs creates the certs directory if it doesn't exist."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        certs_dir = tmp_path / "certs"
        assert not certs_dir.exists()
        with patch("shutil.which", return_value="/usr/local/bin/mkcert"), \
             patch("subprocess.run"):
            generate_mkcert_certs()
        assert certs_dir.exists()

    def test_returns_cert_and_key_paths(self, tmp_path, monkeypatch):
        """generate_mkcert_certs returns (cert_path, key_path) tuple."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        with patch("shutil.which", return_value="/usr/local/bin/mkcert"), \
             patch("subprocess.run"):
            cert, key = generate_mkcert_certs()
        assert str(cert).endswith("localhost.pem")
        assert str(key).endswith("localhost-key.pem")


# ---------------------------------------------------------------------------
# _certs_exist
# ---------------------------------------------------------------------------

class TestCertsExist:
    def test_returns_false_when_no_certs(self, tmp_path, monkeypatch):
        """_certs_exist returns False when cert files don't exist."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        assert _certs_exist() is False

    def test_returns_false_when_only_cert(self, tmp_path, monkeypatch):
        """_certs_exist returns False when only cert exists (no key)."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "localhost.pem").write_text("cert")
        assert _certs_exist() is False

    def test_returns_false_when_only_key(self, tmp_path, monkeypatch):
        """_certs_exist returns False when only key exists (no cert)."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "localhost-key.pem").write_text("key")
        assert _certs_exist() is False

    def test_returns_true_when_both_exist(self, tmp_path, monkeypatch):
        """_certs_exist returns True when both cert and key exist."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        certs_dir = tmp_path / "certs"
        certs_dir.mkdir()
        (certs_dir / "localhost.pem").write_text("cert")
        (certs_dir / "localhost-key.pem").write_text("key")
        assert _certs_exist() is True


# ---------------------------------------------------------------------------
# Certificate path helpers
# ---------------------------------------------------------------------------

from mcp_server.http_server import _certs_dir, _cert_file, _key_file  # noqa: E402


class TestCertPathHelpers:
    def test_certs_dir_under_global_home(self, tmp_path, monkeypatch):
        """_certs_dir returns a path under the global home directory."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        result = _certs_dir()
        assert result == tmp_path / "certs"
        assert str(result).startswith(str(tmp_path))

    def test_cert_file_name(self, tmp_path, monkeypatch):
        """_cert_file returns a path ending in localhost.pem."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        result = _cert_file()
        assert result.name == "localhost.pem"
        assert result.parent == tmp_path / "certs"

    def test_key_file_name(self, tmp_path, monkeypatch):
        """_key_file returns a path ending in localhost-key.pem."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        result = _key_file()
        assert result.name == "localhost-key.pem"
        assert result.parent == tmp_path / "certs"

    def test_cert_and_key_share_same_parent(self, tmp_path, monkeypatch):
        """_cert_file and _key_file are in the same directory."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        assert _cert_file().parent == _key_file().parent


# ---------------------------------------------------------------------------
# run_http_server — bearer token logic
# ---------------------------------------------------------------------------

from mcp_server.http_server import run_http_server  # noqa: E402


class TestRunHttpServerBearerToken:
    """Tests for bearer token logic in run_http_server.

    uvicorn is imported locally inside run_http_server, so we inject a mock
    module into sys.modules rather than patching a module-level attribute.
    All startup side-effects (crash handler, migration, watcher, learning,
    global sync) are wrapped in try/except inside run_http_server, so we
    make their imports raise to suppress them cleanly.
    """

    @staticmethod
    def _make_mock_uvicorn():
        """Create a mock uvicorn module with a .run callable."""
        mock_mod = types.ModuleType("uvicorn")
        mock_mod.run = MagicMock()
        return mock_mod

    def test_non_loopback_creates_token(self, tmp_path, monkeypatch):
        """When host is 0.0.0.0 (non-loopback), a bearer token is created."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        mock_uvicorn = self._make_mock_uvicorn()

        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}), \
             patch("mcp_server.http_server._get_or_create_token", return_value="test-token-xyz") as mock_token, \
             patch("mcp_server.http_server.create_app", return_value=MagicMock()) as mock_create_app, \
             patch("builtins.print"):
            run_http_server(host="0.0.0.0", port=7007)

        mock_token.assert_called_once()
        mock_create_app.assert_called_once_with(bearer_token="test-token-xyz")

    def test_loopback_no_token(self, tmp_path, monkeypatch):
        """When host is 127.0.0.1 (loopback), no bearer token is created."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        mock_uvicorn = self._make_mock_uvicorn()

        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}), \
             patch("mcp_server.http_server._get_or_create_token") as mock_token, \
             patch("mcp_server.http_server.create_app", return_value=MagicMock()) as mock_create_app, \
             patch("builtins.print"):
            run_http_server(host="127.0.0.1", port=7007)

        mock_token.assert_not_called()
        mock_create_app.assert_called_once_with(bearer_token=None)

    def test_localhost_no_token(self, tmp_path, monkeypatch):
        """When host is 'localhost' (loopback alias), no bearer token is created."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        mock_uvicorn = self._make_mock_uvicorn()

        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}), \
             patch("mcp_server.http_server._get_or_create_token") as mock_token, \
             patch("mcp_server.http_server.create_app", return_value=MagicMock()) as mock_create_app, \
             patch("builtins.print"):
            run_http_server(host="localhost", port=7007)

        mock_token.assert_not_called()
        mock_create_app.assert_called_once_with(bearer_token=None)

    def test_non_loopback_token_printed_to_stdout(self, tmp_path, monkeypatch, capsys):
        """When host is non-loopback, the token is printed to stdout."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        mock_uvicorn = self._make_mock_uvicorn()

        with patch.dict("sys.modules", {"uvicorn": mock_uvicorn}), \
             patch("mcp_server.http_server._get_or_create_token", return_value="visible-token-123"), \
             patch("mcp_server.http_server.create_app", return_value=MagicMock()):
            run_http_server(host="0.0.0.0", port=7007)

        captured = capsys.readouterr()
        assert "visible-token-123" in captured.out


# ---------------------------------------------------------------------------
# run_http_server — startup side-effects and HTTPS path (lines 202-281)
# ---------------------------------------------------------------------------

class TestRunHttpServer:
    def test_crash_handler_exception_does_not_crash(self):
        """If crash handler install fails, run_http_server continues."""
        with patch("mcp_server.crash_logger.install_global_handler", side_effect=RuntimeError("boom")), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("uvicorn.run"):
            run_http_server()  # Must not raise

    def test_migration_triggered_on_startup(self):
        """run_http_server triggers migration if legacy .codevira/ detected."""
        with patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=True), \
             patch("mcp_server.migrate.migrate_to_centralized", return_value={"migrated": True, "files_copied": 3, "new_path": "/tmp/x"}) as mock_migrate, \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("uvicorn.run"):
            run_http_server()
        mock_migrate.assert_called_once()

    def test_migration_exception_does_not_crash(self):
        with patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", side_effect=RuntimeError("migrate fail")), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("uvicorn.run"):
            run_http_server()  # Must not raise

    def test_watcher_exception_does_not_crash(self):
        with patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", side_effect=ImportError("watchdog missing")), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("uvicorn.run"):
            run_http_server()  # Must not raise

    def test_learning_exception_does_not_crash(self):
        with patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes", side_effect=RuntimeError("learning fail")), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("uvicorn.run"):
            run_http_server()  # Must not raise

    def test_global_sync_exception_does_not_crash(self):
        with patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", side_effect=RuntimeError("sync fail")), \
             patch("uvicorn.run"):
            run_http_server()  # Must not raise

    def test_https_mode_generates_certs_when_missing(self, tmp_path, monkeypatch):
        """When use_https=True and certs don't exist, generate them."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        cert_file = tmp_path / "certs" / "localhost.pem"
        key_file = tmp_path / "certs" / "localhost-key.pem"
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        cert_file.write_text("CERT")
        key_file.write_text("KEY")
        with patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("mcp_server.http_server._certs_exist", return_value=False), \
             patch("mcp_server.http_server.generate_mkcert_certs") as mock_gen, \
             patch("mcp_server.http_server._cert_file", return_value=cert_file), \
             patch("mcp_server.http_server._key_file", return_value=key_file), \
             patch("uvicorn.run"):
            run_http_server(use_https=True)
        mock_gen.assert_called_once()

    def test_https_mode_cert_generation_failure_returns_early(self):
        """When cert generation fails, run_http_server returns without starting uvicorn."""
        with patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("mcp_server.http_server._certs_exist", return_value=False), \
             patch("mcp_server.http_server.generate_mkcert_certs", side_effect=RuntimeError("mkcert not found")), \
             patch("uvicorn.run") as mock_uvicorn:
            run_http_server(use_https=True)
        # uvicorn.run should NOT be called when cert generation fails
        mock_uvicorn.assert_not_called()

    def test_https_mode_uses_existing_certs(self, tmp_path, monkeypatch):
        """When certs already exist, generate_mkcert_certs is NOT called."""
        monkeypatch.setattr(paths, "get_global_home", lambda: tmp_path)
        cert_file = tmp_path / "certs" / "localhost.pem"
        key_file = tmp_path / "certs" / "localhost-key.pem"
        cert_file.parent.mkdir(parents=True, exist_ok=True)
        cert_file.write_text("CERT")
        key_file.write_text("KEY")
        with patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("mcp_server.http_server._certs_exist", return_value=True), \
             patch("mcp_server.http_server.generate_mkcert_certs") as mock_gen, \
             patch("mcp_server.http_server._cert_file", return_value=cert_file), \
             patch("mcp_server.http_server._key_file", return_value=key_file), \
             patch("uvicorn.run"):
            run_http_server(use_https=True)
        mock_gen.assert_not_called()

    def test_project_dir_calls_set_project_dir(self, tmp_path):
        """When project_dir is passed, run_http_server calls set_project_dir."""
        with patch("mcp_server.paths.set_project_dir") as mock_set, \
             patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("uvicorn.run"):
            run_http_server(project_dir=tmp_path)
        mock_set.assert_called_once_with(tmp_path)

    def test_no_project_dir_skips_set_project_dir(self):
        """When project_dir is None (default), set_project_dir is not called."""
        with patch("mcp_server.paths.set_project_dir") as mock_set, \
             patch("mcp_server.crash_logger.install_global_handler"), \
             patch("mcp_server.migrate.detect_migration_needed", return_value=False), \
             patch("indexer.index_codebase.start_background_watcher", return_value=MagicMock()), \
             patch("indexer.outcome_tracker.analyze_session_outcomes"), \
             patch("indexer.rule_learner.run_rule_inference"), \
             patch("mcp_server.global_sync.import_global_to_project", return_value=None), \
             patch("uvicorn.run"):
            run_http_server()
        mock_set.assert_not_called()
