# Codevira Makefile — discipline scaffold reference implementation.
#
# Targets are the operational equivalents of release_gates in
# codevira.discipline.yaml. Anyone (human, AI agent, CI) wanting to
# release MUST run `make release-gauntlet` and produce evidence.
#
# Bypass is possible at the shell level (run twine directly) but the
# .claude/hooks/pre-release-block.sh hook blocks that path inside any
# Claude Code session.
#
# The Makefile is intentionally simple — every command is one shell
# call. No magic, no recursion. Easy to read, easy to override.

.PHONY: help install dev test test-unit test-unit-random test-e2e lint format type-check \
        release-gauntlet release-evidence release-verify-version \
        release-build release-dry-run release-publish release-smoke \
        release-full ci clean

# Default target: print help.
help:
	@echo "Codevira — discipline scaffold targets"
	@echo ""
	@echo "  Setup:"
	@echo "    install              Install codevira (production deps only)"
	@echo "    dev                  Install dev dependencies + pre-commit hooks"
	@echo ""
	@echo "  Quality (run on every PR):"
	@echo "    test                 Alias for test-unit"
	@echo "    test-unit            Run pytest unit tests (excludes e2e)"
	@echo "    test-unit-random     Same suite in a shuffled order (catches state leaks)"
	@echo "    test-e2e             Run end-to-end first-contact suite (G2)"
	@echo "    lint                 Run ruff lint"
	@echo "    format               Run ruff format"
	@echo "    type-check           Run mypy on mcp_server + indexer"
	@echo ""
	@echo "  Release (gates from codevira.discipline.yaml):"
	@echo "    release-verify-version  Cross-check version: pyproject.toml ↔ __init__.py ↔ CHANGELOG.md"
	@echo "    release-gauntlet        Run G1-G4. Produces .release-evidence/<ver>.json"
	@echo "    release-evidence        Show current release evidence file"
	@echo "    release-build           Clean build → dist/ (wheel + sdist)"
	@echo "    release-dry-run         twine check dist/* (validates metadata)"
	@echo "    release-publish         Upload to PyPI (blocked by hook without G1-G5)"
	@echo "    release-smoke           Post-release: pipx install from PyPI, verify works"
	@echo "    release-full            Full sequence: verify-version → gauntlet → build → dry-run"
	@echo ""
	@echo "  CI:"
	@echo "    ci                   Run everything CI runs (lint + test-unit + test-e2e)"
	@echo "    clean                Remove build artifacts and caches"

# ─── Setup ─────────────────────────────────────────────────────────────────

install:
	$(PYTHON) -m pip install -e .

dev:
	$(PYTHON) -m pip install -e ".[dev]"
	$(PYTHON) -m pre_commit install || true   # pre-commit hook is optional in CI/non-git dirs

# ─── Quality (per-PR) ──────────────────────────────────────────────────────
#
# 2026-05-17 hardening: every quality target invokes tools via `$(PYTHON) -m`
# so the same interpreter that installed the deps runs them. Without this,
# bare `pytest` / `ruff` / `mypy` resolve to whichever copy is first on
# PATH — which on macOS is often system Python 3.9 that doesn't have the
# project deps. Caught by the very first gauntlet trial.

test: test-unit

test-unit:
	$(PYTHON) -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/integration

# Same suite, shuffled. Collection order is ONE order out of many, and a test
# that leaks process-global state only fails in the orders where its victim
# runs after it — so a suite gated exclusively on collection order cannot see
# that class of bug at all. Three such leaks (mutated `rich` modules, a
# memoised crash logger, a latched `_is_http_transport`) sat green in CI until
# 2026-08-02, one of them making two binding tests pass *vacuously*.
#
# Deliberately a SEPARATE target, not a flag on `test-unit`: the fast local
# loop should stay reproducible. pytest-randomly prints the seed it chose;
# reproduce any failure with `--randomly-seed=<n>`.
test-unit-random:
	$(PYTHON) -m pytest tests/ -q --ignore=tests/e2e --ignore=tests/integration -p randomly

test-e2e:
	$(PYTHON) -m pytest tests/e2e/test_first_contact.py tests/e2e/test_product_invariants.py tests/e2e/test_cross_tool_universality.py tests/e2e/test_v350_acceptance.py -v

# v2.1.2 hardening — integration suite (slower; runs in gauntlet):
#   MCP round-trip, help-text linter, sandboxed-parent. Skipped from
#   `make test-unit` so the fast loop stays fast.
test-integration:
	PYTHONPATH=. $(PYTHON) -m pytest tests/integration/ -v --timeout=60

