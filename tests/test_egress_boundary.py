"""4.0 Step 9 · S5 — "local-first" as something the build checks.

ROADMAP.md publishes local-first as a non-negotiable. Until now that was a
property the code happened to have, not one anything verified: nothing
stopped a future module from adding `import httpx` and quietly turning a
memory tool that holds a team's architectural decisions into one that
ships them to a third party.

This walks the import graph of the shipped package and fails if any module
other than `egress.py` imports a network client. A guardrail nobody
enforces is a README sentence; this one breaks the build.

It is deliberately an AST scan rather than a runtime check. A runtime
check only sees code that ran, and the import you most need to catch is
the one on a path you did not think to exercise.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from mcp_server import egress


REPO = Path(__file__).resolve().parents[1]
PACKAGES = ("mcp_server", "indexer")

#: Modules that open sockets. `urllib.parse` is NOT here — it is string
#: manipulation with a misleading package name, and banning it would push
#: people toward hand-rolled URL parsing, which is worse.
NETWORK_MODULES = frozenset(
    {
        "urllib.request",
        "urllib.error",
        "http.client",
        "httpx",
        "requests",
        "aiohttp",
        "socket",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc.client",
    }
)

#: The single module permitted to import them.
EGRESS_MODULE = "mcp_server/egress.py"


def _python_files() -> list[Path]:
    out: list[Path] = []
    for pkg in PACKAGES:
        for path in (REPO / pkg).rglob("*.py"):
            if "__pycache__" not in path.parts:
                out.append(path)
    return out


def _imported_network_modules(path: Path) -> set[str]:
    """Network modules imported by this file, at any nesting depth.

    Walks the whole AST rather than reading top-level imports, because a
    function-scoped `import httpx` is exactly as much egress as a
    module-scoped one and is the form someone reaches for when they
    suspect they are doing something they should not.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:  # pragma: no cover
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_network(alias.name):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and _is_network(node.module):
                found.add(node.module)
    return found


def _is_network(dotted: str) -> bool:
    if dotted in NETWORK_MODULES:
        return True
    # `urllib.request.foo` style, and submodules of banned packages.
    return any(dotted.startswith(m + ".") for m in NETWORK_MODULES)


class TestOnlyEgressTouchesTheNetwork:
    def test_no_other_module_imports_a_network_client(self) -> None:
        offenders: dict[str, set[str]] = {}
        for path in _python_files():
            rel = path.relative_to(REPO).as_posix()
            if rel == EGRESS_MODULE:
                continue
            hits = _imported_network_modules(path)
            if hits:
                offenders[rel] = hits

        assert not offenders, (
            "These modules import a network client directly. Route the call "
            "through mcp_server/egress.py instead:\n"
            + "\n".join(f"  {f}: {sorted(m)}" for f, m in sorted(offenders.items()))
        )

    def test_the_scan_actually_finds_things(self, tmp_path: Path) -> None:
        """A guard that cannot fail is not a guard. Prove the detector
        catches both import forms, including a function-scoped one."""
        sample = tmp_path / "sneaky.py"
        sample.write_text(
            "def fetch():\n"
            "    import httpx\n"
            "    return httpx\n"
            "from urllib.request import urlopen\n"
        )
        assert _imported_network_modules(sample) == {"httpx", "urllib.request"}

    def test_urllib_parse_is_not_treated_as_egress(self, tmp_path: Path) -> None:
        """String parsing with a misleading package name. Banning it would
        push people toward hand-rolled URL parsing, which is worse."""
        sample = tmp_path / "ok.py"
        sample.write_text("from urllib.parse import urlparse\n")
        assert _imported_network_modules(sample) == set()

    def test_egress_is_the_only_exemption(self) -> None:
        """Guard the guard: if someone adds a second exemption, this
        should be a deliberate, visible edit."""
        assert EGRESS_MODULE == "mcp_server/egress.py"


