"""
test_antigravity_hooks.py — v3.7.1 fix D: Antigravity file-hook enforcement.

Antigravity reads a hook's decision from a JSON object on STDOUT
({"decision": "deny"|"allow", ...}) rather than the exit code. This adapter
translates Antigravity's PreToolUse payload into the shared engine's event and
emits the Antigravity-shaped decision — so a do_not_revert / policy block is
enforced in Antigravity exactly as in Claude Code.

The end-to-end block is driven by the BackupExtensionGuard demo policy (blocks
*.py.bak edits), the same policy the Claude Code integration tests use.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mcp_server.engine.demo_policy import BackupExtensionGuard
from mcp_server.engine.runner import register_policy, reset_policies
from mcp_server.engine.wiring import antigravity_hooks


@pytest.fixture(autouse=True)
def _clean_policies():
    reset_policies()
    yield
    reset_policies()


def _run(payload: dict | str, event_type: str = "PreToolUse", monkeypatch=None):
    """Feed a stdin payload to antigravity_hooks.handle and capture stdout JSON."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    stdin = io.StringIO(raw)
    stdin.isatty = lambda: False  # type: ignore[assignment]
    out = io.StringIO()
    old_in, old_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = stdin, out
    try:
        rc = antigravity_hooks.handle(event_type)
    finally:
        sys.stdin, sys.stdout = old_in, old_out
    text = out.getvalue().strip()
    return rc, (json.loads(text) if text else None)


@pytest.fixture
def opted_in_project(tmp_path, monkeypatch):
    """A project that is opted-in so the hook actually enforces (not inert)."""
    proj = tmp_path / "proj"
    (proj / ".codevira").mkdir(parents=True)
    (proj / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    # Force opt-in true regardless of centralized markers.
    import mcp_server.opt_in as _opt

    monkeypatch.setattr(_opt, "activation_allowed", lambda *a, **k: True)
    return proj


class TestAntigravityEnforcement:
    def test_blocks_forbidden_edit_with_deny_decision(self, opted_in_project):
        """A policy block must emit {"decision": "deny", ...} on stdout —
        Antigravity's hard-block signal."""
        register_policy(BackupExtensionGuard())
        proj = opted_in_project
        payload = {
            "toolCall": {
                "name": "replace_file_content",
                "args": {"TargetFile": str(proj / "src" / "foo.py.bak")},
            },
            "workspacePaths": [str(proj)],
            "conversationId": "c1",
        }
        rc, resp = _run(payload)
        assert resp is not None, "hook wrote no decision"
        assert resp["decision"] == "deny", f"expected deny, got {resp}"
        assert "reason" in resp and resp["reason"]
        assert rc == 0  # Antigravity reads stdout, not the exit code

    def test_allows_safe_edit(self, opted_in_project):
        register_policy(BackupExtensionGuard())
        proj = opted_in_project
        payload = {
            "toolCall": {
                "name": "write_to_file",
                "args": {"TargetFile": str(proj / "src" / "foo.py")},
            },
            "workspacePaths": [str(proj)],
        }
        rc, resp = _run(payload)
        assert resp["decision"] == "allow", f"expected allow, got {resp}"

    def test_file_path_extracted_from_lowercase_arg_key(self, opted_in_project):
        """The exact edit-tool arg key is a live-capture unknown — the adapter
        must find the path under any of the candidate keys (here targetFile)."""
        register_policy(BackupExtensionGuard())
        proj = opted_in_project
        payload = {
            "toolCall": {
                "name": "replace_file_content",
                "args": {"targetFile": str(proj / "a.py.bak")},
            },
            "workspacePaths": [str(proj)],
        }
        _, resp = _run(payload)
        assert resp["decision"] == "deny"


class TestHookInstaller:
    """v3.7.1 fix D: install_antigravity_enforcement_hook writes a valid
    .agents/hooks.json that routes edit tools to the codevira adapter."""

    def test_writes_valid_hooks_json(self, tmp_path):
        import json as _json

        from mcp_server import ide_inject

        proj = tmp_path / "proj"
        proj.mkdir()
        path = ide_inject.install_antigravity_enforcement_hook(
            proj, "/usr/local/bin/codevira"
        )
        assert path is not None
        hooks = _json.loads((proj / ".agents" / "hooks.json").read_text())
        entry = hooks["codevira-enforcement"]["PreToolUse"][0]
        assert "write_to_file" in entry["matcher"]
        assert "replace_file_content" in entry["matcher"]
        cmd = entry["hooks"][0]["command"]
        assert "engine handle" in cmd and "--ide antigravity" in cmd
        assert "PreToolUse" in cmd

    def test_merges_with_existing_hooks(self, tmp_path):
        import json as _json

        from mcp_server import ide_inject

        proj = tmp_path / "proj"
        (proj / ".agents").mkdir(parents=True)
        (proj / ".agents" / "hooks.json").write_text(
            _json.dumps({"user-hook": {"PreToolUse": []}})
        )
        ide_inject.install_antigravity_enforcement_hook(proj, "/bin/codevira")
        hooks = _json.loads((proj / ".agents" / "hooks.json").read_text())
        # User's hook preserved; codevira's added.
        assert "user-hook" in hooks
        assert "codevira-enforcement" in hooks

    def test_idempotent(self, tmp_path):
        import json as _json

        from mcp_server import ide_inject

        proj = tmp_path / "proj"
        proj.mkdir()
        ide_inject.install_antigravity_enforcement_hook(proj, "/bin/codevira")
        ide_inject.install_antigravity_enforcement_hook(proj, "/bin/codevira")
        hooks = _json.loads((proj / ".agents" / "hooks.json").read_text())
        # Exactly one codevira entry (dict key, not duplicated).
        assert list(hooks).count("codevira-enforcement") == 1


class TestFailOpen:
    def test_empty_stdin_allows(self):
        _, resp = _run("")
        assert resp == {"decision": "allow"}

    def test_malformed_json_allows(self):
        _, resp = _run("{not valid json")
        assert resp == {"decision": "allow"}

    def test_non_pretooluse_event_allows(self):
        _, resp = _run({"anything": 1}, event_type="Stop")
        assert resp == {"decision": "allow"}

    def test_missing_toolcall_allows(self):
        _, resp = _run({"workspacePaths": ["/tmp"]})
        assert resp == {"decision": "allow"}

    def test_non_opted_project_is_inert(self, tmp_path, monkeypatch):
        """A project the user never init-ed must allow (stay inert)."""
        register_policy(BackupExtensionGuard())
        proj = tmp_path / "uninited"
        proj.mkdir()
        import mcp_server.opt_in as _opt

        monkeypatch.setattr(_opt, "activation_allowed", lambda *a, **k: False)
        payload = {
            "toolCall": {
                "name": "replace_file_content",
                "args": {"TargetFile": str(proj / "foo.py.bak")},
            },
            "workspacePaths": [str(proj)],
        }
        _, resp = _run(payload)
        assert resp["decision"] == "allow"
