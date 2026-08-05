"""4.0 Step 3.4 — recover arguments serialized into the decision body.

A malformed tool call can arrive with the other arguments embedded in the
decision text as `<parameter name="X">value` blocks. D000014 diagnosed
this in May 2026 and it was never systematically fixed. Measured across
every registered project: 262 of 1269 base decisions (21%) carry a leaked
block — 164 lost their `context`, 99 lost `file_path`, 96 lost `tags`, and
14 were INTENDED `do_not_revert` with the lock never landing.

Recovering beats rejecting: the decision is real, only its envelope was
malformed.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mcp_server.storage import decisions_store


class TestRecoverStrandedParameters:
    def test_recovers_every_supported_field(self) -> None:
        text = (
            "Use bcrypt for passwords.</decision>\n"
            '<parameter name="file_path">auth.py</parameter>\n'
            '<parameter name="symbol">hash_password</parameter>\n'
            '<parameter name="context">argon2 needs a C toolchain</parameter>\n'
            '<parameter name="tags">security, auth</parameter>\n'
            '<parameter name="do_not_revert">true'
        )
        clean, rec = decisions_store._recover_stranded_parameters(text)
        assert clean == "Use bcrypt for passwords."
        assert rec["file_path"] == "auth.py"
        assert rec["symbol"] == "hash_password"
        assert rec["context"] == "argon2 needs a C toolchain"
        assert rec["tags"] == ["security", "auth"]
        assert rec["do_not_revert"] is True

    def test_clean_text_passes_through_untouched(self) -> None:
        assert decisions_store._recover_stranded_parameters("plain decision") == (
            "plain decision",
            {},
        )

    def test_empty_input_is_safe(self) -> None:
        assert decisions_store._recover_stranded_parameters("") == ("", {})

    def test_unknown_parameter_names_are_stripped_but_not_returned(self) -> None:
        """`taskId`, `command` etc. appear in real data; drop them from the
        recovered mapping but still clean the prose."""
        text = 'Do the thing.</decision>\n<parameter name="taskId">T-99</parameter>'
        clean, rec = decisions_store._recover_stranded_parameters(text)
        assert clean == "Do the thing."
        assert "taskId" not in rec

    def test_false_do_not_revert_is_not_coerced_true(self) -> None:
        text = 'X.</decision>\n<parameter name="do_not_revert">false'
        _, rec = decisions_store._recover_stranded_parameters(text)
        assert rec["do_not_revert"] is False

    def test_never_returns_an_empty_decision(self) -> None:
        """If stripping would consume everything, keep the original."""
        text = '<parameter name="tags">a, b'
        clean, _ = decisions_store._recover_stranded_parameters(text)
        assert clean != ""


class TestRecordAppliesRecovery:
    @pytest.fixture
    def project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "proj"
        root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(root))
        monkeypatch.chdir(root)
        return root

    def _stored(self, root: Path, did: str) -> dict:
        rows = [
            json.loads(x)
            for x in (root / ".codevira" / "decisions.jsonl").read_text().splitlines()
            if x.strip()
        ]
        return next(r for r in rows if r.get("id") == did)

    def test_malformed_body_lands_as_real_fields(self, project: Path) -> None:
        did = decisions_store.record(
            "Use bcrypt for passwords.</decision>\n"
            '<parameter name="file_path">auth.py</parameter>\n'
            '<parameter name="do_not_revert">true'
        )
        rec = self._stored(project, did)
        assert rec["decision"] == "Use bcrypt for passwords."
        assert rec["file_path"] == "auth.py"
        assert rec["do_not_revert"] is True, "an intended lock must not be lost"

    def test_explicit_arguments_win_over_recovered_ones(self, project: Path) -> None:
        did = decisions_store.record(
            'X.</decision>\n<parameter name="file_path">wrong.py',
            file_path="right.py",
        )
        assert self._stored(project, did)["file_path"] == "right.py"

    def test_well_formed_records_are_unaffected(self, project: Path) -> None:
        did = decisions_store.record(
            "A normal decision", file_path="a.py", tags=["x"], do_not_revert=True
        )
        rec = self._stored(project, did)
        assert rec["decision"] == "A normal decision"
        assert rec["file_path"] == "a.py"
        assert rec["tags"] == ["x"]
        assert rec["do_not_revert"] is True