# v2.1.2 hardening — cold-install smoke (builds wheel + fresh venv +
# every subcommand --help). Runs in gauntlet too.
smoke-install:
	bash scripts/cold_install_smoke.sh

lint:
	$(PYTHON) -m ruff check mcp_server indexer

format:
	$(PYTHON) -m ruff format mcp_server indexer

type-check:
	$(PYTHON) -m mypy mcp_server indexer --ignore-missing-imports --no-strict-optional

# ─── Release gauntlet (the bypass-proof gate) ──────────────────────────────
#
# Why this matters: v2.0.0 shipped to PyPI with bugs A–O still live
# because we trusted "all unit tests pass" as a release signal. That
# was wrong. The gauntlet runs G1-G4 and writes evidence so the
# PreToolUse hook can verify before letting `twine upload` execute.
#
# G5 is human-only — the maintainer must add `"G5_confirmed": true`
# to the evidence file by hand after running on a real machine.

# Prefer python3 (universally available on macOS+Linux); fall back to python.
# Without this, `python` is missing on stock macOS and VERSION silently becomes
# empty, silently breaking every release target. Bug found during the gauntlet
# verification on 2026-05-17.
# Prefer the project venv if present — running `make test-unit` /
# `make release-gauntlet` without activating .venv otherwise falls back to
# system python3, which lacks the project deps and produces dozens of
# spurious "failures" (missing tree-sitter grammars, etc.). Found 2026-05-27
# when the gauntlet went red purely from the interpreter, not the tests.
# Override with `make PYTHON=... <target>` as before (?= keeps env precedence).
PYTHON ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || command -v python3 2>/dev/null || command -v python 2>/dev/null)
VERSION := $(shell $(PYTHON) -c "import re; print(re.search(r'version\s*=\s*\"([^\"]+)\"', open('pyproject.toml').read()).group(1))")
EVIDENCE_FILE := .release-evidence/$(VERSION).json

# 2026-05-19 v2.1.2 hardening: G1.5 + G1.6 + G1.7 added to catch the
# classes of bug that slipped through v2.1.2 (bulk_import placeholder
# skip, calibrate doc drift, Antigravity dlopen). See
# tests/integration/ + scripts/cold_install_smoke.sh.
release-gauntlet:
	@mkdir -p .release-evidence
	@echo "Running release gauntlet for v$(VERSION) ..."
	@echo ""
	@echo "▸ G1 — Unit tests"
	@$(MAKE) test-unit && echo "  ✓ G1 passed" || (echo "  ✗ G1 FAILED — release blocked"; exit 1)
	@echo ""
	@echo "▸ G1.5 — MCP round-trip integration (tests/integration/test_mcp_roundtrip.py)"
	@PYTHONPATH=. $(PYTHON) -m pytest tests/integration/test_mcp_roundtrip.py -q --timeout=60 \
		&& echo "  ✓ G1.5 passed" \
		|| (echo "  ✗ G1.5 FAILED — release blocked (catches Item-23/27/29/etc. drift)"; exit 1)
	@echo ""
	@echo "▸ G1.6 — Help-text vs constants linter (tests/integration/test_help_text_consistency.py)"
	@PYTHONPATH=. $(PYTHON) -m pytest tests/integration/test_help_text_consistency.py -q --timeout=30 \
		&& echo "  ✓ G1.6 passed" \
		|| (echo "  ✗ G1.6 FAILED — release blocked (doc-drift like calibrate clamp range)"; exit 1)
	@echo ""
	@echo "▸ G1.7 — Sandboxed-parent MCP test (tests/integration/test_sandboxed_parent.py)"
