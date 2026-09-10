"""
test_docs_subcommands_exist.py — doc-drift lint (G1.8).

Every ``codevira <subcommand>`` that a LIVE doc tells a reader to RUN must
be a subcommand the CLI actually dispatches.

Why this exists — this class of rot has now bitten three times:

1. **The G4 release gate (fixed 2026-09-10, 0bfe977).** The crash-log gate
   ran ``codevira report 2>/dev/null | grep -c CRASH``. ``report`` was cut
   in v2.2.0, so it exited 2 with a usage error, ``2>/dev/null`` swallowed
   it, ``grep -c`` counted an empty stream, and the count was always 0.
   The gate could not fail. It certified "no crashes" for 4.1.0 while the
   log held three CRASH entries.

2. **``docs/alpha-tester-invites.md``.** The same dead ``codevira report``
   was still being handed to alpha testers, alongside a second dead
   command, ``codevira insights``, that nobody had noticed.

3. **``MIGRATING.md`` / ``DOGFOOD.md``.** Found by extending this lint to
   the root docs: a live "run this" fence in the v2.1.x migration path
   invoked ``codevira archive-legacy``, and the dogfood guide's week-end
   wrap-up invoked ``codevira budget`` / ``codevira insights``.

Sibling of ``test_help_text_consistency.py`` (G1.6), which lints help-text
*descriptions* against module constants. This lints doc *commands* against
the dispatch table.

Source of truth is the argparse subparser choices — not ``--help`` output.
Choices are what ``main()`` actually dispatches on, and they include
subcommands hidden from help (``reconcile``, ``merge-driver-agents``).

Pure static analysis — no subprocess, no MCP server, no chromadb. Fast.


Prose vs instruction
--------------------
Naming a dead command is not the same as telling someone to run one. A
doc legitimately names removed commands when it documents their removal
("| `codevira clean` | **Removed in 4.0.1** ..."), and a version-transition
doc does it constantly — MIGRATING.md's rollback recipe says to run
``codevira register`` because that recipe puts you back on 1.8.0, where
``register`` is the correct command. Linting that against TODAY's
dispatch table would demand breaking working instructions.

So a mention is treated as prose, not an instruction, when any of:

* the line itself declares a removal (``_REMOVAL_MARKER``);
* the enclosing section's HEADING declares one ("### Breaking: `codevira
  clean` is gone", "### `codevira agents` no longer creates ...");
* the section carries an explicit ``<!-- codevira-lint: historical -->``
  marker, for version-transition sections whose headings say nothing
  about removal ("### Activated (new in 2.0)", "## Rollback to 1.8.0").

The explicit marker is deliberately visible and greppable rather than
inferred, and it lives in the doc rather than in a list inside this test:
the doc is where a human editing that section will see it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from integration.test_help_text_consistency import _build_cli_parser

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------
# Docs excluded from the lint, with the reason. These are point-in-time
# records — planning docs, execution logs, dated audits, shipped release
# notes and design specs. They describe the CLI as it was on the day they
# were written; rewriting them to match today's CLI would falsify the
# record. They are not instructions to a live user.
#
# Anything NOT listed here is linted by default, so a newly added doc is
# covered without touching this file.
_HISTORICAL_PREFIXES = (
    "docs/plans/",  # planning docs, per-version
    "docs/release-notes/",  # shipped notes, frozen at release
    "docs/heroes/",  # design specs (sprint-week feature designs)
    "docs/internal/",  # internal working notes
    "docs/v2-",  # v2-master-plan / -completion-plan / -execution-log
    "docs/audit-",  # dated audits
    "docs/surface-cuts-",  # the 2026-05-22 cut audit that removed `report`
    "docs/morning-handoff-",  # dated handoff notes
)

# Root-level docs that carry run instructions. docs/**.md is swept
# automatically; these sit at the repo root, so they are named.
_ROOT_DOCS = (
    "README.md",
    "FAQ.md",
    "MIGRATING.md",
    "CONTRIBUTING.md",
    "DOGFOOD.md",
)

# A line — or a section heading — that DOCUMENTS a command's removal is
# not an instruction to run it. e.g. README's "| `codevira clean` |
# **Removed in 4.0.1** ... It now errors `invalid choice`. Use `prune` |",
# or MIGRATING's "### Breaking: `codevira clean` is gone".
#
# Self-maintaining on purpose: documenting a future removal the same way
# passes with no edit here, and none of these phrases appear in a line
# that tells someone to run something.
_REMOVAL_MARKER = re.compile(
    r"\bremoved\b|\bremoval\b|\bdeleted\b|\bno longer\b|\binvalid choice\b"
    r"|\bnot a (?:valid )?subcommand\b|\bdoes not exist\b|\brenamed to\b"
    r"|\breplaced by\b|\bsuperseded by\b|\bdeprecated\b|\bis gone\b"
    r"|\bare gone\b|\bbreaking\b",
    re.IGNORECASE,
)

# Explicit opt-out for a version-transition section whose heading says
# nothing about removal — "### Activated (new in 2.0)", "## Rollback to
# 1.8.0 if needed". Applies from the marker to the next heading.
_HISTORICAL_SECTION = re.compile(r"<!--\s*codevira-lint:\s*historical\s*-->", re.I)

# A markdown heading, outside a fence. Bash comments inside a fence also
# start with `#`, which is why fence state is tracked first.
_HEADING = re.compile(r"^#{1,6}\s")

# `codevira foo` / `$ codevira foo` inside inline backticks.
_INLINE = re.compile(r"`\$?\s*codevira ([a-z][a-z0-9-]*)")
# A copy-pasteable command line inside a fenced block.
_FENCED = re.compile(r"^\s*\$?\s*codevira ([a-z][a-z0-9-]*)")


def _real_subcommands() -> set[str]:
    """The subcommands ``codevira`` actually dispatches."""
    parser = _build_cli_parser()
    for action in parser._actions:  # noqa: SLF001
        if hasattr(action, "choices") and isinstance(action.choices, dict):
            return set(action.choices)
    raise RuntimeError("could not find the subcommand action on the CLI parser")


def _live_docs() -> list[Path]:
    """Docs that instruct a live user, relative to the repo root."""
    docs = [
        p
        for p in sorted((REPO_ROOT / "docs").rglob("*.md"))
        if not str(p.relative_to(REPO_ROOT)).startswith(_HISTORICAL_PREFIXES)
    ]
    return docs + [REPO_ROOT / name for name in _ROOT_DOCS]


def _scan(text: str) -> list[tuple[int, str]]:
    """Return ``(line_number, subcommand)`` for every RUN instruction.

    Counts inline-backtick commands and command lines inside fences.
    Skips anything the module docstring classifies as prose: a line that
    declares a removal, a section whose heading declares one, and a
    section carrying an explicit historical marker.
    """
    found: list[tuple[int, str]] = []
    in_fence = False
    section_is_prose = False

    for lineno, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue

        if not in_fence:
            if _HEADING.match(line):
                # A new section resets prose mode; the heading itself may
                # re-enter it by declaring a removal.
                section_is_prose = bool(_REMOVAL_MARKER.search(line))
                continue
            if _HISTORICAL_SECTION.search(line):
                section_is_prose = True
                continue

        if section_is_prose or _REMOVAL_MARKER.search(line):
            continue

        names = [m.group(1) for m in _INLINE.finditer(line)]
        if in_fence:
            names += [m.group(1) for m in _FENCED.finditer(line)]
        found.extend((lineno, n) for n in names)

    return found


class TestDocsSubcommandsExist:
    def test_every_documented_subcommand_is_real(self):
        """The lint itself.

        Fails with the exact ``file:line`` of every doc that tells a reader
        to run a command the CLI would reject with ``invalid choice``.
        """
        real = _real_subcommands()
        violations: list[str] = []

        for path in _live_docs():
            rel = path.relative_to(REPO_ROOT)
            for lineno, name in _scan(path.read_text()):
                if name not in real:
                    violations.append(f"  {rel}:{lineno} — `codevira {name}`")

        assert not violations, (
            "Doc(s) instruct a reader to run a subcommand that does not "
            "exist. `codevira <name>` would exit 2 with `invalid choice`:\n"
            + "\n".join(violations)
            + "\n\nFix the doc to name a real subcommand. If the line is "
            "PROSE about a removed command, say so on that line (e.g. "
            '"Removed in 4.0.1"). If a whole section documents an older '
            "version — a migration table, a rollback recipe — put\n"
            "    <!-- codevira-lint: historical -->\n"
            "under its heading.\nReal subcommands: " + ", ".join(sorted(real))
        )

    # -----------------------------------------------------------------
    # Vacuity guards.
    #
    # The bug that motivated this file was a gate that COULD NOT FAIL.
    # A lint that scans nothing, or a detector that matches nothing,
    # passes just as green as a clean tree. These assert it can fail.
    # -----------------------------------------------------------------

    def test_lint_actually_scans_docs(self):
        """A shrinking doc set must not silently empty the lint."""
        docs = _live_docs()
        assert len(docs) >= 10, (
            f"only {len(docs)} live docs found — lint is near-vacuous"
        )

        total = sum(len(_scan(p.read_text())) for p in docs)
        assert total >= 20, (
            f"only {total} `codevira <cmd>` instructions found across "
            f"{len(docs)} docs — the detector is matching almost nothing"
        )

    def test_root_docs_are_actually_covered(self):
        """Every named root doc must exist and be scanned.

        A rename would otherwise drop it from the lint silently — the
        list is by name, so a missing file is a hole, not a pass.
        """
        for name in _ROOT_DOCS:
            path = REPO_ROOT / name
            assert path.exists(), f"{name} is in _ROOT_DOCS but does not exist"
            assert path in _live_docs(), f"{name} is not being scanned"

    def test_detector_catches_a_planted_dead_command(self, tmp_path: Path):
        """The detector must fire on a command that isn't real."""
        doc = tmp_path / "planted.md"
        doc.write_text(
            "Collect crash logs with `codevira report`.\n\n"
            "```bash\ncodevira insights\n```\n"
        )
        real = _real_subcommands()
        dead = [n for _, n in _scan(doc.read_text()) if n not in real]
        assert dead == ["report", "insights"], (
            f"detector missed a planted dead command; found {dead}"
        )

    def test_detector_accepts_a_real_command(self, tmp_path: Path):
        """...and must not fire on a real one (no false positives)."""
        doc = tmp_path / "ok.md"
        doc.write_text("Run `codevira doctor`.\n\n```bash\n$ codevira status\n```\n")
        real = _real_subcommands()
        assert [n for _, n in _scan(doc.read_text()) if n not in real] == []

    def test_removal_prose_is_not_flagged(self, tmp_path: Path):
        """A line documenting a removal is prose, not an instruction."""
        doc = tmp_path / "removed.md"
        doc.write_text(
            "| `codevira clean` | **Removed in 4.0.1** — it now errors "
            "`invalid choice`. Use `prune` |\n"
        )
        assert _scan(doc.read_text()) == []

    def test_removal_heading_covers_its_whole_section(self, tmp_path: Path):
        """A heading that declares a removal puts its section in prose mode.

        Models MIGRATING.md's "### Breaking: `codevira clean` is gone",
        whose migration table names the dead command in every left cell.
        """
        doc = tmp_path / "section.md"
        doc.write_text(
            "### Breaking: `codevira clean` is gone\n\n"
            "| You used to run | Run instead |\n"
            "|---|---|\n"
            "| `codevira clean --ghosts` | `codevira prune --ghosts` |\n\n"
            "### Something else\n\n"
            "Run `codevira budget`.\n"
        )
        real = _real_subcommands()
        dead = [(ln, n) for ln, n in _scan(doc.read_text()) if n not in real]
        assert [n for _, n in dead] == ["budget"], (
            f"section scoping wrong — expected only the post-section "
            f"`budget` to survive, got {dead}"
        )

    def test_explicit_historical_marker_covers_its_section(self, tmp_path: Path):
        """`<!-- codevira-lint: historical -->` exempts to the next heading.

        Models MIGRATING.md's "## Rollback to 1.8.0 if needed", where
        `codevira register` is CORRECT advice — that recipe puts you back
        on 1.8.0, where `register` is the command that exists.
        """
        doc = tmp_path / "hist.md"
        doc.write_text(
            "## Rollback to 1.8.0 if needed\n"
            "<!-- codevira-lint: historical -->\n\n"
            "```bash\ncodevira register\n```\n\n"
            "## Today\n\n"
            "```bash\ncodevira register\n```\n"
        )
        dead = [ln for ln, n in _scan(doc.read_text()) if n == "register"]
        assert dead == [11], (
            f"marker must exempt only its own section; flagged lines {dead}"
        )

    def test_report_specifically_is_not_a_subcommand(self):
        """Regression guard for the 2026-09-10 G4 escape.

        `report` was cut in v2.2.0. If it is ever re-added, this test
        fails — a deliberate prompt to re-read the G4 gate (which now
        reads the crash log directly) and the docs that route users to
        `codevira doctor` instead.
        """
        assert "report" not in _real_subcommands()
