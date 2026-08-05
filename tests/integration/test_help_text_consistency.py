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

    @pytest.mark.skip(
        reason="v2.2.0: calibrate command and _decision_embeddings.py removed entirely"
    )
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

    @pytest.mark.skip(
        reason="v2.2.0: calibrate command and _decision_embeddings.py removed entirely"
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


class TestDestructiveCommandsAreDocumentedHonestly:
    """A destructive command's README line must not read as a safe one.

    Until v4.0 the command table said:

        | `codevira clean` / `reset` | Remove orphaned data / ... |

    `clean` is the full uninstaller — it wipes ~/.codevira/ including
    snapshots, strips codevira from every IDE config, and removes the
    launchd service. On 2026-08-01 an agent read that description, ran
    `yes | codevira clean` to tidy some stale registry rows, and
    destroyed a real installation. The name misled, and the docs
    confirmed the misreading rather than correcting it.

    These tests guard the description, not just the presence of a row.
    """

    def _readme(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[2] / "README.md").read_text()

    def test_prune_is_documented(self):
        """The safe operation needs a name users can find."""
        assert "`codevira prune`" in self._readme(), (
            "codevira prune ships but is absent from the README command "
            "table — leaving `clean` as the only discoverable tidy-up, "
            "which is what caused the incident."
        )

    def test_clean_is_not_described_as_a_tidy_up(self):
        """The exact wording that misled, as a regression test."""
        readme = self._readme()
        for line in readme.splitlines():
            if "`codevira clean`" not in line:
                continue
            low = line.lower()
            assert "orphan" not in low, (
                "README describes `clean` as removing orphaned data. That is "
                f"`prune`. Line:\n  {line.strip()}"
            )
            assert "uninstall" in low or "deprecated" in low, (
                "the `clean` row must say it uninstalls or is deprecated:\n"
                f"  {line.strip()}"
            )

    def test_uninstall_names_what_it_removes(self):
        """ "Reverses every system write" is true but not concrete enough
        to stop someone running it to free disk space."""
        readme = self._readme()
        row = next(
            (ln for ln in readme.splitlines() if "`codevira uninstall`" in ln), ""
        )
        assert row, "no README row for `codevira uninstall`"
        assert "snapshot" in row.lower(), (
            "the uninstall row should name snapshots — they are the one "
            "loss that cannot be recovered from the repo:\n  " + row.strip()
        )


class TestToolCountClaimsMatchReality:
    """ "52 → 37" is a headline claim in three docs. Check it against the
    server rather than against itself.

    The number is genuinely two numbers: 37 tools are defined, 36 are
    advertised, because `refresh_graph` is hidden from `tools/list` while
    staying callable. README and MIGRATING.md said "37 defined, 36
    advertised"; the CHANGELOG's Breaking entry said only "52 → 37",
    which is the first thing a user reads and one more than the 36 an
    agent counts in its own tool list.
    """

    ADVERTISED = 36

    def test_server_advertises_the_documented_count(self):
        import asyncio

        import mcp_server.server as server

        tools = asyncio.run(server.list_tools())
        assert len(tools) == self.ADVERTISED, (
            f"server advertises {len(tools)} tools, docs claim "
            f"{self.ADVERTISED}. Update the docs and this constant together."
        )

    @pytest.mark.parametrize("doc", ["README.md", "MIGRATING.md", "CHANGELOG.md"])
    def test_docs_carry_both_numbers(self, doc: str):
        """A doc citing only "37" leaves the reader unable to reconcile it
        with the 36 they can see."""
        from pathlib import Path

        text = (Path(__file__).resolve().parents[2] / doc).read_text()

        # Anchor on the CLAIM, not on the first "52" in the file — these
        # docs are long and full of unrelated numbers. The claim is a 52
        # and a 37 within a sentence of each other.
        claims = [m for m in re.finditer(r"52[^\n]{0,80}?37", text, flags=re.DOTALL)]
        if not claims:
            pytest.skip(f"{doc} makes no 52→37 tool-count claim")

        for m in claims:
            window = text[m.start() : m.end() + 300]
            assert str(self.ADVERTISED) in window, (
                f"{doc} cites the 52→37 cut without the advertised count of "
                f"{self.ADVERTISED}; a reader counting their own tool list "
                f"sees a mismatch. Claim at offset {m.start()}:\n"
                f"  {window[:160].strip()}"
            )


class TestAllSubcommandHelpRenders:
    """Every subcommand's --help must render without raising.

    Regression guard (v3.7.0 dogfood): `codevira merge-driver --help` crashed
    with `ValueError: unsupported format character 'O'` because its help /
    description text contained git's literal `%O %A %B` placeholders — argparse
    %-formats help strings, so a bare `%` blows up format_help(). Any future
    subcommand that puts a raw `%` in help/description text is caught here.
    """

    def test_every_subparser_format_help_ok(self):
        parser = _build_cli_parser()
        # Locate the subparsers action and format_help() each choice.
        failures = []
        for action in parser._actions:  # noqa: SLF001
            choices = getattr(action, "choices", None)
            if not isinstance(choices, dict):
                continue
            for name, sub in choices.items():
                try:
                    sub.format_help()
                except Exception as e:  # noqa: BLE001
                    failures.append(f"{name}: {type(e).__name__}: {e}")
        assert not failures, "subcommand --help failed to render:\n" + "\n".join(
            failures
        )

    def test_merge_driver_help_renders(self):
        """Direct guard for the specific v3.7.0 crash."""
        parser = _build_cli_parser()
        md = _find_subparser(parser, "merge-driver")
        assert md is not None, "merge-driver subparser not registered"
        text = md.format_help()  # must not raise
        assert "base" in text and "theirs" in text
