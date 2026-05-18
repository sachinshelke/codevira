"""
test_help_text_consistency.py — v2.1.2 hardening (Test C).

Lints CLI help-text descriptions against the module constants they
claim to document. Catches doc-drift bugs like the v2.1.2 calibrate
case where `--help` said clamp range "[0.20, 0.55]" but the actual
``_decision_embeddings.py`` constants were `[0.35, 0.80]`.

Pattern:
  - Build the top-level CLI argparse parser (without running anything)
  - Walk every subparser's `description`
  - For each `(parser_name, claim_pattern, source_module_constant)` rule,
    extract the claimed value(s) from the description and assert they
    match the module's actual constants.

Pure static analysis — no subprocess, no MCP server, no chromadb. Fast.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.integration


def _build_cli_parser():
    """Mirror mcp_server.cli.main()'s parser construction without
    actually executing the dispatch. We replicate the argparse build
    by importing the module and using a stand-in that captures the
    parser object.

    Easiest approach: just call the module-level argparse builder via
    parse_args(['--help']) inside a SystemExit guard. argparse exposes
    the parser by inspection through the `--help` formatter.

    A cleaner approach is to refactor cli.py to expose a build_parser()
    function. That refactor is too invasive for this hardening pass —
    we use the run-and-introspect path instead.
    """
    import sys
    import argparse

    # Find the parser by patching argparse.ArgumentParser to record the
    # first instance created during cli main(). This way we don't have
    # to refactor cli.py.
    captured = {"parser": None}
    real_init = argparse.ArgumentParser.__init__

    def _spy_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        if captured["parser"] is None and kwargs.get("add_help", True) is not False:
            # First top-level parser created — capture it.
            # argparse creates many sub-parsers; we want the FIRST one
            # which is the top-level codevira parser.
            captured["parser"] = self

    argparse.ArgumentParser.__init__ = _spy_init  # type: ignore[method-assign]
    try:
        # Run cli main with --help; it'll SystemExit after printing.
        import mcp_server.cli as cli_mod

        old_argv = sys.argv
        sys.argv = ["codevira", "--help"]
        try:
            cli_mod.main()
        except SystemExit:
            pass
        finally:
            sys.argv = old_argv
    finally:
        argparse.ArgumentParser.__init__ = real_init  # type: ignore[method-assign]

    if captured["parser"] is None:
        raise RuntimeError("could not capture CLI parser")
    return captured["parser"]


def _find_subparser(top_parser, name: str):
    """Walk the top-level parser, find the subparser with given name."""
    for action in top_parser._actions:  # noqa: SLF001
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            if name in action.choices:
                return action.choices[name]
    return None


class TestHelpTextConsistency:
    """Lint CLI help text against the module constants it claims to document.

    Each test corresponds to ONE help-text vs constant pair we've been
    bitten by. Add a new test method per bug-of-this-class found.
    """

    def test_calibrate_clamp_range_matches_constants(self):
        """v2.1.2 38447fe regression guard: `codevira calibrate --help`
        cited the WRONG clamp range for ~1 day before smoke-testing
        caught it. Lock it in: the help text MUST cite the actual
        constants from _decision_embeddings.py.
        """
        from mcp_server.tools._decision_embeddings import (
            _THRESHOLD_MIN,
            _THRESHOLD_MAX,
        )

        parser = _build_cli_parser()
        calibrate = _find_subparser(parser, "calibrate")
        assert calibrate is not None, "calibrate subparser not registered"

        description = calibrate.description or ""
        # Extract any [<float>, <float>] pattern. The help text format
        # we ship today is "Clamped to [0.35, 0.80] for safety."
        ranges = re.findall(r"\[(\d+\.\d+),\s*(\d+\.\d+)\]", description)
        assert ranges, (
            f"calibrate help text doesn't mention a clamp range. "
            f"description = {description!r}"
        )

        actual_min = float(ranges[0][0])
        actual_max = float(ranges[0][1])
        assert actual_min == _THRESHOLD_MIN, (
            f"calibrate help cites min={actual_min} but code constant "
            f"_THRESHOLD_MIN = {_THRESHOLD_MIN}. Doc drift — update one "
            f"or the other so they agree."
        )
        assert actual_max == _THRESHOLD_MAX, (
            f"calibrate help cites max={actual_max} but code constant "
            f"_THRESHOLD_MAX = {_THRESHOLD_MAX}. Doc drift — update one "
            f"or the other so they agree."
        )

    def test_calibrate_auto_recalibrate_cadence_matches_constant(self):
        """Same family: the help text says 'every 10 decisions added in
        the background' but the actual cadence is
        ``_CALIBRATION_AUTO_EVERY_N``. If someone changes the constant
        we want the help text to drift-detect.
        """
        from mcp_server.tools._decision_embeddings import (
            _CALIBRATION_AUTO_EVERY_N,
        )

        parser = _build_cli_parser()
        calibrate = _find_subparser(parser, "calibrate")
        description = calibrate.description or ""
        # Look for "every N decisions" pattern
        m = re.search(r"every (\d+) decisions", description)
        assert m, (
            f"calibrate help doesn't mention recalibration cadence. "
            f"description = {description!r}"
        )
        claimed = int(m.group(1))
        assert claimed == _CALIBRATION_AUTO_EVERY_N, (
            f"calibrate help cites every {claimed} decisions, but the "
            f"code constant _CALIBRATION_AUTO_EVERY_N is "
            f"{_CALIBRATION_AUTO_EVERY_N}. Doc drift."
        )

    def test_version_consistency_pyproject_vs_module(self):
        """pyproject.toml::version and mcp_server.__version__ must agree.
        Cheap to enforce; catches version-bump misses.
        """
        from pathlib import Path
        import tomllib
        import mcp_server

        repo_root = Path(__file__).resolve().parents[2]
        with open(repo_root / "pyproject.toml", "rb") as f:
            pyproject = tomllib.load(f)
        pyproject_version = pyproject["project"]["version"]
        module_version = mcp_server.__version__
        assert pyproject_version == module_version, (
            f"Version drift: pyproject.toml = {pyproject_version!r}, "
            f"mcp_server.__version__ = {module_version!r}. "
            f"Update both in lockstep when bumping."
        )

    def test_changelog_unreleased_or_matches_current_version(self):
        """The CHANGELOG must have an entry for the current __version__
        (or Unreleased). Catches the "bumped version but forgot to add
        changelog notes" case.
        """
        from pathlib import Path
        import mcp_server

        repo_root = Path(__file__).resolve().parents[2]
        text = (repo_root / "CHANGELOG.md").read_text()
        version = mcp_server.__version__
        # Accept either "## [<version>]" or "## [Unreleased]" headers.
        if f"## [{version}]" not in text and "## [Unreleased]" not in text:
            pytest.fail(
                f"CHANGELOG.md has no section for version {version!r} "
                f"nor [Unreleased]. Add one or revert the version bump."
            )
