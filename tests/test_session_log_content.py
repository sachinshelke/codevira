"""
test_session_log_content.py — a session log must keep what it was given.

Found 2026-11-25 on released 4.2.0: ``write_session_log`` REQUIRES
``decisions``, ``files_changed`` and ``next_steps``, stored none of them, and
answered "logged". The v2.2.0 JSONL repoint (d6eaf27) forwarded only an ``id``
key that the tool's own schema does not offer. Measured on one machine: 971
logs across four projects, 22 of them linked to any decision.

The earlier tests could not see it: they asserted the echoed ``session_id``,
which is the one field that always survived. These read back from disk and
through the tools an agent would use.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=proj, check=True)
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(proj))
    monkeypatch.setenv("CODEVIRA_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(proj)
    (proj / ".codevira").mkdir()
    (proj / ".codevira/config.yaml").write_text("schema_version: 1\n")
    return proj


def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    from mcp_server.server import call_tool

    return json.loads(asyncio.run(call_tool(name, args))[0].text)


def _log(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "session_id": "s1",
        "task": "t",
        "phase": "p",
        "files_changed": [],
        "decisions": [],
        "next_steps": [],
    }
    args.update(over)
    return _call("write_session_log", args)


def _rows(project: Path) -> list[dict[str, Any]]:
    f = project / ".codevira/sessions.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()]


def _decisions(project: Path) -> list[dict[str, Any]]:
    from mcp_server.storage import decisions_store

    return decisions_store._read_merged()


def _record(text: str, **kw: Any) -> str:
    from mcp_server.storage import decisions_store

    return decisions_store.record(decision=text, **kw)


LONG = "Export refuses SQL when the JSONL store holds rows the graph lacks"
FULL = LONG + ". It points to the JSON command instead"


class TestNothingIsLost:
    def test_every_required_field_is_on_disk(self, project: Path) -> None:
        """THE regression test."""
        _log(
            files_changed=["giraffe.py"],
            decisions=[
                {
                    "decision": "Use hippopotamus storage",
                    "context": "because walrus",
                    "file_path": "a.py",
                }
            ],
            next_steps=["feed the pelican"],
        )
        row = _rows(project)[-1]
        assert row["files_changed"] == ["giraffe.py"]
        assert row["next_steps"] == ["feed the pelican"]
        assert row["decisions"][0]["decision"] == "Use hippopotamus storage"
        assert row["decisions"][0]["context"] == "because walrus"

    def test_a_decision_is_findable_like_any_other(self, project: Path) -> None:
        res = _log(
            decisions=[
                {
                    "decision": "Use hippopotamus storage",
                    "context": "because walrus",
                    "file_path": "a.py",
                }
            ]
        )
        found = _call("search_decisions", {"query": "hippopotamus", "full": True})
        assert [r["id"] for r in found["results"]] == res["decision_ids"]
        (d,) = _decisions(project)
        assert d["context"] == "because walrus"
        assert d["file_path"] == "a.py"
        assert d["session_id"] == "s1"
        assert _rows(project)[-1]["decision_ids"] == [d["id"]]

    def test_the_next_session_sees_the_next_steps(self, project: Path) -> None:
        """Stored but never read is the same loss one step later."""
        _log(task="did the thing", next_steps=["feed the pelican"])
        newest = _call("get_session_context", {})["recent_sessions"][0]
        assert newest["next_steps"] == ["feed the pelican"]
        assert newest["task"] == "did the thing"

    def test_the_answer_says_what_was_kept(self, project: Path) -> None:
        res = _log(
            files_changed=["a", "b"],
            decisions=[{"decision": "One thing"}],
            next_steps=["x"],
        )
        assert res["stored"] == {
            "files_changed": 2,
            "decisions": 1,
            "next_steps": 1,
        }

    def test_secrets_are_scrubbed_in_every_new_field(self, project: Path) -> None:
        key = "sk-ant-api03-" + "A" * 40
        _log(
            files_changed=[f"{key}.py"],
            decisions=[{"decision": f"Use {key}", "context": f"key {key}"}],
            next_steps=[f"rotate {key}"],
        )
        for f in (project / ".codevira").rglob("*.jsonl"):
            assert key not in f.read_text(), f.name


class TestRestatedDecisions:
    """Agents record a decision, then repeat it in the log. The repeat must
    not become a twin, and must never REPLACE the detailed original."""

    @pytest.mark.parametrize(
        "again",
        [
            LONG,
            "  export refuses sql when the JSONL store holds rows the graph lacks. ",
            FULL,
            "Export refuses SQL when the JSONL store holds…",
            "Export refuses SQL when the JSONL store holds...",
        ],
    )
    def test_a_repeat_links_to_the_original(self, project: Path, again: str) -> None:
        did = _record(FULL, context="the long reasoning")
        res = _log(decisions=[{"decision": again}])
        assert res["decision_ids"] == [did]
        (d,) = _decisions(project)
        assert d["context"] == "the long reasoning" and not d.get("superseded_by")

    def test_an_id_links_without_recording(self, project: Path) -> None:
        did = _record(LONG)
        assert _log(decisions=[{"id": did}])["decision_ids"] == [did]
        assert _log(decisions=[did])["decision_ids"] == [did]
        assert len(_decisions(project)) == 1

    def test_writing_the_same_log_twice_records_once(self, project: Path) -> None:
        a = _log(decisions=[{"decision": LONG}])
        b = _log(decisions=[{"decision": LONG}])
        assert a["decision_ids"] == b["decision_ids"]
        assert len(_decisions(project)) == 1

    @pytest.mark.parametrize(
        "first, second",
        [
            # One token apart: record_decision would supersede the first.
            (
                "The retry timeout for the payment gateway client is 30 seconds",
                "The retry timeout for the payment gateway client is 60 seconds",
            ),
            # A prefix in characters but not in words.
            (
                "The retry timeout for the payment gateway is 300",
                "The retry timeout for the payment gateway is 30",
            ),
            # Contained, but the original says the opposite.
            (
                "Never switch the workspace package manager from pnpm to npm",
                "Switch the workspace package manager from pnpm to npm",
            ),
            # Too short to be confident it is the same decision.
            ("Use Postgres. Keep Redis for the cache", "Use Postgres"),
            # The start of a sentence is not the sentence.
            (
                "Use Postgres as the primary job queue backend only in tests",
                "Use Postgres as the primary job queue backend",
            ),
            # The second sentence takes the first one back.
            (
                "Use Postgres as the primary job queue backend. Never in prod",
                "Use Postgres as the primary job queue backend",
            ),
        ],
    )
    def test_a_different_decision_is_kept_beside_it(
        self, project: Path, first: str, second: str
    ) -> None:
        old = _record(first)
        res = _log(decisions=[{"decision": second}])
        assert res["decision_ids"] != [old]
        got = {d["decision"]: d.get("superseded_by") for d in _decisions(project)}
        assert got == {first: None, second: None}

    def test_same_words_about_another_file_is_another_decision(
        self, project: Path
    ) -> None:
        old = _record(LONG, file_path="a.py")
        res = _log(decisions=[{"decision": LONG, "file_path": "b.py"}])
        assert res["decision_ids"] != [old]

    def test_a_retired_decision_is_not_revived_by_a_link(self, project: Path) -> None:
        from mcp_server.storage import decisions_store

        old = _record(LONG)
        decisions_store.mark_outdated(old, reason="no longer true")
        res = _log(decisions=[{"decision": LONG}])
        assert res["decision_ids"] != [old]

    def test_context_given_with_a_repeat_is_kept(self, project: Path) -> None:
        _record(LONG)
        _log(decisions=[{"decision": LONG, "context": "new detail"}])
        assert _rows(project)[-1]["decisions"][0]["context"] == "new detail"


class TestAwkwardInput:
    def test_a_clash_with_a_locked_decision_is_reported(self, project: Path) -> None:
        lock = _record(
            "AgentStore uses pnpm workspaces, do not switch package manager",
            do_not_revert=True,
        )
        res = _log(decisions=[{"decision": "Switch AgentStore from pnpm to npm"}])
        assert len(res["decision_ids"]) == 1
        assert any(lock in w for w in res["warnings"])

    def test_an_unknown_id_is_reported_not_stored_as_a_link(
        self, project: Path
    ) -> None:
        res = _log(decisions=[{"id": "D0ZZZZ"}])
        assert res["decision_ids"] == []
        assert any("D0ZZZZ" in w for w in res["warnings"])
        assert _rows(project)[-1]["decisions"] == [{"id": "D0ZZZZ"}]

    def test_plain_strings_are_accepted(self, project: Path) -> None:
        _log(
            files_changed="one.py",
            decisions=["A plain string decision"],
            next_steps="one step",
        )
        row = _rows(project)[-1]
        assert row["files_changed"] == ["one.py"]
        assert row["next_steps"] == ["one step"]
        assert [d["decision"] for d in _decisions(project)] == [
            "A plain string decision"
        ]

    def test_an_unusable_item_is_reported_and_the_rest_kept(
        self, project: Path
    ) -> None:
        res = _log(decisions=[{"context": "no text"}, 7, {"decision": "Good one"}])
        assert len(res["decision_ids"]) == 1
        assert len(res["warnings"]) == 2

    def test_the_log_survives_a_decision_that_cannot_be_recorded(
        self, project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from mcp_server.storage import decisions_store

        def boom(*a: Any, **k: Any) -> str:
            raise OSError("disk full")

        monkeypatch.setattr(decisions_store, "record", boom)
        res = _log(decisions=[{"decision": "Keep me"}], next_steps=["n"])
        row = _rows(project)[-1]
        assert row["decisions"][0]["decision"] == "Keep me"
        assert row["next_steps"] == ["n"]
        assert any("disk full" in w for w in res["warnings"])

    def test_optional_decision_fields_are_not_dropped(self, project: Path) -> None:
        _log(
            decisions=[
                {
                    "decision": "Lock this down for good",
                    "do_not_revert": True,
                    "tags": ["security"],
                    "would_re_examine_if": "the sun goes out",
                }
            ]
        )
        (d,) = _decisions(project)
        assert d["do_not_revert"] is True
        assert "security" in d["tags"]
        assert d["would_re_examine_if"] == "the sun goes out"

    def test_an_old_log_without_the_new_fields_still_reads(self, project: Path) -> None:
        (project / ".codevira/sessions.jsonl").write_text(
            json.dumps({"id": "S1", "ts": "2026-01-01", "session_id": "old"}) + "\n"
        )
        assert _call("get_session_context", {})["recent_sessions"][0] == {
            "session_id": "old",
            "summary": None,
            "phase": None,
        }


def test_the_schema_offers_the_id_the_code_reads() -> None:
    """The bug in one line: code read a key no caller was told about."""
    from mcp_server.server import list_tools

    tools = {t.name: t for t in asyncio.run(list_tools())}
    item = tools["write_session_log"].inputSchema["properties"]["decisions"]["items"]
    assert "id" in item["properties"]
