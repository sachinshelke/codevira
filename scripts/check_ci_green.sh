#!/bin/bash
#
# check_ci_green.sh — G1.9 of the release gauntlet.
#
# Asks GitHub whether CI passed on the commit being released.
#
# Why: 4.2.0 was published on 2026-09-15 from a commit whose CI had been red for
# four days. A test file that needs Python 3.11 stopped the whole 3.10 job at
# collection. Every local gate was green, because the gauntlet runs on one
# interpreter and nothing in it ever looked at CI.
#
# Exit codes:
#   0 — every CI run for HEAD finished and succeeded.
#   1 — CI failed on HEAD (release blocked).
#   2 — could not tell: not pushed, still running, no gh, offline, or running
#       inside CI itself. Recorded as "skipped", which blocks publishing.

set -uo pipefail

skip() {
  echo "  could not check CI: $1"
  exit 2
}

[ -z "${GITHUB_ACTIONS:-}" ] || skip "this is CI"
command -v gh >/dev/null 2>&1 || skip "the GitHub CLI (gh) is not installed"
SHA=$(git rev-parse HEAD 2>/dev/null) || skip "not a git checkout"
[ -z "$(git status --porcelain --untracked-files=no 2>/dev/null)" ] \
  || skip "uncommitted changes, so no commit describes what would be released"

RUNS=$(gh run list --workflow ci.yml --commit "$SHA" --limit 50 \
  --json event,status,conclusion,createdAt,url 2>/dev/null) \
  || skip "GitHub did not answer"

RUNS="$RUNS" SHA="$SHA" python3 -c '
import json, os, sys

try:
    runs = json.loads(os.environ["RUNS"])
except ValueError:
    print("  could not check CI: unreadable answer from GitHub")
    sys.exit(2)
short = os.environ["SHA"][:7]
if not runs:
    print(f"  could not check CI: no run for {short}. Push it and wait for CI.")
    sys.exit(2)

# A newer run of the same kind replaces an older one; a push run and a
# pull-request run of one commit are different evidence and both must pass.
latest = {}
for r in sorted(runs, key=lambda r: r["createdAt"]):
    latest[r["event"]] = r
done = [r for r in latest.values() if r["status"] == "completed"]
failed = [r for r in done if r["conclusion"] != "success"]
pending = [r for r in latest.values() if r["status"] != "completed"]
for r in failed:
    print("  CI %s on %s (%s): %s" % (r["conclusion"], short, r["event"], r["url"]))
if failed:
    sys.exit(1)
for r in pending:
    print("  could not check CI: still %s on %s: %s" % (r["status"], short, r["url"]))
if pending:
    sys.exit(2)
print(f"  CI passed on {short} ({len(latest)} run(s))")
'
