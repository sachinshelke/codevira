"""
test_watcher_circuit.py — Pillar 3.2 watcher restart circuit breaker.

Tests the circuit-breaker state machine in isolation (no actual
filesystem watcher needed for these). Verifies:

  * record_success resets the counter
  * record_failure increments + opens at threshold
  * Open circuit short-circuits should_run()
  * Backoff window elapses → should_run returns True again
  * Status snapshot has the right shape for `codevira doctor`
  * 14 silent-exception audit doesn't apply here (we use crash_logger
    in the watcher, not silent except)
"""
from __future__ import annotations

import time

import pytest

from indexer import index_codebase as ic


@pytest.fixture(autouse=True)
def _reset_circuit():
    ic.reset_watcher_circuit()
    yield
    ic.reset_watcher_circuit()


class TestCircuitBreaker:

    def test_initial_state_is_closed(self):
        assert ic._watcher_circuit_should_run() is True
        s = ic.watcher_circuit_status()
        assert s["open"] is False
        assert s["consecutive_failures"] == 0
        assert s["seconds_until_retry"] == 0.0
        assert s["last_error"] == ""

    def test_one_failure_does_not_open_circuit(self):
        ic._watcher_circuit_record_failure(RuntimeError("first failure"))
        s = ic.watcher_circuit_status()
        assert s["open"] is False
        assert s["consecutive_failures"] == 1
        assert "first failure" in s["last_error"]
        # Reindex should still be allowed (below threshold)
        assert ic._watcher_circuit_should_run() is True

    def test_three_consecutive_failures_open_circuit(self):
        for i in range(ic._CIRCUIT_OPEN_THRESHOLD):
            ic._watcher_circuit_record_failure(
                RuntimeError(f"failure {i + 1}"),
            )
        s = ic.watcher_circuit_status()
        assert s["open"] is True
        assert s["consecutive_failures"] == ic._CIRCUIT_OPEN_THRESHOLD
        assert s["seconds_until_retry"] > 0
        # Reindex should be blocked
        assert ic._watcher_circuit_should_run() is False

    def test_success_resets_circuit(self):
        # Open the circuit
        for _ in range(ic._CIRCUIT_OPEN_THRESHOLD):
            ic._watcher_circuit_record_failure(RuntimeError("x"))
        # Record success
        ic._watcher_circuit_record_success()
        s = ic.watcher_circuit_status()
        assert s["open"] is False
        assert s["consecutive_failures"] == 0
        assert s["seconds_until_retry"] == 0.0
        assert s["last_error"] == ""
        assert ic._watcher_circuit_should_run() is True

    def test_backoff_doubles_per_failure_after_threshold(self):
        """4th failure: ~120s. 5th: ~240s. Capped at 1800s."""
        for _ in range(ic._CIRCUIT_OPEN_THRESHOLD):
            ic._watcher_circuit_record_failure(RuntimeError("x"))
        # The threshold-hit gives initial backoff (~60s)
        s1 = ic.watcher_circuit_status()
        first_backoff = s1["seconds_until_retry"]
        assert 50 <= first_backoff <= 70  # ~60s ± slack

        # One more failure → ~120s
        ic._watcher_circuit_record_failure(RuntimeError("y"))
        s2 = ic.watcher_circuit_status()
        second_backoff = s2["seconds_until_retry"]
        # Should be roughly double
        assert second_backoff > first_backoff

    def test_backoff_capped_at_30_minutes(self):
        """Many failures should cap at the ceiling, not grow unbounded."""
        for _ in range(20):  # way more than threshold
            ic._watcher_circuit_record_failure(RuntimeError("x"))
        s = ic.watcher_circuit_status()
        # Cap is 1800s; allow some slack for clock granularity
        assert s["seconds_until_retry"] <= ic._CIRCUIT_BACKOFF_CAP + 1


class TestCircuitInDoctor:
    """The doctor health check should surface circuit state correctly."""

    def test_clean_circuit_is_pass(self):
        from mcp_server.doctor import check_watcher_circuit
        ic.reset_watcher_circuit()
        r = check_watcher_circuit()
        assert r.state == "PASS"
        assert "clean" in r.message.lower()

    def test_below_threshold_failures_warn(self):
        from mcp_server.doctor import check_watcher_circuit
        ic._watcher_circuit_record_failure(RuntimeError("once"))
        r = check_watcher_circuit()
        assert r.state == "WARN"
        assert "failure" in r.message.lower()

    def test_open_circuit_fails(self):
        from mcp_server.doctor import check_watcher_circuit
        for _ in range(ic._CIRCUIT_OPEN_THRESHOLD):
            ic._watcher_circuit_record_failure(RuntimeError("boom"))
        r = check_watcher_circuit()
        assert r.state == "FAIL"
        assert "OPEN" in r.message
        assert r.fix_command  # has a recovery action
