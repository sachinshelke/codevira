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
from pathlib import Path
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

# tree-sitter stubs
sys.modules["tree_sitter_language_pack"].get_language = MagicMock(return_value=None)
sys.modules["tree_sitter_language_pack"].get_parser = MagicMock(return_value=None)
sys.modules["tree_sitter"].Node = type("Node", (), {})

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
