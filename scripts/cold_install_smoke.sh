#!/usr/bin/env bash
# cold_install_smoke.sh — v2.1.2 hardening (Test B).
#
# Builds the wheel from HEAD, installs it in a CLEAN venv, bootstraps
# a fresh project, runs every public CLI command (--help + at least one
# real invocation), asserts on key output patterns.
#
# Catches the classes of bug unit tests can't see:
#   - Wheel packaging gaps (missing files in MANIFEST.in)
#   - Bootstrap regressions on truly-empty projects
#   - Help-text drift vs actual command behavior
#   - Version-bump misses (pyproject vs __init__ vs entry-point output)
#   - Auto-init failures on cold start
#
# Usage:
#   bash scripts/cold_install_smoke.sh
#   # or via make:
#   make smoke-install
#
# Exits 0 on success, non-zero on first failure. Emits ✓/✗ markers
# so the output is greppable.

set -uo pipefail

# ─── Setup ──────────────────────────────────────────────────────────
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Find a real Python — prefer 3.13 or 3.12 (the codevira-supported range).
PY=""
for candidate in /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
                  /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
                  $(command -v python3.13) $(command -v python3.12) \
                  $(command -v python3); do
    if [ -x "$candidate" ]; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "✗ FATAL: no python3.13 or python3.12 found"
    exit 1
fi
echo "  Using Python: $PY"

# Pull version once for assertions.
EXPECTED_VERSION="$(python3 -c "import re; print(re.search(r'version = \"([^\"]+)\"', open('pyproject.toml').read()).group(1))")"
echo "  Expected version: $EXPECTED_VERSION"

# ─── Step 1: build the wheel ────────────────────────────────────────
echo
echo "═══ Step 1: build wheel ═══"
rm -rf dist build
PYTHON="$PY" make release-build > /tmp/cold_install_build.log 2>&1
if [ ! -f "dist/codevira-${EXPECTED_VERSION}-py3-none-any.whl" ]; then
    echo "✗ FAIL: wheel codevira-${EXPECTED_VERSION}-py3-none-any.whl not built"
    echo "  Last 20 lines of build log:"
    tail -20 /tmp/cold_install_build.log
    exit 1
fi
WHEEL="dist/codevira-${EXPECTED_VERSION}-py3-none-any.whl"
WHEEL_SIZE=$(stat -f%z "$WHEEL" 2>/dev/null || stat -c%s "$WHEEL")
echo "✓ wheel built: $WHEEL ($WHEEL_SIZE bytes)"

# ─── Step 2: fresh venv + install ───────────────────────────────────
echo
echo "═══ Step 2: clean venv + install ═══"
TMP="$(mktemp -d -t cold_install_smoke_XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
"$PY" -m venv "$TMP/venv" > /tmp/cold_install_venv.log 2>&1
if [ ! -x "$TMP/venv/bin/codevira" ] && [ ! -x "$TMP/venv/bin/pip" ]; then
    echo "✗ FAIL: venv creation failed"
    cat /tmp/cold_install_venv.log
    exit 1
fi
"$TMP/venv/bin/pip" install --quiet "$WHEEL" 2>&1 | tail -3
if [ ! -x "$TMP/venv/bin/codevira" ]; then
    echo "✗ FAIL: codevira entry point not installed"
    exit 1
fi
echo "✓ wheel installed; codevira entry point present"

# ─── Step 2.5: venv size budget (v2.2.0 G1 — ≤100 MB target) ───────
# v2.2.0 architectural promise: dropping chromadb / sentence-transformers /
# torch lands install at ≤100 MB (was ~450 MB on v2.1.2 with
# tree-sitter-language-pack + chromadb stack). 100 MB matches the
# practical floor in a 2026-05 dep tree: mcp pulls cryptography
# (24 MB) + pydantic (4 MB) + httpx (1 MB); pip itself takes 11 MB;
# rich pulls pygments (9 MB); the 4 individual tree-sitter grammars
# (TS/JS/Go/Rust) total ~5 MB; codevira itself is ~3 MB; the rest is
# transitive (~40 MB). Earlier planning numbers (≤55 MB) didn't
# account for mcp's 2026 dep growth.
#
# If a future dep change inflates the venv beyond the budget, fail
# loudly here so the regression doesn't reach users via PyPI. Override
# with CODEVIRA_VENV_SIZE_MAX_MB=NNN for local experimentation.
echo
echo "═══ Step 2.5: venv size budget (≤${CODEVIRA_VENV_SIZE_MAX_MB:-100} MB) ═══"
VENV_SIZE_MB="$(du -sm "$TMP/venv" | awk '{print $1}')"
VENV_SIZE_LIMIT_MB="${CODEVIRA_VENV_SIZE_MAX_MB:-100}"
if [ "$VENV_SIZE_MB" -le "$VENV_SIZE_LIMIT_MB" ]; then
    echo "✓ venv size: ${VENV_SIZE_MB} MB (under ${VENV_SIZE_LIMIT_MB} MB budget)"
