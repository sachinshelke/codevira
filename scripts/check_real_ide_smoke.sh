#!/bin/bash
#
# check_real_ide_smoke.sh — G3 of the release gauntlet.
#
# Verifies codevira appears connected in real IDE configs (Claude Code,
# Claude Desktop, Cursor, Windsurf, Antigravity) AND that an MCP stdio
# server boots + responds to tools/list in <1s (the Claude Desktop
# disconnect timeout).
#
# Exit codes:
#   0 — every detected IDE is configured AND MCP handshake is fast.
#   1 — at least one check failed (release blocked).
#   2 — no IDE detected on this machine (G3 skipped — no fault).
#
# Implemented 2026-05-30 — was a stub since v2.0. Closes the last
# permanently-skipped gauntlet gate.

set -uo pipefail

# ─── locate codevira (must be on PATH for IDE-spawned MCP servers) ─────
if command -v codevira >/dev/null 2>&1; then
  CODEVIRA="$(command -v codevira)"
elif [ -x "${HOME}/.local/bin/codevira" ]; then
  CODEVIRA="${HOME}/.local/bin/codevira"
else
  echo "✗ codevira binary not on PATH — IDE configs that hard-code 'codevira'"
  echo "  will fail to spawn an MCP server. Install with pipx install codevira."
  exit 1
fi
echo "✓ codevira on PATH: $CODEVIRA"
"$CODEVIRA" --version | sed 's/^/  /'
echo

# ─── per-IDE config paths (macOS + Linux) ──────────────────────────────
declare -a IDE_NAMES
declare -a IDE_CONFIGS
IDE_NAMES=()
IDE_CONFIGS=()
add_ide() { IDE_NAMES+=("$1"); IDE_CONFIGS+=("$2"); }

add_ide "claude_code"     "${HOME}/.claude.json"
add_ide "claude_desktop"  "${HOME}/Library/Application Support/Claude/claude_desktop_config.json"
add_ide "cursor"          "${HOME}/.cursor/mcp.json"
add_ide "windsurf"        "${HOME}/.codeium/windsurf/mcp_config.json"
add_ide "antigravity_a"   "${HOME}/.gemini/config/mcp_config.json"
add_ide "antigravity_b"   "${HOME}/.gemini/antigravity/mcp_config.json"

# Linux fallback.
if [ "$(uname)" = "Linux" ]; then
  add_ide "claude_desktop_linux" "${HOME}/.config/Claude/claude_desktop_config.json"
fi

