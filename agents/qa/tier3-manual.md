# QA Tier 3 — Manual / Human-Driven Checklists

6 angles that need real environments or human judgment. Run pre-beta /
pre-GA, time-boxed to ~1 day each.

These are pre-flight checks, not exhaustive scientific experiments.
Aim is to catch obvious failures, not prove perfection.

---

## Angle 10 — External Schema (Live Verification)

**What it tests:** Real-world docs are accurate; nothing has changed
since we read them.

**How:**
1. For each tier-1 IDE, fetch the latest docs URL.
2. Open the relevant page and skim for any changes since the date you
   last verified (check `docs/v2-execution-log.md` for the verification
   date).
3. Pay attention to:
   - "Breaking change" callouts in the IDE's release notes
   - New required fields the IDE added
   - Fields the IDE deprecated (we should avoid using them)
4. If anything changed, queue a `13-multi-ide-schema.md` agent run.

**Time:** 15 min per IDE × 7 IDEs = ~2 hrs.

**Frequency:** Quarterly post-GA. Pre-GA is captured by R5/Angle 13.

---

## Angle 11 — Live Claude Code Lifecycle Observation

**What it tests:** Claude Code's runtime behavior matches its documented
schema. (Docs can lag; runtime can have undocumented quirks.)

**How:**
1. Install hooks in user's actual `~/.claude/hooks/codevira-*.sh` (already
   done in Round 5).
2. Add a logging shim before the codevira call:
   ```bash
   # In ~/.claude/hooks/codevira-pre_tool_use.sh, before the exec line:
   tee /tmp/cc-hook-input-$(date +%s).json
   ```
3. Add to `~/.claude/settings.json`:
   ```json
   {"hooks":{"PreToolUse":[{"matcher":"Edit|Write","hooks":[
     {"type":"command","command":"$HOME/.claude/hooks/codevira-pre_tool_use.sh","timeout":10}
   ]}]}}
   ```
4. Open Claude Code in any project. Ask Claude to make a small Edit.
5. Inspect `/tmp/cc-hook-input-*.json` — compare every field to what
   `_build_event` in `claude_code_hooks.py` reads.
6. Document any divergence in `docs/v2-execution-log.md`.

**Time:** 30 min.

**Frequency:** Once before alpha.1; once before GA; quarterly post-GA.

**Cleanup:** Remove the `tee` line and `/tmp/cc-hook-input-*.json` files
when done.

---

## Angle 16 — Long-Haul Soak Test

**What it tests:** Memory leaks, file-descriptor leaks, lock starvation
over long uptime.

**How:**
1. Run `agents/qa/tier2-scripts.md::Angle 09 (concurrent stress)` in
   a loop for 4+ hours.
2. Watch resident memory of the codevira process via `ps -o rss`.
3. Watch open file descriptors via `lsof -p $PID | wc -l`.
4. Look for:
   - Resident memory growing >50 MB/hour (leak)
   - FD count growing unbounded (connection cache leak — R2 candidate)
   - Slowdown over time (lock contention or GC pressure)

**Pass criteria:**
- Memory growth < 20 MB / 4 hours
- FD count stable
- p95 latency variance < 2× over the run

**Time:** 4–8 hrs (mostly waiting). Run overnight.

**Frequency:** Once before GA; before any major architecture change.

---

## Angle 18 — CLI UX (New User Perspective)

**What it tests:** A brand-new user can install and use codevira without
external help.

**How (be honest, fight the curse of knowledge):**
1. Pretend you have NEVER used codevira. Open the README.
2. Try the first install command shown. Does it work?
3. Now try to "make it work in Claude Code" using only what the docs say.
   - Stopwatch from `pipx install` to first useful AI answer.
   - Note every error message you hit.
   - Note every step where you had to guess what to do next.
4. Show the README to one person who has never used codevira. Ask them
   to explain back what codevira does. Note where they got it wrong.

**Pass criteria:**
- Time from install to first AI answer: < 2 minutes
- Zero "guess what to do next" steps
- The "explain back" person matches our positioning sentence

**Time:** 1 hr (your test) + 30 min (someone else's).

**Frequency:** Before GA. Re-run after any UX-affecting change.

---

## Angle 19 — i18n / Unicode Robustness

**What it tests:** Codevira handles Unicode in paths, project names,
fix descriptions, decisions, etc.

**How:**
1. Create a project at `~/Documents/Projects/プロジェクト/` (Japanese).
2. Create files with emoji in names: `🚀-launcher.py`, `café-app/`.
3. Write a fix description with RTL text: "إصلاح خلل في حلقة لانهائية".
4. Run codevira's full flow on this project: configure, init, index,
   register, doctor.

**Pass criteria:** Every command completes without UnicodeEncodeError /
UnicodeDecodeError. Decision log displays the description correctly.
Hooks fire correctly when Edit targets a file with emoji name.

**Time:** 1 hr.

**Frequency:** Pre-GA, then any time the path-handling code changes.

---

## Angle 20 — Filesystem Edge Cases

**What it tests:** Codevira works on different filesystems with
different semantics.

**How (test on each, time-boxed to 30 min each):**

1. **APFS case-insensitive** (macOS default): create files `Foo.py` and
   `foo.py` — should be treated as same file. Decisions on one should
   apply to the other.
2. **NFS mount**: install codevira's data dir on NFS share. Atomic
   replace via `os.replace` may not be atomic across NFS — verify
   `_atomic_write_text` behavior.
3. **Read-only mount**: try to run codevira in a project mounted RO.
   Should fail gracefully with a clear error, not crash with permission
   denied stack trace.
4. **Symlink loops**: create a symlink that points back to itself
   inside watched_dirs. Watcher should not infinite-loop.
5. **Encrypted volumes** (FileVault on macOS, LUKS on Linux): codevira
   data dir on encrypted volume — verify graph.db opens cleanly even
   when machine is unlocked mid-session.

**Pass criteria:** Each case either works correctly OR fails with a
clear error message. NEVER crash with raw stack trace.

**Time:** 2.5 hrs total.

**Frequency:** Once before GA. Re-run when paths.py changes.