# --timeout must stay ABOVE the in-test per-spawn budget (_SPAWN_TIMEOUT_S +
# _COLD_START_GRACE_S = 60s for the first spawn). At the old --timeout=60 the
# two collided, so pytest could kill the test at the same moment the spawn
# budget expired — and pytest's timeout produces a bare traceback instead of
# the spawn's phase timeline, which is the whole point of the instrumentation.
# Costs nothing when healthy: the file runs in ~8s. See the block comment at
# the top of test_sandboxed_parent.py.
	@PYTHONPATH=. $(PYTHON) -m pytest tests/integration/test_sandboxed_parent.py -q --timeout=120 \
		&& echo "  ✓ G1.7 passed" \
		|| (echo "  ✗ G1.7 FAILED — release blocked (Antigravity-class regression — issue #10)"; exit 1)
	@echo ""
	@echo "▸ G1.8 — Docs cite real subcommands (tests/integration/test_docs_subcommands_exist.py)"
	@PYTHONPATH=. $(PYTHON) -m pytest tests/integration/test_docs_subcommands_exist.py -q --timeout=30 \
		&& echo "  ✓ G1.8 passed" \
		|| (echo "  ✗ G1.8 FAILED — release blocked (a doc tells users to run a dead subcommand — the `codevira report` class)"; exit 1)
	@echo ""
	@echo "▸ G2 — First-contact e2e"
	@$(MAKE) test-e2e && echo "  ✓ G2 passed" || (echo "  ✗ G2 FAILED — release blocked"; exit 1)
	@echo ""
	@echo "▸ G2.5 — Cold-install wheel smoke (scripts/cold_install_smoke.sh)"
	@bash scripts/cold_install_smoke.sh > /tmp/.g25_output 2>&1; \
		G25_EXIT=$$?; \
		if [ "$$G25_EXIT" = "0" ]; then \
			tail -10 /tmp/.g25_output; \
			echo "  ✓ G2.5 passed (wheel installs cleanly, all subcommands work)"; \
		else \
			echo "  ✗ G2.5 FAILED — release blocked (wheel packaging or subcommand regression)"; \
			cat /tmp/.g25_output; \
			rm -f /tmp/.g25_output; \
			exit 1; \
		fi; \
		rm -f /tmp/.g25_output
	@echo ""
	@echo "▸ G3 — Real-IDE smoke"
	@if [ -x scripts/check_real_ide_smoke.sh ]; then \
		scripts/check_real_ide_smoke.sh > /tmp/.g3_output 2>&1; \
		G3_EXIT=$$?; \
		cat /tmp/.g3_output; \
		case "$$G3_EXIT" in \
			0) echo "  ✓ G3 passed" && echo true > .release-evidence/.g3.tmp ;; \
			2) echo "  ✗ G3 skipped (stub returned exit 2) — PUBLISH WILL BE BLOCKED" && echo '"skipped"' > .release-evidence/.g3.tmp ;; \
			*) echo "  ✗ G3 FAILED (exit $$G3_EXIT) — release blocked"; rm -f /tmp/.g3_output; exit 1 ;; \
		esac; \
		rm -f /tmp/.g3_output; \
	else \
		echo "  ✗ scripts/check_real_ide_smoke.sh missing — G3 skipped, PUBLISH WILL BE BLOCKED"; \
		echo '"skipped"' > .release-evidence/.g3.tmp; \
	fi
	@echo ""
	@echo "▸ G4 — Crash log clean"
	@# Reads the log FILE, not a CLI subcommand.
	@#
	@# This gate was dead. It ran `codevira report | grep -c CRASH`, and
	@# `report` is not a codevira subcommand — it exits 2 with a usage error,
	@# `2>/dev/null` swallowed that, grep counted an empty stream, and the count
	@# was always 0. G4 could not fail. It reported "✓ no crashes" for 4.1.0
	@# while three CRASH entries sat in the log, and every G4_crash_log_clean
	@# in every past evidence file was therefore meaningless.
	@#
	@# A gate that depends on a CLI surface can be silently retired by a rename.
	@# The log path is the thing being asserted about, so read it directly, and
	@# distinguish "0 crashes" from "could not check" — a check that cannot run
	@# must say so rather than pass.
	@CRASH_LOG="$${CODEVIRA_HOME:-$$HOME/.codevira}/logs/crashes.log"; \
	if [ ! -e "$$CRASH_LOG" ]; then \
		echo "  ✓ G4 passed (no crash log at $$CRASH_LOG)"; \
		echo true > .release-evidence/.g4.tmp; \
	elif [ ! -r "$$CRASH_LOG" ]; then \
		echo "  ✗ G4 INDETERMINATE: $$CRASH_LOG exists but is not readable — PUBLISH WILL BE BLOCKED"; \
		echo '"skipped"' > .release-evidence/.g4.tmp; \
	else \
		CRASH_COUNT=$$(grep -c '^CRASH:' "$$CRASH_LOG" 2>/dev/null || echo 0); \
		if [ "$$CRASH_COUNT" -eq 0 ] 2>/dev/null; then \
			echo "  ✓ G4 passed (0 crashes in $$CRASH_LOG)"; \
			echo true > .release-evidence/.g4.tmp; \
		else \
			echo "  ⚠ G4 WARN: $$CRASH_COUNT crash(es) in $$CRASH_LOG"; \
			grep '^CRASH:' "$$CRASH_LOG" | sort | uniq -c | sort -rn | sed -n '1,3p' | sed 's/^/      /'; \
			echo "    Review, then archive rather than delete (keeps the audit):"; \
			echo "      mv '$$CRASH_LOG' '$$CRASH_LOG.pre-$(VERSION)'"; \
			echo '"warn"' > .release-evidence/.g4.tmp; \
		fi; \
	fi
	@echo ""
	@echo "Writing evidence to $(EVIDENCE_FILE) ..."
	@G3_RESULT=$$(cat .release-evidence/.g3.tmp); \
	G4_RESULT=$$(cat .release-evidence/.g4.tmp); \
	printf '{\n  "version": "%s",\n  "timestamp": "%s",\n  "G1_unit_tests": true,\n  "G1_5_mcp_roundtrip": true,\n  "G1_6_help_text_consistency": true,\n  "G1_7_sandboxed_parent": true,\n  "G1_8_docs_subcommands": true,\n  "G2_first_contact": true,\n  "G2_5_cold_install_smoke": true,\n  "G3_real_ide_smoke": %s,\n  "G4_crash_log_clean": %s,\n  "G5_human_confirmed": false,\n  "note": "G5 must be set true by hand after maintainer verification on a real machine."\n}\n' "$(VERSION)" "$$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$G3_RESULT" "$$G4_RESULT" > $(EVIDENCE_FILE)
	@rm -f .release-evidence/.g3.tmp .release-evidence/.g4.tmp
	@echo ""
	@echo "✓ Gauntlet complete for v$(VERSION)"
	@echo "  Evidence: $(EVIDENCE_FILE)"
	@echo ""
	@echo "  G5 (human confirmation) is REQUIRED before publishing:"
	@echo "    1. Run a fresh install on a real machine against your projects"
	@echo "    2. Verify behavior matches expectations"
	@echo "    3. Edit $(EVIDENCE_FILE) and set \"G5_human_confirmed\": true"
	@echo "    4. Then run: twine upload dist/*"
	@echo ""
	@echo "  Without G5_human_confirmed=true, the PreToolUse hook will block twine."