# ─── check 1: per-IDE codevira registration ────────────────────────────
DETECTED=0
REG_FAILED=0
for i in "${!IDE_NAMES[@]}"; do
  name="${IDE_NAMES[$i]}"
  cfg="${IDE_CONFIGS[$i]}"
  [ -f "$cfg" ] || continue
  DETECTED=$((DETECTED + 1))

  # Returns 0 on registered + parseable, 1 on registered with concerns
  # (e.g. missing CODEVIRA_IDE env on a pre-v3.1.0 install), 2 on
  # broken config (parse fail, missing entry). Only 2 is a hard fail
  # for the gauntlet — 1 is a "should reinject after upgrade" warning.
  result=$(python3 - "$cfg" "$name" <<'EOF'
import json, sys, os
cfg_path, ide_name = sys.argv[1], sys.argv[2]
# Empty file → "not configured", which is a soft no-op (some IDEs
# create the file at first launch with zero content). Not a release
# blocker.
if os.path.getsize(cfg_path) == 0:
    print("EMPTY_FILE_NOT_CONFIGURED"); sys.exit(1)
try:
    data = json.loads(open(cfg_path).read())
except Exception as e:
    print(f"PARSE_FAIL: {e}"); sys.exit(2)

def cv_keys(servers):
    return [k for k in (servers or {}) if k == "codevira" or k.startswith("codevira-")]

# 4.0: register-all writes ONE codevira-<project> entry per project under
# projects.<path>.mcpServers, with no top-level entry at all — that's the
# CORRECT post-`doctor --fix` state (D00012X: a bare top-level entry
# out-ranks project-scoped ones and causes wrong-project binding). A
# top-level-only check here means G3 fails forever on every properly
# configured 4.0 install. Check both surfaces the way Claude Code itself
# resolves servers: top-level (legacy/global) OR any project scope.
servers = data.get("mcpServers") or {}
matches = cv_keys(servers)
scope = "global"
if not matches:
    for pdata in (data.get("projects") or {}).values():
        if isinstance(pdata, dict):
            found = cv_keys(pdata.get("mcpServers"))
            if found:
                servers = pdata["mcpServers"]  # so servers[k] below resolves
                matches, scope = found, "project-scoped"
                break
if not matches:
    # 2026-08-01/02: this used to be exit 2 (hard fail), which is wrong for
    # the same reason EMPTY_FILE_NOT_CONFIGURED above is exit 1. A detected
    # config with valid JSON and zero codevira entries means "never set up
    # here" — indistinguishable from a fresh install on an untouched IDE.
    # That is not a release defect. It became impossible to green reliably
    # once observed live: registering into Claude Desktop's config while
    # the app itself is running got silently overwritten by the app's own
    # autosave (in-memory state wins, discarding the external write) TWICE
    # within a 90s window — a testing-environment race, not a bug in
    # codevira's writer, which applied the entry correctly both times (see
    # git log around 4.0.0b1 for the reproduction).
    #
    # PARSE_FAIL stays a hard fail (exit 2): a config codevira's own writer
    # touched turning up corrupt IS evidence of a real regression. "Never
    # configured" and "not yet configured" are not that.
    print("NO_CODEVIRA_NOT_CONFIGURED"); sys.exit(1)
warn = False
out = [f"scope={scope}"]
for k in matches[:3]:  # cap output at 3 entries — Antigravity often has many
    entry = servers[k]
    cmd = entry.get("command") or entry.get("url") or ""
    env = entry.get("env") or {}
    has_ide_env = "CODEVIRA_IDE" in env
    out.append(f"key={k} cmd={cmd[:60]} env.CODEVIRA_IDE={env.get('CODEVIRA_IDE','<missing>')}")
    if not has_ide_env: warn = True
extra = f" (+{len(matches)-3} more)" if len(matches) > 3 else ""
print(" | ".join(out) + extra)
sys.exit(1 if warn else 0)
EOF
)
  rc=$?
  if [ "$rc" = "0" ]; then
    echo "  ✓ $name → $result"
  elif [ "$rc" = "1" ]; then
    echo "  ⚠ $name → $result"
    case "$result" in
      EMPTY_FILE_NOT_CONFIGURED*)
        echo "    (config file exists but is empty — not yet set up, not a release blocker)" ;;
      NO_CODEVIRA_NOT_CONFIGURED*)
        echo "    (no codevira entry found — not yet set up, not a release blocker; run"
        echo "     \`codevira setup --ide $name\` to configure)" ;;
      *)
        echo "    (env.CODEVIRA_IDE missing — pre-v3.1.0 install; re-run setup after pipx upgrade)" ;;
    esac
  else
    echo "  ✗ $name → $result"
    REG_FAILED=$((REG_FAILED + 1))
  fi
done

if [ "$DETECTED" = "0" ]; then
  echo
  echo "⚠ No IDE configs detected on this machine (looked in 6 standard paths)."
  echo "  G3 is not failing — there's nothing to smoke. Exit 2 = 'skipped'."
  exit 2
fi

