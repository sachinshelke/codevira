# IDE config hygiene — stray / wrong / temp codevira entries

This covers the class of problem where codevira shows up in an IDE bound to the
**wrong project**, a **deleted/temporary directory**, or where **ghost projects**
accumulate in `~/.codevira/projects/`. Your real memory is never lost by any of
this — it lives in each project's in-repo `.codevira/` — but the *binding* can be
wrong until you clean the config.

---

## Symptoms

- "codevira works in the chat, but I can't see it in the IDE's connector list."
- A session returns **another project's** decisions, or an empty roadmap.
- Claude Desktop's codevira points at a path like
  `/private/var/folders/.../T/tmp.XXXX/…` that no longer exists.
- `~/.codevira/projects/` fills with `…_T_tmp…`, `…pytest…`, or
  `…cv-isolated…` directories you never created.

## Root cause

`codevira init` **auto-injects** codevira into *every* installed IDE's MCP config
(Claude Code, Claude Desktop, Cursor, Windsurf, Antigravity). So if `init` runs
in a **throwaway or temporary directory** — a scratch repo, a test harness, a
`mktemp` dir — it writes a real entry into your real IDE configs pointing at that
throwaway path, and registers a project dir under `~/.codevira/projects/`. When
the throwaway directory is later deleted, the entry is left pinned to a path that
no longer exists.

**Claude Desktop is the most visible victim.** Its MCP config
(`~/Library/Application Support/Claude/claude_desktop_config.json`) has **no
per-project scope** — it can pin exactly **one** project via `--project-dir`. So
whatever ran `init` last wins, and a stray `init` silently repoints it.

> Prevention: don't run `codevira init` in scratch/temp directories. If you must,
> run it with the IDE injection disabled: `codevira init --no-inject`.

---

## 1. Detect what's stray

Scan every surface for codevira entries that point at a temp/nonexistent path:

```bash
python3 - <<'PY'
import json, pathlib, re
TEMP = re.compile(r'/(private/)?(var/folders|tmp)/')
home = pathlib.Path.home()
surfaces = {
    "Claude Desktop":       home/"Library/Application Support/Claude/claude_desktop_config.json",
    "Claude Code":          home/".claude.json",
    "Antigravity shared":   home/".gemini/config/mcp_config.json",
    "Antigravity per-app":  home/".gemini/antigravity/mcp_config.json",
}
for label, p in surfaces.items():
    if not p.is_file():
        print(f"{label}: (missing)"); continue
    d = json.loads(p.read_text())
    def check(scope, servers):
        for k, v in (servers or {}).items():
            if "codevira" not in k.lower(): continue
            args = " ".join(map(str, v.get("args") or []))
            dead = bool(TEMP.search(args)) or ("--project-dir" in args and
                   not pathlib.Path(args.split("--project-dir",1)[1].strip().split()[0]).exists())
            print(f"  {'STRAY' if dead else 'ok   '} [{label}/{scope}] {k} -> {args or '(bare)'}")
    check("top", d.get("mcpServers"))
    for proj, pd in (d.get("projects") or {}).items():   # Claude Code project scope
        check(proj[-30:], (pd or {}).get("mcpServers"))

# ghost project dirs in the central home
import glob
print("\nGhost project dirs (source path gone):")
n = 0
for meta in glob.glob(str(home/".codevira/projects/*/metadata.json")):
    op = json.loads(open(meta).read()).get("original_path", "")
    if op and not pathlib.Path(op).exists():
        print("  ", pathlib.Path(meta).parent.name); n += 1
print(f"  ({n} ghost(s))" if n else "  (none)")
PY
```

## 2. Fix it

### The safe, built-in way

```bash
# Remove ONE project's entries (all IDE surfaces) + its central data dir.
# Works even if the directory is already gone — it matches on the path.
codevira untrack "/private/var/folders/.../T/tmp.XXXX/proj"

# Sweep ghost project dirs whose source no longer exists.
codevira clean --ghosts        # add -y to skip the prompt

# Re-check the machine end to end.
codevira doctor
```

### Claude Desktop specifically

Because Desktop can only hold one project, the fix is to **remove** the stray
entry and, if you want codevira in Desktop at all, **re-pin it to one real
project** (back it up first):

```bash
CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M%S)"

# Remove the codevira entry:
python3 - "$CFG" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d.get("mcpServers", {}).pop("codevira", None)
json.dump(d, open(p, "w"), indent=2)
print("removed codevira from Claude Desktop")
PY

# OPTIONAL — re-pin Desktop to ONE project (e.g. the one you use it for):
codevira init --project-dir "/Users/you/Documents/Projects/MyProject" --no-inject  # writes memory scaffold only
python3 - "$CFG" "$(command -v codevira)" "/Users/you/Documents/Projects/MyProject" <<'PY'
import json, sys
cfg, cmd, proj = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(cfg)); d.setdefault("mcpServers", {})["codevira"] = {
    "command": cmd, "args": ["--project-dir", proj], "env": {"CODEVIRA_IDE": "claude_desktop"}}
json.dump(d, open(cfg, "w"), indent=2); print("pinned Claude Desktop ->", proj)
PY
```

Then **fully quit and reopen** the IDE so it re-reads the config.

## 3. Verify

```bash
codevira doctor        # committed_memory, claude_binding_conflict, ghost_projects, mcp_running_versions
```

All checks green (or only informative warnings) means the machine is clean.

---

## What is NOT affected

- **Your decisions / sessions / skills.** They live in each project's in-repo
  `.codevira/` and are read from there regardless of a wrong IDE pin.
- **Other projects.** Removing a stray entry only removes that one entry;
  `codevira untrack` and the manual snippets here are scoped to a single
  project/key and never touch the others.
- Every edit above backs the file up first (`.bak-<timestamp>`); restore with
  `cp <file>.bak-… <file>` if anything looks wrong.