class TestTheKillSwitch:
    def test_env_var_blocks_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(egress.NO_NETWORK_ENV, "1")
        assert not egress.is_enabled()
        assert egress.get_json("https://pypi.org/x", user_agent="t") is None

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_it_accepts_the_obvious_spellings(
        self, monkeypatch: pytest.MonkeyPatch, val: str
    ) -> None:
        """Someone reaching for this switch is trying to guarantee no
        connections. It must not fail because they typed 'true'."""
        monkeypatch.setenv(egress.NO_NETWORK_ENV, val)
        assert not egress.is_enabled()

    def test_it_is_off_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(egress.NO_NETWORK_ENV, raising=False)
        assert egress.is_enabled()

    def test_the_reason_is_reportable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`codevira doctor` should be able to say WHY, not just that."""
        monkeypatch.setenv(egress.NO_NETWORK_ENV, "1")
        assert egress.NO_NETWORK_ENV in (egress.blocked_reason() or "")

    def test_config_can_disable_it(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv(egress.NO_NETWORK_ENV, raising=False)
        monkeypatch.setattr(
            "mcp_server.storage.config.get_flag",
            lambda path, default=None: False if path == "network.enabled" else default,
        )
        assert not egress.is_enabled()
        assert "config.yaml" in (egress.blocked_reason() or "")

    def test_an_unreadable_config_does_not_break_the_tool(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(egress.NO_NETWORK_ENV, raising=False)

        def boom(*a, **k):
            raise OSError("no")

        monkeypatch.setattr("mcp_server.storage.config.get_flag", boom)
        assert egress.is_enabled()

    def test_env_wins_over_a_broken_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The kill switch must not depend on parsing anything."""
        monkeypatch.setenv(egress.NO_NETWORK_ENV, "1")

        def boom(*a, **k):
            raise OSError("no")

        monkeypatch.setattr("mcp_server.storage.config.get_flag", boom)
        assert not egress.is_enabled()


class TestItRefusesWhatItShould:
    @pytest.fixture(autouse=True)
    def net_on(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(egress.NO_NETWORK_ENV, raising=False)

    def test_plain_http_is_refused(self) -> None:
        assert egress.get_json("http://pypi.org/x", user_agent="t") is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://evil.example.com/x",
            "https://pypi.org.evil.com/x",
            "https://notpypi.org/x",
            "file:///etc/passwd",
            "",
        ],
    )
    def test_only_allowlisted_hosts(self, url: str) -> None:
        """An allowlist, not a blocklist: forgetting to add a host breaks a
        feature; forgetting to block one sends a user's decision log
        somewhere they did not choose."""
        assert egress.get_json(url, user_agent="t") is None

    def test_the_allowlist_is_one_host(self) -> None:
        """The entire network surface of the product. If this grows, it
        should be a deliberate edit someone reviews."""
        assert egress.ALLOWED_HOSTS == frozenset({"pypi.org"})

    def test_the_specific_reason_reaches_the_caller(self) -> None:
        """Collapsing every failure to "it didn't work" costs real
        diagnostic ground — a user asking why their update check is quiet
        is better served by the actual error."""
        seen: list[str] = []
        egress.get_json(
            "https://evil.example.com/x", user_agent="t", on_error=seen.append
        )
        assert seen and "evil.example.com" in seen[0]

    def test_a_broken_error_sink_does_not_break_the_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_msg):
            raise RuntimeError("bad sink")

        assert (
            egress.get_json("http://pypi.org/x", user_agent="t", on_error=boom) is None
        )


class TestUpdateCheckGoesThroughIt:
    def test_refresh_cache_uses_egress(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict = {}

        def fake(url, *, user_agent, timeout=None, on_error=None):
            seen["url"] = url
            return {"info": {"version": "99.0.0"}}

        monkeypatch.setattr(egress, "get_json", fake)
        monkeypatch.setattr(
            "mcp_server.update_check._write_cache_atomic", lambda *a, **k: None
        )
        from mcp_server import update_check

        assert update_check.refresh_cache() == 0
        assert seen["url"].startswith("https://pypi.org/")

    def test_a_blocked_check_is_not_a_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Offline is a normal operating state. The CLI must still work."""
        monkeypatch.setenv(egress.NO_NETWORK_ENV, "1")
        written: dict = {}
        monkeypatch.setattr(
            "mcp_server.update_check._write_cache_atomic", written.update
        )
        from mcp_server import update_check

        assert update_check.refresh_cache() == 1
        assert egress.NO_NETWORK_ENV in str(written.get("error", ""))