else
    echo "✗ FAIL: venv size ${VENV_SIZE_MB} MB exceeds budget ${VENV_SIZE_LIMIT_MB} MB"
    echo "  Top 5 packages contributing to the bloat:"
    du -sh "$TMP/venv/lib"/*/site-packages/* 2>/dev/null \
        | sort -rh | awk 'NR<=5 {print "    "$0}'
    echo
    echo "  To diagnose: pip install --target ./_inspect $WHEEL && du -sh _inspect/*"
    echo "  To override the gate (dev only): CODEVIRA_VENV_SIZE_MAX_MB=NNN $0"
    exit 1
fi

# ─── Step 3: version ───────────────────────────────────────────────
echo
echo "═══ Step 3: --version ═══"
ACTUAL_VERSION="$("$TMP/venv/bin/codevira" --version 2>&1 | awk '{print $2}')"
if [ "$ACTUAL_VERSION" != "$EXPECTED_VERSION" ]; then
    echo "✗ FAIL: version mismatch — wheel reports '$ACTUAL_VERSION', pyproject says '$EXPECTED_VERSION'"
    exit 1
fi
echo "✓ codevira --version → $ACTUAL_VERSION"

# ─── Step 4: --help (top-level) ────────────────────────────────────
echo
echo "═══ Step 4: subcommand registration ═══"
HELP_OUT="$("$TMP/venv/bin/codevira" --help 2>&1)"
# v2.1.2 must surface: reset, export, calibrate (the new commands)
for cmd in reset export calibrate heal doctor projects clean insights replay budget; do
    if echo "$HELP_OUT" | grep -q "$cmd"; then
        echo "✓ $cmd present in top-level --help"
    else
        echo "✗ FAIL: $cmd missing from top-level --help"
        echo "$HELP_OUT" | sed -n '1,10p'
        exit 1
    fi
done

# ─── Step 5: per-command --help works (no exception) ───────────────
echo
echo "═══ Step 5: per-command --help ═══"
for cmd in init index status report serve setup register configure budget doctor \
           projects agents replay insights clean heal reset export calibrate; do
    if "$TMP/venv/bin/codevira" "$cmd" --help > /tmp/cold_install_cmdhelp.log 2>&1; then
        echo "✓ codevira $cmd --help"
    else
        echo "✗ FAIL: codevira $cmd --help exited non-zero"
        tail -10 /tmp/cold_install_cmdhelp.log
        exit 1
    fi
done

# ─── Step 6: doctor on a fresh project ─────────────────────────────
echo
echo "═══ Step 6: doctor on fresh project ═══"
PROJECT="$TMP/proj"
mkdir -p "$PROJECT"
cat > "$PROJECT/pyproject.toml" <<EOF
[project]
name = "smoke-test"
version = "0.0.1"
EOF
cd "$PROJECT"
"$TMP/venv/bin/codevira" --project-dir "$PROJECT" doctor > /tmp/cold_install_doctor.log 2>&1
DOCTOR_RC=$?
# doctor returning non-zero is OK (ghost projects etc.); we just want to confirm
# it RAN and produced output.
if [ -s /tmp/cold_install_doctor.log ]; then
    PASSES=$(grep -c "^✓\|pass " /tmp/cold_install_doctor.log || true)
    echo "✓ doctor ran (rc=$DOCTOR_RC, $PASSES ✓ checks)"
else
    echo "✗ FAIL: doctor produced no output"
    exit 1
fi

# ─── Step 7: reset --help (v2.1.2 critical command) ────────────────
echo
echo "═══ Step 7: reset --help cites v2.1.2 split-from-heal text ═══"
RESET_HELP="$("$TMP/venv/bin/codevira" reset --help 2>&1)"
for phrase in "AUTO-EXPORTED" "no-backup" "destructive"; do
    if echo "$RESET_HELP" | grep -iq "$phrase"; then
        echo "✓ reset --help mentions '$phrase'"
    else
        echo "✗ FAIL: reset --help missing '$phrase'"
        exit 1
    fi
done

# ─── Step 8: heal --vectors --dry-run (deprecation warning) ────────
echo
echo "═══ Step 8: heal deprecation forwards to reset ═══"
HEAL_OUT="$("$TMP/venv/bin/codevira" --project-dir "$PROJECT" heal --vectors --dry-run 2>&1)"
if echo "$HEAL_OUT" | grep -q "DEPRECATED in v2.1.2"; then
    echo "✓ heal --vectors triggers deprecation warning"
else
    echo "✗ FAIL: heal --vectors didn't print v2.1.2 deprecation warning"
    echo "$HEAL_OUT" | sed -n '1,10p'
    exit 1
fi
if echo "$HEAL_OUT" | grep -q "Codevira — Reset"; then
    echo "✓ heal --vectors forwards to reset cmd"
else
    echo "✗ FAIL: heal --vectors didn't forward to reset"
    exit 1
fi

# ─── Step 9: calibrate --help cites correct clamp range ────────────
echo
echo "═══ Step 9: calibrate --help cites code-correct clamp range ═══"
CAL_HELP="$("$TMP/venv/bin/codevira" calibrate --help 2>&1)"
# v2.1.2 38447fe: clamp range MUST be [0.35, 0.80], not the old [0.20, 0.55]
if echo "$CAL_HELP" | grep -q '\[0\.35, 0\.80\]'; then
    echo "✓ calibrate --help cites clamp range [0.35, 0.80]"
else
    echo "✗ FAIL: calibrate --help clamp range wrong — doc-drift regression"
    echo "$CAL_HELP" | grep -i clamp || echo "  (no 'clamp' line found)"
    exit 1
fi

# ─── Step 10: export --help cites v2.1.2 surfaces ──────────────────
echo
echo "═══ Step 10: export --help (Item 3e) ═══"
EXP_HELP="$("$TMP/venv/bin/codevira" export --help 2>&1)"
for phrase in "decisions" "json" "sql"; do
    if echo "$EXP_HELP" | grep -iq "$phrase"; then
        echo "✓ export --help mentions '$phrase'"
    else
        echo "✗ FAIL: export --help missing '$phrase'"
        exit 1
    fi
done

# ─── Step 11: bundled per-language playbooks in wheel ──────────────
echo
echo "═══ Step 11: bundled v2.1.2 playbooks packaged ═══"
RULES_DIR="$TMP/venv/lib/python"*"/site-packages/mcp_server/data/rules"
for p in coding-standards-typescript coding-standards-go coding-standards-generic; do
    if ls $RULES_DIR/${p}.md > /dev/null 2>&1; then
        echo "✓ bundled playbook: ${p}.md"
    else
        echo "✗ FAIL: bundled playbook missing from wheel: ${p}.md"
        exit 1
    fi
done

# ─── Step 12: troubleshooting doc in repo (not in wheel) ───────────
echo
echo "═══ Step 12: docs/troubleshooting/antigravity.md present ═══"
if [ -f "$REPO_ROOT/docs/troubleshooting/antigravity.md" ]; then
    echo "✓ docs/troubleshooting/antigravity.md exists"
else
    echo "✗ FAIL: docs/troubleshooting/antigravity.md missing"
    exit 1
fi

# ─── Summary ───────────────────────────────────────────────────────
echo
echo "═══════════════════════════════════════════════════════════════"
echo "✓ Cold-install smoke PASSED for codevira $EXPECTED_VERSION"
echo "  Wheel:   $WHEEL ($WHEEL_SIZE bytes)"
echo "  Tested:  fresh venv + 19 subcommand --help + reset/export/calibrate/heal"
echo "  Cleanup: $TMP (will be removed by trap)"
echo "═══════════════════════════════════════════════════════════════"
exit 0
