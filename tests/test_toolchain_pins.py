"""The lint/format toolchain is pinned in two files that must agree.

This repo carries NO ``[tool.ruff]`` config, so ruff's DEFAULTS are the
project's format and lint policy. A different ruff version is therefore a
different policy — not a cosmetic detail.

The two declarations drifted: ``pyproject.toml`` allowed a range
(``ruff>=0.6.0,<0.16``) while ``.pre-commit-config.yaml`` pinned ``v0.6.9``
exactly. pip resolved the range to the newest (0.15.14), so a local
``ruff format`` rewrote 108 files that the commit hook immediately rewrote
back — and every fresh dev environment reproduced it, because the range let
the resolver, not the project, choose the policy.

Two files that must match by hand will eventually not. This asserts it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _pyproject_ruff_pin() -> str:
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]["dev"]
    pins = [d for d in deps if re.match(r"^ruff\b", d)]
    assert len(pins) == 1, f"expected exactly one ruff dep, got {pins}"
    return pins[0]


def _precommit_ruff_rev() -> str:
    text = (REPO / ".pre-commit-config.yaml").read_text()
    m = re.search(
        r"repo:\s*https://github\.com/astral-sh/ruff-pre-commit\s*\n(?:\s*#.*\n)*\s*rev:\s*(\S+)",
        text,
    )
    assert m, "could not find the ruff-pre-commit rev"
    return m.group(1)


class TestRuffIsPinnedIdentically:
    def test_pyproject_pins_an_exact_version(self) -> None:
        """A range is the defect. ``>=`` lets the resolver pick the policy, and
        it will pick a different one than the hook's fixed rev."""
        pin = _pyproject_ruff_pin()
        assert "==" in pin, (
            f"ruff must be pinned exactly, got {pin!r}. A range lets pip choose "
            "a different formatter than the pre-commit hook runs."
        )

    def test_both_files_name_the_same_version(self) -> None:
        pin = _pyproject_ruff_pin().split("==")[1].strip()
        rev = _precommit_ruff_rev().lstrip("v")
        assert pin == rev, (
            f"ruff version drift: pyproject.toml pins {pin}, "
            f".pre-commit-config.yaml runs {rev}. With no [tool.ruff] config "
            "these are two different format policies — `ruff format` locally "
            "and the commit hook will fight over every file. Move both in the "
            "same commit."
        )

    @pytest.mark.skipif(
        not (REPO / ".venv").exists(), reason="no in-tree venv to check"
    )
    def test_the_installed_ruff_matches_too(self) -> None:
        """The pins can agree while the venv holds something else entirely —
        which is how this was discovered, mid-release."""
        import subprocess

        exe = REPO / ".venv" / "bin" / "python"
        if not exe.exists():
            pytest.skip("no venv python")
        out = subprocess.run(
            [str(exe), "-m", "ruff", "--version"], capture_output=True, text=True
        )
        if out.returncode != 0:
            pytest.skip("ruff not installed in the venv")
        installed = out.stdout.strip().split()[-1]
        want = _pyproject_ruff_pin().split("==")[1].strip()
        assert installed == want, (
            f"the venv has ruff {installed} but the project pins {want}. "
            "Re-install the dev extra: pip install -e '.[dev]'"
        )
