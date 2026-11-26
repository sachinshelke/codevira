"""
test_review_findings_430.py — defects the 4.3.0 adversarial review reproduced.

Each was found by an independent reviewer trying to break the combined release,
after every author's own tests passed.
"""

from __future__ import annotations

import asyncio
import json
import os
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
    monkeypatch.chdir(proj)
    (proj / ".codevira").mkdir()
    (proj / ".codevira/config.yaml").write_text("schema_version: 1\n")
    return proj


def _log(decisions: list[Any]) -> dict[str, Any]:
    from mcp_server.server import call_tool

    args = {
        "session_id": "s",
        "task": "t",
        "phase": "p",
        "files_changed": [],
        "decisions": decisions,
        "next_steps": [],
    }
    return json.loads(asyncio.run(call_tool("write_session_log", args))[0].text)


TEXT = "Passwords are hashed with bcrypt at cost twelve, never anything weaker"


class TestARepeatKeepsWhatItAdds:
    def test_asking_to_lock_a_repeated_decision_locks_it(self, project: Path) -> None:
        from mcp_server.storage import decisions_store

        did = decisions_store.record(TEXT, file_path="auth.py")
        res = _log([{"decision": TEXT, "file_path": "auth.py", "do_not_revert": True}])
        assert res["decision_ids"] == [did]
        assert decisions_store.get(did)["do_not_revert"] is True

    def test_what_a_repeat_could_not_add_is_reported(self, project: Path) -> None:
        from mcp_server.storage import decisions_store

        did = decisions_store.record(TEXT)
        res = _log([{"decision": TEXT, "context": "must never change", "tags": ["x"]}])
        assert any(did in w and "context" in w for w in res["warnings"]), res

    def test_a_repeat_adding_nothing_is_silent(self, project: Path) -> None:
        from mcp_server.storage import decisions_store

        decisions_store.record(TEXT)
        assert "warnings" not in _log([{"decision": TEXT}])

    @pytest.mark.parametrize(
        "given, locked", [("true", True), (1, True), ("no", False)]
    )
    def test_a_non_boolean_lock_is_read_and_reported(
        self, project: Path, given: Any, locked: bool
    ) -> None:
        from mcp_server.storage import decisions_store

        res = _log([{"decision": TEXT, "do_not_revert": given}])
        (did,) = res["decision_ids"]
        assert decisions_store.get(did)["do_not_revert"] is locked
        assert any("do_not_revert" in w for w in res["warnings"])


def test_secrets_are_scrubbed_at_any_depth(project: Path) -> None:
    key = "ghp_" + "a1B2" * 9
    _log(
        [
            {
                "decision": "Rotate the deploy credentials",
                "alternatives_considered": [{"option": f"api_key={key}"}],
                "tags": [key],
            }
        ]
    )
    for f in (project / ".codevira").rglob("*.jsonl"):
        assert key not in f.read_text(), f.name


def test_a_word_that_looks_like_an_id_is_still_a_decision(project: Path) -> None:
    res = _log(["Deprecated"])
    assert len(res["decision_ids"]) == 1 and "warnings" not in res


def test_atomic_writes_outside_a_project_survive_a_deleted_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_server.storage import atomic

    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    monkeypatch.delenv("CODEVIRA_PROJECT_DIR", raising=False)
    os.rmdir(gone)
    target = tmp_path / "elsewhere" / "f.txt"
    atomic.atomic_write_text(target, "x")
    assert target.read_text() == "x"


def test_new_lock_and_backup_files_stay_out_of_git(project: Path) -> None:
    from mcp_server.cli_init import cmd_init

    (project / ".codevira/config.yaml").unlink()
    cmd_init(yes=True)
    text = (project / ".gitignore").read_text()
    assert ".codevira/*.lock" in text
    assert ".codevira/roadmap.yaml.before-adoption*" in text


def test_prune_keeps_a_roadmap_that_was_never_moved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A project renamed before upgrading leaves its only roadmap here."""
    from mcp_server.cli import _cmd_clean_orphans
    from mcp_server.paths import get_global_home

    old, kept, plain = (
        get_global_home() / "projects" / n for n in ("old_key", "adopted", "plain")
    )
    for d in (old, kept, plain):
        d.mkdir(parents=True)
        (d / "metadata.json").write_text(
            json.dumps({"original_path": str(tmp_path / ("renamed-" + d.name))})
        )
    (old / "roadmap.yaml").write_text("current_phase: {next_action: real}\n")
    (kept / "roadmap.yaml.moved-into-project").write_text("x\n")

    _cmd_clean_orphans(yes=True)

    out = capsys.readouterr().out
    assert (old / "roadmap.yaml").read_text().endswith("real}\n")
    assert "codevira-memory" in out and str(old) in out
    assert not kept.exists() and not plain.exists(), "the rest is still tidied"
