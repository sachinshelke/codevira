"""Tests for mcp_server.engine.token_meter — per-session token accounting."""
from __future__ import annotations

import threading

import pytest

from mcp_server.engine.token_meter import (
    TokenMeter,
    end_session,
    get_or_create_session_meter,
    get_session_meter,
    reset_meters,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_meters()
    yield
    reset_meters()


class TestTokenMeterBasics:
    def test_initial_state_zero(self):
        m = TokenMeter(session_id="s1")
        assert m.injected_total == 0
        assert m.used_total == 0

    def test_record_injected(self):
        m = TokenMeter(session_id="s1")
        m.record_injected(100, source="get_node")
        assert m.injected_total == 100
        assert m.injected_by_source["get_node"] == 100

    def test_record_used(self):
        m = TokenMeter(session_id="s1")
        m.record_injected(100, source="get_node")
        m.record_used(60, source="get_node")
        assert m.used_total == 60

    def test_negative_or_zero_ignored(self):
        m = TokenMeter(session_id="s1")
        m.record_injected(0)
        m.record_injected(-5)
        m.record_used(0)
        m.record_used(-5)
        assert m.injected_total == 0
        assert m.used_total == 0

    def test_summary_efficiency(self):
        m = TokenMeter(session_id="s1")
        m.record_injected(200, source="t1")
        m.record_used(100, source="t1")
        s = m.summary()
        assert s["injected_total"] == 200
        assert s["used_total"] == 100
        assert s["efficiency"] == 0.5

    def test_summary_top_wasted_sources(self):
        m = TokenMeter(session_id="s1")
        m.record_injected(500, source="big_waster")
        m.record_used(50, source="big_waster")
        m.record_injected(100, source="small_waster")
        m.record_used(95, source="small_waster")
        s = m.summary()
        wasted = s["top_wasted_sources"]
        assert wasted[0]["source"] == "big_waster"
        assert wasted[0]["wasted"] == 450


class TestSessionLifecycle:
    def test_create_makes_it_current(self):
        m = get_or_create_session_meter("session-1")
        assert m.session_id == "session-1"
        assert get_session_meter() is m

    def test_create_idempotent_per_session_id(self):
        m1 = get_or_create_session_meter("session-1")
        m2 = get_or_create_session_meter("session-1")
        assert m1 is m2

    def test_switching_sessions_changes_current(self):
        m1 = get_or_create_session_meter("session-1")
        m2 = get_or_create_session_meter("session-2")
        assert get_session_meter() is m2
        assert m1 is not m2

    def test_get_session_meter_none_when_no_session(self):
        assert get_session_meter() is None

    def test_end_session_returns_summary(self):
        m = get_or_create_session_meter("session-x")
        m.record_injected(123)
        summary = end_session("session-x")
        assert summary is not None
        assert summary["injected_total"] == 123

    def test_end_session_clears_current(self):
        get_or_create_session_meter("session-x")
        end_session("session-x")
        assert get_session_meter() is None

    def test_end_unknown_session_returns_none(self):
        assert end_session("does-not-exist") is None


class TestThreadSafety:
    def test_concurrent_record_injected(self):
        m = TokenMeter(session_id="s1")
        N = 100
        threads = [
            threading.Thread(target=lambda: m.record_injected(1, source="t"))
            for _ in range(N)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert m.injected_total == N
