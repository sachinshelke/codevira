#!/usr/bin/env bash
# uninstall-codevira.sh — complete, no-residue removal of codevira from a Mac
#
# Why this script instead of just `pipx uninstall codevira`?
#   `codevira clean` (the in-tool uninstaller) misses several things in v2.0.0:
#     - leaves ~/.claude/hooks/codevira-*.sh on disk
#     - doesn't drop codevira entries from ~/.claude/settings.json's hooks block
#     - doesn't remove per-project nudge files (CLAUDE.md / AGENTS.md / etc.)
#     - doesn't remove legacy .codevira/ directories committed inside repos
#     - doesn't clean pip's wheel cache
#     - doesn't remove the local-pypi-https Docker setup if you spun one up
#
#   This script does everything end-to-end. Safe to run on a machine that
#   never had codevira — it just no-ops every step that finds nothing.
#
# Usage:
#   bash uninstall-codevira.sh             # interactive: shows plan, asks before each destructive step
#   bash uninstall-codevira.sh --yes       # non-interactive: removes everything; no prompts
#   bash uninstall-codevira.sh --dry-run   # show what WOULD be removed; no changes
#   bash uninstall-codevira.sh --keep-data # remove binary + IDE configs but keep ~/.codevira/ (decisions, learned rules)
#
set -euo pipefail

DRY_RUN=0
YES=0
KEEP_DATA=0
for arg in "$@"; do
  case "$arg" in
    --dry-run)    DRY_RUN=1 ;;
    --yes|-y)     YES=1 ;;
    --keep-data)  KEEP_DATA=1 ;;
    -h|--help)
      sed -n '2,30p' "$0"
      exit 0 ;;
    *)
      echo "unknown arg: $arg" >&2
      exit 2 ;;
  esac
done

# ----- helpers -----------------------------------------------------------

run() {
  # Run a shell command, respecting --dry-run.
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] $*"
  else
    eval "$@"
  fi
}

confirm() {
  # Prompt y/N before a destructive step. Non-interactive when --yes.
  local prompt="$1"
  if [[ $YES -eq 1 || $DRY_RUN -eq 1 ]]; then
    return 0
  fi
  read -r -p "$prompt [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

note()  { printf "\n\033[1;34m▸\033[0m %s\n" "$*"; }
ok()    { printf "  \033[1;32m✓\033[0m %s\n" "$*"; }
skip()  { printf "  \033[1;33m·\033[0m %s\n" "$*"; }
warn()  { printf "  \033[1;33m⚠\033[0m %s\n" "$*"; }

# ----- step 0: snapshot for safety --------------------------------------

note "step 0 — snapshot existing state to /tmp (recoverable in case of regret)"

BACKUP_DIR="/tmp/codevira-uninstall-backup-$(date +%Y%m%d-%H%M%S)"
if [[ $DRY_RUN -eq 0 ]]; then
  mkdir -p "$BACKUP_DIR"
  [[ -d ~/.codevira ]] && cp -r ~/.codevira "$BACKUP_DIR/dot-codevira" 2>/dev/null || true
  for f in \
    "$HOME/Library/Application Support/Claude/claude_desktop_config.json" \
    "$HOME/.claude.json" \
    "$HOME/.claude/settings.json" \
    "$HOME/.codeium/windsurf/mcp_config.json" \
    "$HOME/.gemini/antigravity/mcp_config.json" \
    "$HOME/.cursor/mcp.json"
  do
    [[ -f "$f" ]] && cp "$f" "$BACKUP_DIR/$(basename "$f")" 2>/dev/null || true
  done
  [[ -d ~/.claude/hooks ]] && cp -r ~/.claude/hooks "$BACKUP_DIR/claude-hooks" 2>/dev/null || true
  ok "backup at $BACKUP_DIR"
else
  ok "[dry-run] would back up to $BACKUP_DIR"
fi

# ----- step 1: try codevira's own clean first ---------------------------

note "step 1 — codevira clean (best-effort; binary may not exist)"

if command -v codevira >/dev/null 2>&1; then
  if confirm "Run \`codevira clean -y\` first?"; then
    run "codevira clean -y --all 2>&1 | tail -20" || warn "codevira clean failed, continuing"
    ok "codevira clean attempted"
  else
    skip "codevira clean skipped"
  fi
else
  skip "codevira binary not on PATH; nothing to invoke"
fi

# ----- step 2: pipx uninstall -------------------------------------------

note "step 2 — pipx uninstall codevira"

if pipx list 2>/dev/null | grep -q "package codevira "; then
  if confirm "Uninstall codevira via pipx?"; then
    run "pipx uninstall codevira"
    ok "pipx uninstalled"
  else
    skip "pipx uninstall skipped"
  fi
else
  skip "codevira not installed via pipx"
fi

# Defensive: also try pip uninstall (some users install with plain pip)
if python3 -m pip show codevira >/dev/null 2>&1; then
  if confirm "Found codevira installed via pip too. Uninstall it?"; then
    run "python3 -m pip uninstall -y codevira"
    ok "pip-installed codevira removed"
  fi
fi

# ----- step 3: drop codevira from every IDE config ----------------------

note "step 3 — strip codevira from IDE config files (mcpServers.codevira)"

# A small Python helper that surgically removes the codevira key without
# disturbing other entries in mcpServers.
strip_codevira_from_config() {
  local path="$1"
  local label="$2"
  if [[ ! -f "$path" ]]; then
    skip "$label — config file not present"
    return
  fi
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] would strip mcpServers.codevira from $path"
    return
  fi
  python3 - "$path" <<'PY' && ok "$label cleaned" || warn "$label — failed (check $path manually)"
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
try:
    d = json.loads(p.read_text())
except Exception as e:
    print(f"  parse failed: {e}")
    sys.exit(1)
mcp = d.get("mcpServers", {})
removed = mcp.pop("codevira", None) is not None
if removed:
    if not mcp and "mcpServers" in d:
        del d["mcpServers"]
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    tmp.replace(p)
sys.exit(0)
PY
}

