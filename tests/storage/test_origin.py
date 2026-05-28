"""
Tests for mcp_server.storage.origin — v3.1.0 M1.

Covers the origin helper's contract: shape of the returned dict,
``CODEVIRA_IDE`` env-var lookup, ``host_hash`` stability +
privacy-preservation, fallback behavior.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from mcp_server.storage import origin


class TestCurrentOrigin:
    def test_shape(self) -> None:
        o = origin.current_origin()
        assert set(o.keys()) == {"ide", "agent_model", "host_hash", "ts"}

    def test_ide_defaults_to_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CODEVIRA_IDE", raising=False)
        assert origin.current_origin()["ide"] == "unknown"

    def test_ide_reads_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        assert origin.current_origin()["ide"] == "cursor"

    def test_agent_model_optional(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CODEVIRA_AGENT_MODEL", raising=False)
        assert origin.current_origin()["agent_model"] is None
        monkeypatch.setenv("CODEVIRA_AGENT_MODEL", "claude-opus-4-7")
        assert origin.current_origin()["agent_model"] == "claude-opus-4-7"

    def test_agent_model_empty_string_treated_as_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Some IDEs may set an empty string — treat as None for
        consistency with absence."""
        monkeypatch.setenv("CODEVIRA_AGENT_MODEL", "")
        assert origin.current_origin()["agent_model"] is None

    def test_ts_is_iso_utc(self) -> None:
        ts = origin.current_origin()["ts"]
        # ISO 8601 UTC with timezone offset
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts)
        assert ts.endswith("+00:00") or ts.endswith("Z")

    def test_two_calls_have_distinct_ts(self) -> None:
        """Per-call ts so per-record timestamps are honest."""
        import time

        t1 = origin.current_origin()["ts"]
        time.sleep(0.005)  # ensure clock advances on coarse-resolution systems
        t2 = origin.current_origin()["ts"]
        assert t1 != t2


class TestHostHash:
    def test_length_12_hex(self) -> None:
        h = origin._host_hash()
        assert len(h) == 12
        # Either 12 hex chars or the "unknown" fallback (only fires if
        # both uuid.getnode AND getpass.getuser fail, which won't on
        # any normal test environment).
        assert re.match(r"^[0-9a-f]{12}$", h) or h == "unknown"

    def test_stable_within_process(self) -> None:
        """LRU cache guarantees stability across calls."""
        assert origin._host_hash() == origin._host_hash()

    def test_does_not_leak_plaintext_user_or_host(self) -> None:
        """SHA1 truncation must obscure raw identifying info — the
        hash should not contain plaintext bytes from the user or
        hostname (privacy-preserving for committed JSONL files).
        """
        import getpass

        try:
            user = getpass.getuser()
        except Exception:  # pragma: no cover
            pytest.skip("no user available")
        h = origin._host_hash()
        if user and len(user) >= 3:
            assert user not in h, "username leaked into host_hash"

    def test_matches_documented_formula(self) -> None:
        """Catches a regression that swaps the hash algorithm or the
        input mix (e.g., dropping the username component)."""
        import getpass
        import uuid

        try:
            mac_bytes = uuid.getnode().to_bytes(6, "big")
            user = getpass.getuser()
        except Exception:  # pragma: no cover
            pytest.skip("env doesn't expose mac + user")

        raw = mac_bytes + user.encode("utf-8", errors="replace")
        expected = hashlib.sha1(raw).hexdigest()[:12]
        assert origin._host_hash() == expected