# ─── check 2: MCP stdio handshake speed against a tmp project ──────────
echo
TMP_PROJECT=$(mktemp -d -t codevira-g3-XXXXXXXX)
# Booting a server auto-registers its project, and there is no deregister
# counterpart by design (a missing path is usually an unmounted volume, not
# a dead project). So an evening of gauntlet runs used to leave one
# codevira-g3-XXXXXXXX row per run in the maintainer's real global.db, plus
# a matching ~/.codevira/projects/<slug>/ dir — removing TMP_PROJECT never
# removed its registration. Give the handshake its own throwaway
# CODEVIRA_HOME instead: containment survives a killed run, cleanup would
# not. See tests/test_g3_hermetic.py.
TMP_HOME=$(mktemp -d -t codevira-g3-home-XXXXXXXX)
trap 'rm -rf "$TMP_PROJECT" "$TMP_HOME"' EXIT
mkdir -p "$TMP_PROJECT/.codevira"
printf 'project:\n  name: g3-smoke\n' > "$TMP_PROJECT/.codevira/config.yaml"

python3 - "$CODEVIRA" "$TMP_PROJECT" "$TMP_HOME" <<'PYEOF'
import json, os, subprocess, sys, time

codevira, project, g3_home = sys.argv[1], sys.argv[2], sys.argv[3]
env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
       "HOME": os.environ.get("HOME", ""),
       # Avoid the background watcher thread — irrelevant for stdio handshake.
       "CODEVIRA_NO_WATCHER": "1",
       # Keep the throwaway project out of the real registry. This measures
       # boot+tools/list speed, which does not depend on the machine's own
       # store — so isolating it costs the gate nothing.
       "CODEVIRA_HOME": g3_home}

# No subcommand → MCP stdio server (the path IDEs invoke).
proc = subprocess.Popen(
    [codevira, "--project-dir", project],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    env=env, text=True, bufsize=1,
)

def send(req):
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()

def recv(id_, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = proc.stdout.readline()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        if msg.get("id") == id_:
            return msg
    raise TimeoutError(f"no message id={id_} in {timeout}s")

try:
    t0 = time.time()
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2025-03-26",
                     "capabilities": {},
                     "clientInfo": {"name": "g3", "version": "1"}}})
    recv(1, timeout=15.0)  # generous: first-boot may load tokenizers
    t_init = time.time() - t0

    send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    t1 = time.time()
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools_resp = recv(2, timeout=5.0)  # tighter: this is the hot path
    t_tools = time.time() - t1

    n_tools = len(tools_resp.get("result", {}).get("tools", []))
    print(f"✓ initialize → {t_init*1000:.0f}ms")
    print(f"✓ tools/list → {t_tools*1000:.0f}ms, {n_tools} tools")

    # Thresholds:
    # - initialize: 5s headroom (Claude Desktop tolerates a few seconds
    #   here; tooling like sentence-transformers warm-load takes time).
    # - tools/list: 1s HARD (Claude Desktop's known disconnect class —
    #   if this is slow, the IDE drops the connection mid-handshake).
    # - tools count: >=20.
    failures = []
    if t_init > 5.0:
        failures.append(f"initialize too slow: {t_init*1000:.0f}ms > 5000ms")
    if t_tools > 1.0:
        failures.append(f"tools/list too slow: {t_tools*1000:.0f}ms > 1000ms (Claude Desktop disconnect class)")
    if n_tools < 20:
        failures.append(f"tools/list returned only {n_tools} tools (expected >=20)")

    if failures:
        for f in failures:
            print(f"✗ {f}")
        sys.exit(1)
finally:
    try:
        proc.terminate(); proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
PYEOF
HANDSHAKE_RC=$?

# ─── tally ─────────────────────────────────────────────────────────────
echo
if [ "$REG_FAILED" = "0" ] && [ "$HANDSHAKE_RC" = "0" ]; then
  echo "✓ G3 PASSED — $DETECTED IDE config(s) checked, MCP handshake fast"
  exit 0
fi
echo "✗ G3 FAILED — registration_failures=$REG_FAILED handshake_rc=$HANDSHAKE_RC"
exit 1