strip_codevira_from_config "$HOME/Library/Application Support/Claude/claude_desktop_config.json" "Claude Desktop"
strip_codevira_from_config "$HOME/.claude.json"                                                  "Claude Code (~/.claude.json)"
strip_codevira_from_config "$HOME/.codeium/windsurf/mcp_config.json"                             "Windsurf"
strip_codevira_from_config "$HOME/.gemini/antigravity/mcp_config.json"                           "Antigravity"
strip_codevira_from_config "$HOME/.cursor/mcp.json"                                              "Cursor"

# ----- step 4: remove Claude Code hook scripts AND deregister from settings.json

note "step 4 — Claude Code lifecycle hooks (scripts + settings.json registration)"

# 4a — remove hook scripts
HOOK_SCRIPTS=( ~/.claude/hooks/codevira-*.sh )
if compgen -G "$HOME/.claude/hooks/codevira-*.sh" > /dev/null; then
  if confirm "Remove $(ls ~/.claude/hooks/codevira-*.sh 2>/dev/null | wc -l | tr -d ' ') hook script(s)?"; then
    run "rm -v ~/.claude/hooks/codevira-*.sh"
    ok "hook scripts removed"
  fi
else
  skip "no codevira-* hook scripts to remove"
fi

# 4b — drop codevira hook entries from settings.json
SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$SETTINGS" ]]; then
  if [[ $DRY_RUN -eq 1 ]]; then
    echo "  [dry-run] would strip codevira entries from $SETTINGS hooks block"
  else
    python3 - "$SETTINGS" <<'PY' && ok "settings.json deregistered" || warn "settings.json deregister failed"
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
try:
    d = json.loads(p.read_text())
except Exception:
    sys.exit(1)
hooks = d.get("hooks") or {}
new_hooks = {}
for event, entries in hooks.items():
    kept = []
    for entry in entries or []:
        kept_inner = [
            h for h in (entry.get("hooks") or [])
            if "codevira-" not in h.get("command", "")
        ]
        if kept_inner:
            ne = dict(entry)
            ne["hooks"] = kept_inner
            kept.append(ne)
    if kept:
        new_hooks[event] = kept
