"""G3 must not write into the machine's real codevira home.

`scripts/check_real_ide_smoke.sh` boots a real MCP stdio server against a
`mktemp -d` project to time the handshake. Booting a server auto-registers
the project (`mcp_server/auto_init.py` -> `GlobalDB.register_project`), and
there is no deregister counterpart anywhere in the tree — by design, since
a missing path is usually an unmounted volume, not a dead project.

The consequence was measured, not theorised: on 2026-08-01 an evening of
gauntlet runs left NINE `codevira-g3-XXXXXXXX` rows in the maintainer's
`~/.codevira/global.db`, each with a matching `~/.codevira/projects/<slug>/`
data dir, because the script's `trap rm -rf` removed the temp project but
nothing removed its registration.

Containment beats cleanup here: pointing the subprocess at a CODEVIRA_HOME
*inside* the temp project means a killed or crashed run leaves nothing
either, which an after-the-fact deregister could not promise.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_real_ide_smoke.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("codevira") is None or not SCRIPT.exists(),
    reason="G3 drives the installed `codevira` binary and its own script; "
    "the sdist ships tests/ but not scripts/, so both must be checked",
)


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    """A HOME with exactly one IDE config, so G3 gets past its detect gate.

    With zero configs detected the script exits 2 ("nothing to smoke")
    before reaching the handshake — the very code path under test.
    """
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "codevira": {
                        "command": "codevira",
                        "env": {"CODEVIRA_IDE": "claude_code"},
                    }
                }
            }
        )
    )
    return home


def _run_g3(home: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    env.pop("CODEVIRA_HOME", None)  # the point is that the SCRIPT sets it
    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    # Guard against the vacuous pass. "No rows leaked" is also true when
    # the script never ran — and it would not run in an sdist-only
    # checkout, since the sdist packages tests/ but not scripts/. Without
    # this, a missing or broken G3 makes these tests GREENER, which is the
    # exact failure shape this file exists to catch.
    if "tools/list" not in proc.stdout + proc.stderr:
        raise AssertionError(
            "G3 never reached the handshake, so the leak assertions below "
            f"would pass for the wrong reason.\nrc={proc.returncode}\n"
            f"{(proc.stdout + proc.stderr)[-2000:]}"
        )
    return proc


def _project_rows(db: Path) -> list[str]:
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT path FROM projects")]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


class TestG3LeavesNoResidue:
    def test_g3_does_not_register_its_temp_project(self, fake_home: Path) -> None:
        """The load-bearing test: run G3, assert the home gained no rows.

        Fails before the fix with one `codevira-g3-XXXXXXXX` row.
        """
        _run_g3(fake_home)
        rows = _project_rows(fake_home / ".codevira" / "global.db")
        leaked = [p for p in rows if "codevira-g3-" in p]
        assert not leaked, f"G3 registered its throwaway project: {leaked}"

    def test_g3_leaves_no_project_data_dirs(self, fake_home: Path) -> None:
        """A row is only half the residue — the data dir is the other half."""
        _run_g3(fake_home)
        projects = fake_home / ".codevira" / "projects"
        strays = (
            [p.name for p in projects.iterdir() if "codevira-g3-" in p.name]
            if projects.is_dir()
            else []
        )
        assert not strays, f"G3 left data dirs behind: {strays}"


class TestTheGateStillWorks:
    def test_isolation_did_not_silently_disable_the_handshake(
        self, fake_home: Path
    ) -> None:
        """Containment must not turn G3 into a no-op that always passes.

        If CODEVIRA_HOME isolation broke the boot, the handshake block would
        stop reporting timings — and a gate that measures nothing is worse
        than the leak it was fixing.
        """
        r = _run_g3(fake_home)
        blob = r.stdout + r.stderr
        assert "tools/list" in blob, blob[-2000:]
