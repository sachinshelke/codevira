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


# ──────────────────────────────────────────────────────────────────────
# End-to-end: origin embeds into downstream stores
# ──────────────────────────────────────────────────────────────────────


class TestOriginE2EOnDecisionWrite:
    """The point of M1 is that every write embeds origin. Verifies the
    full pipe: current_origin → decisions_store.record → re-read row
    carries all four origin fields (ide, agent_model, host_hash, ts)."""

    def test_decision_carries_full_origin_dict(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import mcp_server.paths as paths_module
        from mcp_server.storage import decisions_store

        # Isolated project so we don't write into real memory.
        root = tmp_path / "proj"
        (root / ".codevira").mkdir(parents=True)
        (root / ".codevira" / "config.yaml").write_text(
            "project:\n  name: e2e-origin\n"
        )
        monkeypatch.setattr(paths_module, "_project_dir_override", None)
        monkeypatch.chdir(root.resolve())

        # Stamp identity for this write.
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        monkeypatch.setenv("CODEVIRA_AGENT_MODEL", "test-model-v9")

        did = decisions_store.record(decision="origin e2e", tags=["m1"])
        row = decisions_store.get(did)
        assert row is not None
        o = row.get("origin")
        assert isinstance(o, dict), f"origin not embedded: row={row}"
        # All four documented fields land on the row.
        assert o.get("ide") == "cursor"
        assert o.get("agent_model") == "test-model-v9"
        assert isinstance(o.get("host_hash"), str)
        assert (
            re.match(r"^[0-9a-f]{12}$", o["host_hash"]) or o["host_hash"] == "unknown"
        )
        assert isinstance(o.get("ts"), str) and ("T" in o["ts"])


# ──────────────────────────────────────────────────────────────────────
# M1 minor + polish coverage
# ──────────────────────────────────────────────────────────────────────


class TestMidProcessIdeReread:
    """ide / agent_model are re-read each call (no caching). A mid-process
    monkeypatch switch from cursor → windsurf must be honored."""

    def test_ide_switches_within_process(self, monkeypatch) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        first = origin.current_origin()["ide"]
        monkeypatch.setenv("CODEVIRA_IDE", "windsurf")
        second = origin.current_origin()["ide"]
        monkeypatch.delenv("CODEVIRA_IDE")
        third = origin.current_origin()["ide"]
        assert (first, second, third) == ("cursor", "windsurf", "unknown")


class TestHostHashCacheBehavior:
    """host_hash is lru_cached; CODEVIRA_IDE / CODEVIRA_AGENT_MODEL changes
    do NOT trigger recomputation."""

    def test_cache_holds_through_env_changes(self, monkeypatch) -> None:
        h0 = origin._host_hash()
        monkeypatch.setenv("CODEVIRA_IDE", "cursor")
        monkeypatch.setenv("CODEVIRA_AGENT_MODEL", "opus")
        h1 = origin._host_hash()
        assert h0 == h1
        assert origin._host_hash.cache_info().hits >= 1


class TestHostHashUnknownFallback:
    """When both uuid.getnode and getpass.getuser raise, _host_hash
    returns the literal string 'unknown' (container without /etc/passwd
    + no NIC). Documented fallback; locked here."""

    def test_returns_unknown_when_both_inputs_fail(self, monkeypatch) -> None:
        origin._host_hash.cache_clear()
        try:
            monkeypatch.setattr(
                origin.uuid,
                "getnode",
                lambda: (_ for _ in ()).throw(OSError()),
            )
            monkeypatch.setattr(
                origin.getpass,
                "getuser",
                lambda: (_ for _ in ()).throw(OSError()),
            )
            assert origin._host_hash() == "unknown"
        finally:
            origin._host_hash.cache_clear()


class TestTsIsTimezoneAwareUtc:
    """ts MUST come from datetime.now(timezone.utc) — a naive utcnow
    would produce '+00:00' or 'Z' but lacks tzinfo when parsed back."""

    def test_ts_round_trips_as_aware_utc(self) -> None:
        from datetime import datetime, timezone

        ts = origin.current_origin()["ts"]
        # Parse back. Python's fromisoformat handles +00:00; Z requires
        # a transform in older Pythons but is acceptable.
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        assert dt.tzinfo is not None, f"ts is naive (no tzinfo): {ts}"
        assert dt.utcoffset() == timezone.utc.utcoffset(dt)


class TestHostHashStableAcrossProcesses:
    """Two MCP server processes on the same host MUST hash to the same
    string. We can't fork in a unit test cleanly but we can re-call
    after cache_clear (simulates a fresh import on the same host)."""

    def test_stable_across_cache_clears(self) -> None:
        origin._host_hash.cache_clear()
        h1 = origin._host_hash()
        origin._host_hash.cache_clear()
        h2 = origin._host_hash()
        assert h1 == h2 and isinstance(h1, str) and len(h1) in (12, len("unknown"))


class TestNonCanonicalIdePassesThrough:
    """No validation on CODEVIRA_IDE today — non-canonical strings
    pass through. Locks the current behavior; a future validator
    landing should break this and force a deliberate update."""

    def test_garbage_ide_string_returned_as_is(self, monkeypatch) -> None:
        monkeypatch.setenv("CODEVIRA_IDE", "not-an-ide-99")
        assert origin.current_origin()["ide"] == "not-an-ide-99"


class TestAgentModelWhitespaceNormalization:
    """current_origin uses `or None`; empty string → None already
    (covered). Whitespace and literal 'null'/'None' currently pass
    through. Lock the gap so the future trim/normalize lands deliberately."""

    def test_whitespace_only_currently_passes_through(self, monkeypatch) -> None:
        monkeypatch.setenv("CODEVIRA_AGENT_MODEL", "   ")
        # Locked-in current behavior.
        assert origin.current_origin()["agent_model"] == "   "

    def test_literal_null_string_passes_through(self, monkeypatch) -> None:
        monkeypatch.setenv("CODEVIRA_AGENT_MODEL", "null")
        assert origin.current_origin()["agent_model"] == "null"