if new_hooks:
    d["hooks"] = new_hooks
elif "hooks" in d:
    del d["hooks"]
tmp = p.with_suffix(p.suffix + ".tmp")
tmp.write_text(json.dumps(d, indent=2))
tmp.replace(p)
PY
  fi
else
  skip "~/.claude/settings.json not present"
fi

# ----- step 5: ~/.codevira/ data directory ------------------------------

note "step 5 — ~/.codevira/ data directory (decisions, learned rules, project graphs)"

if [[ -d ~/.codevira ]]; then
  SIZE=$(du -sh ~/.codevira 2>/dev/null | awk '{print $1}')
  if [[ $KEEP_DATA -eq 1 ]]; then
    skip "--keep-data set; preserving ~/.codevira ($SIZE)"
  else
    if confirm "Remove ~/.codevira ($SIZE)? backup at $BACKUP_DIR"; then
      run "rm -rf ~/.codevira"
      ok "~/.codevira removed"
    else
      skip "~/.codevira preserved (your choice)"
    fi
  fi
else
  skip "~/.codevira not present"
fi

# ----- step 6: per-project artifacts in repos --------------------------

note "step 6 — per-project nudge files & legacy .codevira/ dirs"

# Default search: only the current dir's tree. To scan all your project
# roots, override CODEVIRA_PROJECT_SEARCH_ROOTS as a colon-separated list.
SEARCH_ROOTS="${CODEVIRA_PROJECT_SEARCH_ROOTS:-$PWD}"

found_any=0
IFS=':'
for root in $SEARCH_ROOTS; do
  unset IFS
  [[ -d "$root" ]] || continue

  # Per-project nudge files codevira writes during `setup`. They contain a
  # codevira:start ... codevira:end block. We surgically strip just that block
  # if other content surrounds it (e.g. a project's own CLAUDE.md), or remove
  # the whole file if it's only the codevira block.
  for nudge in "$root/CLAUDE.md" "$root/AGENTS.md" "$root/GEMINI.md" "$root/.windsurfrules" \
               "$root/.cursor/rules/codevira.mdc" "$root/.github/copilot-instructions.md"; do
    if [[ -f "$nudge" ]] && grep -q "codevira:start" "$nudge" 2>/dev/null; then
      found_any=1
      if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [dry-run] would strip codevira block from $nudge"
        continue
      fi
      python3 - "$nudge" <<'PY'
import sys, pathlib, re
p = pathlib.Path(sys.argv[1])
text = p.read_text()
new = re.sub(r"<!-- codevira:start -->.*?<!-- codevira:end -->\n?", "", text, flags=re.DOTALL)
if new.strip():
    p.write_text(new)
    print(f"  ✓ codevira block removed from {p}")
else:
    p.unlink()
    print(f"  ✓ {p} (file was only codevira content)")
PY
    fi
  done

  # Legacy in-repo .codevira/ directory (pre-v1.6 layout)
  if [[ -d "$root/.codevira" ]]; then
    found_any=1
    if confirm "Remove legacy $root/.codevira/?"; then
      run "rm -rf '$root/.codevira'"
      ok "$root/.codevira removed"
    fi
  fi

  # Legacy .codevira.migrated/ backup (post-migration safety net)
  if [[ -d "$root/.codevira.migrated" ]]; then
    found_any=1
    if confirm "Remove backup $root/.codevira.migrated/?"; then
      run "rm -rf '$root/.codevira.migrated'"
      ok "$root/.codevira.migrated removed"
    fi
  fi
done
[[ $found_any -eq 0 ]] && skip "no per-project nudge files / legacy dirs in $SEARCH_ROOTS"

# ----- step 7: pipx leftovers + pip cache -------------------------------

note "step 7 — pipx leftovers and pip's wheel cache"

# pipx_shared.pth is a leftover that occasionally lands in project dirs from
# pipx-related operations. Cosmetic but worth removing.
if [[ -f "./pipx_shared.pth" ]]; then
  run "rm -v ./pipx_shared.pth"
  ok "removed local pipx_shared.pth"