release-evidence:
	@if [ -f $(EVIDENCE_FILE) ]; then \
		echo "Evidence for v$(VERSION):"; \
		cat $(EVIDENCE_FILE); \
	else \
		echo "No evidence file yet for v$(VERSION). Run: make release-gauntlet"; \
		exit 1; \
	fi

# ─── Foolproof release: version coherence + git state ──────────────────────
#
# Why this exists: v2.0.0 was tagged on a commit where pyproject.toml said
# 2.0.0 but earlier commits had `2.0.0rc4` referenced in docs that weren't
# updated. The release flow had no enforcement that "version" meant ONE
# value across the whole repo. This target catches that class of bug.

release-verify-version:
	@echo "▸ Version coherence check for v$(VERSION)"
	@# 1. Check git working tree is clean.
	@if [ -n "$$(git status --porcelain)" ]; then \
		echo "  ✗ Working tree has uncommitted changes. Commit or stash first."; \
		git status --short; \
		exit 1; \
	fi
	@echo "  ✓ Working tree clean"
	@# 2. Confirm we're on main (or release/* branch).
	@BRANCH=$$(git rev-parse --abbrev-ref HEAD); \
	if [ "$$BRANCH" != "main" ] && [[ "$$BRANCH" != release/* ]]; then \
		echo "  ✗ Current branch is '$$BRANCH', expected 'main' or 'release/*'."; \
		exit 1; \
	fi
	@echo "  ✓ Branch OK ($$(git rev-parse --abbrev-ref HEAD))"
	@# 3. Compare against origin to ensure we're up-to-date.
	@git fetch origin --quiet 2>/dev/null || true
	@LOCAL=$$(git rev-parse @ 2>/dev/null); \
	REMOTE=$$(git rev-parse @{u} 2>/dev/null || echo "no-upstream"); \
	if [ "$$REMOTE" != "no-upstream" ] && [ "$$LOCAL" != "$$REMOTE" ]; then \
		echo "  ⚠ Local branch differs from upstream. git pull/push first."; \
		echo "    local:  $$LOCAL"; \
		echo "    remote: $$REMOTE"; \
		exit 1; \
	fi
	@echo "  ✓ In sync with origin (or no upstream tracking)"
	@# 4. Cross-check version in __init__.py if it declares __version__.
	@if [ -f mcp_server/__init__.py ]; then \
		INIT_VER=$$(grep -E "^__version__" mcp_server/__init__.py 2>/dev/null | sed -E 's/.*= *"([^"]+)".*/\1/'); \
		if [ -n "$$INIT_VER" ] && [ "$$INIT_VER" != "$(VERSION)" ]; then \
			echo "  ✗ Version drift: pyproject.toml=$(VERSION) but mcp_server/__init__.py=$$INIT_VER"; \
			exit 1; \
		fi; \
		[ -n "$$INIT_VER" ] && echo "  ✓ mcp_server/__init__.py __version__ matches ($$INIT_VER)"; \
	fi
	@# 5. Check CHANGELOG has an entry for this version (not still Unreleased).
	@if [ -f CHANGELOG.md ]; then \
		if grep -q "^## \[$(VERSION)\]" CHANGELOG.md; then \
			echo "  ✓ CHANGELOG.md has an entry for $(VERSION)"; \
		else \
			echo "  ✗ CHANGELOG.md missing entry: ## [$(VERSION)] - <date>"; \
			echo "    Promote the [Unreleased] section to [$(VERSION)] before releasing."; \
			exit 1; \
		fi; \
	fi
	@# Staleness is a GIT question, not an mtime question. The old check used
	@# `find -newer CHANGELOG.md`, which reports any file whose mtime moved for
	@# a reason that has nothing to do with its content — a `git checkout` that
	@# restores a file, a formatter rewriting it in place, a fresh clone. It
	@# produced a false blocker on two consecutive releases while the CHANGELOG
	@# was in fact current, which trains a maintainer to bypass the gate.
	@#
	@# Commits that touch source AFTER the commit that last touched CHANGELOG.md
	@# is the thing actually being asked about, and it cannot be faked by a
	@# timestamp. Uncommitted source edits are caught by the clean-tree check
	@# at the top of this target, so they need no separate case here.
	@if [ -f CHANGELOG.md ]; then \
		CL_COMMIT=$$(git log -1 --format=%H -- CHANGELOG.md 2>/dev/null); \
		if [ -z "$$CL_COMMIT" ]; then \
			echo "  ⚠ CHANGELOG.md is untracked — skipping the staleness check"; \
		else \
			STALE=$$(git log --format=%H "$$CL_COMMIT"..HEAD -- mcp_server indexer 2>/dev/null | wc -l | tr -d ' '); \
			if [ "$$STALE" -gt "0" ]; then \
				echo "  ✗ $$STALE commit(s) changed mcp_server/ or indexer/ AFTER the last"; \
				echo "    CHANGELOG.md edit, so the $(VERSION) entry does not describe the wheel."; \
				echo "    Either: (a) update the entry to cover them, OR"; \
				echo "    (b) bump the patch version + add a new entry."; \
				echo "    Commits not yet described:"; \
				git log --format='      %h %s' "$$CL_COMMIT"..HEAD -- mcp_server indexer 2>/dev/null | sed -n '1,5p'; \
				exit 1; \
			fi; \
			echo "  ✓ CHANGELOG.md covers every source commit through HEAD"; \
		fi; \
	fi
	@# 6. Tag check: if tag exists, must point at HEAD.
	@if git rev-parse "v$(VERSION)" >/dev/null 2>&1; then \
		TAG_COMMIT=$$(git rev-parse "v$(VERSION)^{commit}" 2>/dev/null); \
		HEAD_COMMIT=$$(git rev-parse HEAD); \
		if [ "$$TAG_COMMIT" != "$$HEAD_COMMIT" ]; then \
			echo "  ⚠ Tag v$(VERSION) exists but points to a different commit."; \
			echo "    tag:  $$TAG_COMMIT"; \
			echo "    head: $$HEAD_COMMIT"; \
			echo "    Either move the tag or release a new patch version."; \
			exit 1; \
		fi; \
		echo "  ✓ Tag v$(VERSION) matches HEAD"; \
	else \
		echo "  ⚠ Tag v$(VERSION) does not exist yet (will be created by release-publish)"; \
	fi
	@echo "  ✓ Version coherence verified"

# ─── Build: clean → dist/ ──────────────────────────────────────────────────

release-build: clean
	@echo "▸ Building dist/ for v$(VERSION) ..."
	@$(PYTHON) -m pip install --quiet --upgrade build
	@$(PYTHON) -m build
	@echo ""
	@echo "Built artifacts:"
	@ls -la dist/

# ─── Dry-run: validate the artifact metadata BEFORE upload ─────────────────

release-dry-run:
	@echo "▸ Validating dist/ for v$(VERSION) ..."
	@if [ ! -d dist ] || [ -z "$$(ls dist/ 2>/dev/null)" ]; then \
		echo "  ✗ dist/ is empty. Run: make release-build"; \
		exit 1; \
	fi
	@$(PYTHON) -m pip install --quiet --upgrade twine
	@$(PYTHON) -m twine check dist/*
	@echo "  ✓ Metadata valid (twine check passed)"
	@# Verify the wheel name matches the declared version.
	@WHEEL=$$(ls dist/*.whl 2>/dev/null | sed -n '1p'); \
	if [ -n "$$WHEEL" ]; then \
		case "$$WHEEL" in \
			*-$(VERSION)-*) echo "  ✓ Wheel filename includes v$(VERSION)" ;; \
			*) echo "  ✗ Wheel $$WHEEL does NOT include v$(VERSION) — version drift in build"; exit 1 ;; \
		esac; \
	fi

# ─── Publish: gated by hook + all of the above must have passed ───────────
#
# This target is matched by .claude/hooks/pre-release-block.sh and will
# be refused unless .release-evidence/$(VERSION).json shows G1-G5 pass.

release-publish:
	@echo "▸ Publishing v$(VERSION) to PyPI"
	@echo "  (PreToolUse hook will block this if evidence is missing)"
	@if [ ! -f $(EVIDENCE_FILE) ]; then \
		echo "  ✗ No evidence file at $(EVIDENCE_FILE). Run: make release-gauntlet"; \
		exit 1; \
	fi
	@$(PYTHON) -c "import json; d=json.load(open('$(EVIDENCE_FILE)')); \
		assert d.get('G5_human_confirmed') is True, 'G5 not confirmed in evidence file. Set G5_human_confirmed=true after manual verification.'"
	$(PYTHON) -m twine upload dist/*

# ─── Post-release smoke: install from PyPI in fresh venv ──────────────────

release-smoke:
	@echo "▸ Post-release smoke test for v$(VERSION) (install from PyPI)"
	@TMPDIR=$$(mktemp -d); \
	echo "  Using temp dir: $$TMPDIR"; \
	$(PYTHON) -m venv $$TMPDIR/venv; \
	$$TMPDIR/venv/bin/pip install --quiet "codevira==$(VERSION)" || \
		(echo "  ✗ pip install codevira==$(VERSION) failed"; exit 1); \
	INSTALLED_VER=$$($$TMPDIR/venv/bin/codevira --version 2>&1 | sed -n '1p'); \
	echo "  Installed version reports: $$INSTALLED_VER"; \
	case "$$INSTALLED_VER" in \
		*$(VERSION)*) echo "  ✓ PyPI install of v$(VERSION) works" ;; \
		*) echo "  ✗ Version mismatch: expected $(VERSION), got: $$INSTALLED_VER"; exit 1 ;; \
	esac; \
	rm -rf $$TMPDIR

# ─── Full sequence: the human-runnable foolproof flow ──────────────────────
#
# Run this ONCE before publishing:
#   make release-full
#   # then manually set G5_human_confirmed=true in evidence file
#   make release-build
#   make release-dry-run
#   # then either:
#   make release-publish     (will work because all gates pass)
# OR:
#   twine upload dist/*      (will also work; hook checks evidence)

release-full: release-verify-version release-gauntlet release-build release-dry-run
	@echo ""
	@echo "═══════════════════════════════════════════════════════════════════"
	@echo "  Release-full complete for v$(VERSION)."
	@echo ""
	@echo "  Next steps:"
	@echo "    1. Manually verify on a real machine (G5 — the human gate)"
	@echo "    2. Edit $(EVIDENCE_FILE) and set \"G5_human_confirmed\": true"
	@echo "    3. Run: make release-publish"
	@echo "    4. After PyPI propagates (~30s): make release-smoke"
	@echo "═══════════════════════════════════════════════════════════════════"

# ─── CI (mirror what GitHub Actions runs) ──────────────────────────────────

ci: lint test-unit test-e2e

# ─── Cleanup ───────────────────────────────────────────────────────────────

clean:
	rm -rf build dist *.egg-info
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
