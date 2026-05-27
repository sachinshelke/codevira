"""
test_sqlite_util.py — Pillar 3.3 shared WAL-retry helper.

Contract test for ``indexer._sqlite_util.enable_wal_with_retry``:
  * Already-WAL connections are no-ops (short-circuit)
  * Locked-database OperationalError triggers retry with backoff
  * Non-locked OperationalError propagates (caller bug, not contention)
  * After all retries exhaust, function logs + returns (non-fatal)
"""
from __future__ import annotations

import sqlite3
import unittest.mock as mock

import pytest

from indexer._sqlite_util import enable_wal_with_retry


class _FlakyConn:
    """Simulates a sqlite3.Connection where PRAGMA journal_mode=WAL
    returns 'locked' for the first N attempts then succeeds."""

    def __init__(self, lock_attempts: int = 0, already_wal: bool = False):
        self.calls: list[str] = []
        self._lock_attempts_remaining = lock_attempts
        self._already_wal = already_wal

    def execute(self, sql: str):
        self.calls.append(sql)
        if sql == "PRAGMA journal_mode":
            class _R:
                def fetchone(self_inner):
                    return ("wal" if self._already_wal else "delete",)
            return _R()
        if sql == "PRAGMA journal_mode=WAL":
            if self._lock_attempts_remaining > 0:
                self._lock_attempts_remaining -= 1
                raise sqlite3.OperationalError("database is locked")
            return None
        return None


class TestEnableWALWithRetry:

    def test_short_circuits_when_already_wal(self):
        """If PRAGMA journal_mode reports 'wal', skip the retry loop."""
        c = _FlakyConn(already_wal=True)
        enable_wal_with_retry(c, "/tmp/x.db", attempts=10)
        # Only one call: the read-back check. No PRAGMA journal_mode=WAL.
        assert c.calls == ["PRAGMA journal_mode"]

    def test_retries_on_locked_then_succeeds(self):
        """U1 contract: must retry on 'locked' error."""
        c = _FlakyConn(lock_attempts=3)
        enable_wal_with_retry(
            c, "/tmp/x.db", attempts=10, initial_delay=0.001,
        )
        # Read-back check + 3 failed attempts + 1 successful = 5
        assert len(c.calls) == 5
        assert c.calls[0] == "PRAGMA journal_mode"
        assert c.calls[1:] == ["PRAGMA journal_mode=WAL"] * 4

    def test_non_locked_error_propagates(self):
        """A non-'locked' OperationalError IS a real error and must
        propagate so the caller knows."""
        class _BadConn:
            def execute(self, sql):
                if sql == "PRAGMA journal_mode":
                    class _R:
                        def fetchone(self_):
                            return ("delete",)
                    return _R()
                raise sqlite3.OperationalError(
                    "database disk image is malformed"
                )
        with pytest.raises(sqlite3.OperationalError):
            enable_wal_with_retry(_BadConn(), "/tmp/x.db", attempts=3)

    def test_all_retries_exhausted_logs_and_continues(self, caplog):
        """If retries exhaust, log a warning + continue. Non-fatal —
        write-write serialization still works without WAL."""
        # 100 lock attempts — way more than the 5 we'll allow
        c = _FlakyConn(lock_attempts=100)
        with caplog.at_level("WARNING"):
            enable_wal_with_retry(
                c, "/tmp/some.db", attempts=5, initial_delay=0.001,
            )
        # No exception raised. Warning logged.
        assert any(
            "Could not enable WAL" in record.message
            for record in caplog.records
        ), f"Expected WAL warning in log; got: {[r.message for r in caplog.records]}"

    def test_backoff_geometric(self):
        """Each attempt's delay should grow geometrically up to the
        0.2s cap. We verify by counting time.sleep calls."""
        sleep_durations: list[float] = []
        with mock.patch("indexer._sqlite_util.time.sleep") as mock_sleep:
            mock_sleep.side_effect = lambda d: sleep_durations.append(d)
            c = _FlakyConn(lock_attempts=10)
            enable_wal_with_retry(
                c, "/tmp/x.db", attempts=10, initial_delay=0.02,
            )
        # 10 lock attempts → 10 sleeps. Geometric ×1.5 capped at 0.2.
        assert len(sleep_durations) == 10
        # First delay = 0.02
        assert sleep_durations[0] == pytest.approx(0.02, rel=0.001)
        # Each subsequent ≥ previous (capped at 0.2)
        for i in range(1, len(sleep_durations)):
            assert sleep_durations[i] >= sleep_durations[i - 1] - 0.0001
        # No delay exceeds 0.2
        for d in sleep_durations:
            assert d <= 0.2 + 0.0001