fi

if confirm "Purge pip's wheel cache (frees disk; not codevira-specific)?"; then
  run "python3 -m pip cache purge 2>&1 | tail -2"
  ok "pip cache purged"
fi

# ----- step 8: optional — local PyPI Docker decommission ---------------

note "step 8 — local PyPI server (only if you ran the rc-testing setup)"

if [[ -d ~/.codevira-local-pypi ]]; then
  if confirm "Remove ~/.codevira-local-pypi/ (~$(du -sh ~/.codevira-local-pypi 2>/dev/null | awk '{print $1}'))?"; then
    run "rm -rf ~/.codevira-local-pypi"
    ok "local PyPI directory removed"
  fi
fi

# Stop and remove containers if they exist (best-effort; needs Docker running)
if command -v docker >/dev/null 2>&1; then
  if docker ps -a --format "{{.Names}}" 2>/dev/null | grep -qE "^(pypi|codevira-pypi-nginx)$"; then
    if confirm "Stop + remove local PyPI Docker containers (pypi, codevira-pypi-nginx)?"; then
      run "docker stop pypi codevira-pypi-nginx 2>/dev/null"
      run "docker rm pypi codevira-pypi-nginx 2>/dev/null"
      run "docker network rm codevira-pypi-net 2>/dev/null"
      ok "Docker containers removed"
    fi
  fi
fi

# ----- step 9: launchd service (macOS auto-start) -----------------------

note "step 9 — launchd service (only if you ran 'codevira serve --install-service')"

PLIST="$HOME/Library/LaunchAgents/com.codevira.server.plist"
if [[ -f "$PLIST" ]]; then
  if confirm "Unload + remove launchd service?"; then
    run "launchctl unload '$PLIST' 2>/dev/null || true"
    run "rm -v '$PLIST'"
    ok "launchd service removed"
  fi
else
  skip "no launchd service installed"
fi

# ----- step 10: final verification --------------------------------------

note "step 10 — verification"

errors=0
for path in \
  ~/.codevira \
  ~/.local/pipx/venvs/codevira \
  ~/.claude/hooks/codevira-session_start.sh
do
  if [[ -e "$path" ]]; then
    [[ "$path" == ~/.codevira && $KEEP_DATA -eq 1 ]] && continue
    warn "still present: $path"
    errors=$((errors + 1))
  fi
done

if command -v codevira >/dev/null 2>&1; then
  warn "codevira binary still on PATH: $(which codevira)"
  errors=$((errors + 1))
fi

# Spot-check IDE configs
for cfg in \
  "$HOME/Library/Application Support/Claude/claude_desktop_config.json" \
  "$HOME/.claude.json" \
  "$HOME/.codeium/windsurf/mcp_config.json" \
  "$HOME/.gemini/antigravity/mcp_config.json"
do
  if [[ -f "$cfg" ]] && python3 -c "
import json, sys
d = json.load(open(sys.argv[1]))
sys.exit(0 if 'codevira' not in d.get('mcpServers', {}) else 1)
" "$cfg" 2>/dev/null; then
    : # clean — codevira not in config
  elif [[ -f "$cfg" ]]; then
    warn "codevira still listed in $cfg"
    errors=$((errors + 1))
  fi
done

echo
if [[ $errors -eq 0 ]]; then
  printf "\033[1;32m✓ Codevira fully removed.\033[0m\n"
  echo
  echo "  Reinstall any time:  pipx install codevira && codevira setup"
  echo "  Recover from backup: cp -r $BACKUP_DIR/dot-codevira ~/.codevira"
else
  printf "\033[1;33m⚠ Removal complete with %d residual item(s) above.\033[0m Review the warnings.\n" "$errors"
fi

# Restart hint
if pgrep -f "Claude.app" >/dev/null 2>&1 || pgrep -fi "claude code" >/dev/null 2>&1; then
  echo
  echo "  Restart any open Claude Desktop / Claude Code session so the running"
  echo "  app picks up the new (codevira-free) config:"
  echo "    osascript -e 'quit app \"Claude\"' && sleep 2 && open -a Claude"
fi
