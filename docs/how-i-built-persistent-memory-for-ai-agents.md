# My AI Coding Agent Kept Starting from Zero. So I Built a Memory for It.

*How I went from burning thousands of tokens on orientation to actually getting things done — and what I open-sourced along the way.*

---

Let me describe a scene you might recognise.

You open Claude Code. You type a task. The agent starts reading files. `memory.md`. `journal.md`. `consumer.py`. `prompts.py`. `model_builder.py`. Five minutes later — it's still reading. The context indicator is climbing. It hasn't written a single line of code yet.

Then it makes a change. A reasonable-looking change. Except that file had a hard-won rule from three weeks ago — a decision that took 45 minutes to reason through and that you'd marked "do not change." The agent didn't know. It read the file, not the history.

You correct it. The context window is now 60% full. You haven't actually shipped anything.

This was my life by Phase 15 of building **UADP** — a data platform with 400+ files, 26 phases of development, and a codebase that had accumulated enough institutional knowledge to fill a small wiki. Every single agent session started from zero. Every session, the agent re-discovered the same things it had learned the session before.

I got frustrated enough to build something about it.

---

## The Real Cost Nobody Talks About

Everyone talks about token cost in terms of API pricing. That's real, but it's not the painful part.

The painful part is **wasted context window**.

Most AI coding tools give you a finite context window per session. Once it's full, the session is over. If you burn 10,000–18,000 tokens on orientation before the first line of code — reading files the agent has seen a dozen times before — you're eating into the window you actually need for the hard parts.

On a complex task, I was regularly hitting the context limit before the work was done. The session would end mid-way through a multi-file change. The next session would start from scratch, re-read everything again, and try to figure out where the previous session left off.

I tracked this for a few weeks. The pattern was consistent:

- **10,000–18,000 tokens** spent on orientation before any code was written
- **8–15 source files** read before the first edit
- About **1 in 3 sessions** running out of context before completing the task
- About **1 in 4 sessions** where the agent accidentally undid something I'd intentionally decided

That last one was the most demoralising. Not the wasted tokens — the *undo*. An agent touching a file it didn't fully understand and quietly reversing a decision that had taken a lot of effort to reach.

There had to be a better way.

---

## The Insight: Stop Making Agents Read, Make Them Query

I sat with the problem for a while before the solution became obvious.

The agent was reading `memory.md` not because it wanted to — it was reading it because that was the only way to understand the project state. There was no alternative. If you wanted to know "what phase is the project in?" you read the file. If you wanted to know "what are the rules for this file?" you read the file. If you wanted to know "what will break if I touch this?" you traced imports manually.

The insight: **agents shouldn't read files to understand the project. They should query structured data that describes the project.**

The same information. Completely different delivery mechanism.

Instead of:
```
Read memory.md         — 3,500 lines — ~10,000 tokens
Read journal.md        — 2,452 lines — ~7,000 tokens
Read consumer.py       — 300 lines   — ~1,000 tokens
Read prompts.py        — 400 lines   — ~1,400 tokens
... and 5 more files

Total: ~12,000–18,000 tokens just to get oriented
```

What if you could do this:
```
get_roadmap()     → "Phase 26, drift detection, no open changesets"      ~400 tokens
get_node(file)    → "role, rules, do_not_revert: true, key functions"   ~200 tokens
get_impact(file)  → "12 downstream files, 5 protected"                  ~300 tokens

Total: ~1,000–2,500 tokens — and the agent actually knows more
```

Same project knowledge. 75–85% fewer tokens. And critically — the structured artifacts contain *exactly* what the agent needs, not everything that was ever written.

That was the idea. Then I had to build it.

---

## What I Built

It took a few iterations to land on the right architecture. Here's what ended up working.

### The Context Graph — A Memory for Each File

The first thing I built was a YAML-based graph where every important file in the codebase gets a node. Not a summary. A *structured description* with specific fields an agent can act on.

```yaml
src/services/generator/prompts.py:
  role: "ALL LLM prompt templates for all generation passes"
  stability: medium
  do_not_revert: true
  key_functions:
    - build_pass2_attribute_prompt
    - build_semantic_prompt_v5
  connects_to:
    - target: src/services/generator/llm_strategy.py
      edge: consumed_by
  rules:
    - "Never drop the synonyms field — downstream indexer requires it"
    - "All attribute templates must include the synonyms array"
  last_changed_by: "Phase 17 — synonym generation fix"
```

When an agent calls `get_node("src/services/generator/prompts.py")`, it gets ~200 tokens of structured intelligence — the role, the rules it must respect, what it connects to, and whether it contains protected decisions.

When it calls `get_impact("src/services/generator/prompts.py")`, it gets a graph traversal — every file downstream of this one, how deep it goes, and which files are marked `do_not_revert`. Before touching a single line, the agent knows the blast radius.

That `do_not_revert` flag is the one that mattered most to me. Not technically impressive — just a boolean in a YAML file. But it's the difference between an agent that undoes your hard work once a week and one that almost never does.

Auto-generating the graph used to be the biggest barrier to adoption. I got tired of writing YAML by hand, so I added a `--generate-graph` flag to the indexer. It parses your Python files with AST, extracts imports to infer connections, pulls the first docstring line as the role, and creates a working stub for every file. Takes about 30 seconds. You enrich the important files over time — add rules, mark protected decisions — but the stubs work immediately.

---

### The Code Index — Search Instead of Read

The second component is a semantic search index over the codebase. ChromaDB under the hood, `sentence-transformers` for embeddings, built from AST-level chunks rather than arbitrary line windows.

The practical difference: you can ask `search_codebase("payment validation flow")` and get back the exact function that handles it — not a list of files that might contain something relevant. The agent finds the pattern it needs in one call instead of reading five files hoping to stumble across it.

The tricky part was keeping the index current.

My first version only re-indexed on commit. That sounds fine until you're in an active session — editing files, saving constantly, asking the agent questions — and you realise the agent is searching a version of your code from two hours ago. It finds "the right function" but sees outdated content. That's how you get hallucinations that look plausible: the agent is confidently wrong because it's working from stale information.

The fix was straightforward once I identified the problem: switch from git-based change detection to timestamp-based. Every file in the watched directories gets compared against a `.last_indexed` timestamp. Any file newer than that gets re-indexed. This catches every save — committed or not.

Three modes for keeping it current:
- **Incremental** (default): re-index only changed files, takes a few seconds
- **Post-commit hook**: auto-runs after every commit, completely silent
- **Watch mode**: real-time re-index on every save during active development

And when the agent itself detects a stale file via `get_node()`, it can call `refresh_index(["path/to/file.py"])` to self-heal without any human intervention. The graph tells the agent what it knows and what it doesn't.

---

### The Roadmap — Project State as Queryable Data

I had a `memory.md` file. 3,500 lines. Every session, the agent would read it to understand the project state. It worked, but it was expensive — and the agent would still miss things buried in the middle.

I replaced it with a structured YAML roadmap that agents can *query*.

```yaml
current_phase:
  number: 26
  name: "Context Graph Auto-Refresh"
  status: in_progress
  next_action: "Implement CI step to detect drifted graph nodes after merge"
  open_changesets: ["graph-refresh-ci"]

completed_phases:
  - phase: 23
    name: "Context Assembly Pipeline"
    completed: "2026-03-05"
    key_decisions:
      - "entity_id reconciliation: Qdrant uuid5 vs MetadataStore uuid4 resolved by name_to_id dict"
      - "isolated entities flagged but still returned — LLM decides what to do with them"
```

`get_roadmap()` returns the current phase, next action, and open changesets in ~400 tokens. `get_full_roadmap()` returns every completed phase with every key decision — the complete institutional memory of the project — in a structured, scannable form.

Agents can also *write* to it. When an agent discovers a gap during a session, it calls `add_phase()` and queues new work directly. When a phase is done, `complete_phase()` records the key decisions and advances to the next one. The roadmap stays current with the actual state of the project, not just what was planned at the start.

One quality-of-life addition: if `roadmap.yaml` doesn't exist when an agent calls `get_roadmap()`, it auto-creates a minimal Phase 1 stub. Zero setup needed. The first session just works.

---

### The Session Protocol — Making It Stick

Having the tools is one thing. Making sure agents actually use them is another.

The session protocol is a simple mandatory ritual — documented in `.agents/PROTOCOL.md`, referenced in `CLAUDE.md` so it's automatically loaded.

**Every session starts with:**
```
list_open_changesets()       → any unfinished work to pick up?
get_roadmap()                → where are we, what's next?
search_decisions("keyword")  → has this been decided before?
get_node(file)               → rules and constraints for files I'll touch
get_impact(file)             → what will break if I change this?
```

**Every session ends with:**
```
complete_changeset() or update_changeset_progress() with a blocker note
update_node() for each modified file
update_next_action("exact description for the next agent")
write_session_log(task, files_changed, decisions, ...)
```

The changeset system was a specific solution to a specific frustration. I kept hitting the context limit mid-way through multi-file changes. The session would end, and the next session had no idea what had been done and what was still pending. I'd end up redoing work or — worse — thinking I was done when half the files hadn't been updated yet.

The changeset is just a YAML file: which files are in scope, which are done, which are pending, and what decisions were made. When a session ends mid-work, it records a blocker note. The next session picks up the changeset and knows exactly where to continue. Simple, but it changed the multi-file experience entirely.

---

### The MCP Server — Connecting Everything to Your Tools

All of this is exposed through a local MCP (Model Context Protocol) server. One server that works with Claude Code, Cursor, Windsurf, Google Antigravity, and any other MCP-compatible tool.

33 tools total across six modules: graph, roadmap, changesets, search, adaptive learning, and code reader. One JSON config block and everything is available in your AI tool immediately.

The server runs entirely locally. No external services, no API keys, no data leaving your machine. ChromaDB is embedded, embeddings run on-device with `sentence-transformers`. Your code stays yours.

---

## The Honest Numbers

I tracked this across about 15 sessions on the UADP codebase after getting the framework stable. I want to be upfront: this is one codebase, one developer, my measurement methodology. Treat these as directional, not benchmarks.

| Metric | Before | After | Change |
|---|---|---|---|
| Tokens spent on orientation | 12,000–18,000 | 1,000–2,500 | **~75–85% less** |
| Source files read before first edit | 8–15 | 0–2 | **~85% less** |
| Sessions hitting context limit before completion | ~1 in 3 | ~1 in 10 | **much less frequent** |
| Agent accidentally reversing a protected decision | ~1 in 4 sessions | ~1 in 20 sessions | **rare now** |
| Multi-file changes successfully continuing next session | ~30% | ~85–90% | **dramatically better** |

The orientation token reduction (75–85%) is the most structural number — it's essentially fixed by how much information is in the graph vs. the files. The specific percentage varies based on how large your files are and how well-written your graph nodes are.

The context window exhaustion improvement is the one I felt most in day-to-day work. Not running out of context before a task is done changes how you plan sessions entirely. You stop dreading complex multi-file changes.

The blast radius protection is the one I care about most. Going from "roughly 1 in 4 sessions, the agent undoes something intentional" to "this almost never happens" — that's not a metric, that's peace of mind.

---

## What Broke Along the Way

I want to be honest about this part too, because it's where the interesting learning happened.

**The search wasn't finding the right things at first.**

Early on, a search for "synonym embedding flow" would return generic LLM utilities instead of the specific pipeline file I needed. The problem was chunking granularity: I was embedding whole files as single chunks, so the embedding was an average over hundreds of lines. Switching to function-level chunking — one chunk per function or class — fixed this completely. Now a search for a specific pattern finds the specific function that implements it.

**The stale index problem almost broke everything.**

This was the most dangerous failure mode because it was invisible. During active development, I'd be editing files, saving frequently, asking the agent questions — and the agent was searching a version of my code from hours ago without knowing it. It would confidently cite functions that had been refactored. This is exactly how AI hallucinations happen in a coding context: not making things up, but working from outdated information with full confidence.

The fix — switching from commit-based to timestamp-based change detection — sounds simple in retrospect. It wasn't obvious until I traced a specific wrong answer back to its root cause.

**The graph went stale after the first week.**

I refactored a few files and forgot to update the graph nodes. Suddenly `get_node()` was returning descriptions that no longer matched reality. The discipline of calling `update_node()` at session end is easy to skip when you're tired and just want to close the session. I made it a mandatory last step in the protocol — and added the staleness signal to `get_node()` so agents at least know when the code index is outdated for a file, even if the graph YAML itself needs human attention.

**The MCP SDK upgraded and silently broke everything.**

One day the server started failing the handshake with the AI tool. No error in the output — just silent connection failure. After half a session of debugging, the cause: MCP SDK 1.26.0 changed `stdio_server` from a coroutine to an async context manager. The fix was three lines of code. The diagnosis was what cost the time. If you're building your own MCP server, the currently correct pattern is:

```python
async def _run():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

asyncio.run(_run())
```

---

## Is This Worth It For Your Project?

Honestly, it depends on where you are.

If your codebase is small — under 50 files, early stage, few interdependencies — a well-written `CLAUDE.md` file is probably enough. The maintenance overhead of a context graph doesn't pay back at small scale. Write good comments, keep a `CLAUDE.md` updated, and you'll be fine.

The framework starts earning its keep when:

- You have 50+ files with real interdependencies
- You're running multiple agent sessions per week
- You have accumulated decisions that must survive across sessions
- You've been burned by an agent undoing intentional choices
- You're working across more than one AI tool on the same codebase

At that point, the overhead of maintaining the graph and running the session protocol is genuinely small compared to the frustration it eliminates.

---

## How to Try It

The framework is now open source as **[Codevira MCP](https://github.com/sachinshelke/codevira)** — MIT licensed, drop it into any project.

```bash
# Clone into your project's .agents/ directory
git clone https://github.com/sachinshelke/codevira .agents

# Install dependencies
pip install -r .agents/requirements.txt

# Configure for your project
cp .agents/config.example.yaml .agents/config.yaml
# Edit: set watched_dirs, language, file_extensions

# Build everything in one command
python .agents/indexer/index_codebase.py --full --generate-graph --bootstrap-roadmap

# Auto-reindex on every commit
bash .agents/hooks/install-hooks.sh
```

Add this to `.claude/settings.json` (or your tool's MCP config):
```json
{
  "mcpServers": {
    "codevira": {
      "command": "python",
      "args": [".agents/mcp-server/server.py"]
    }
  }
}
```

Add four lines to your `CLAUDE.md` or `.cursorrules`:
```
At session start: call list_open_changesets(), get_roadmap(), get_node(), get_impact()
before touching any file.
At session end: call complete_changeset(), update_node(), update_next_action().
```

Try it for a week. Watch the context window usage. See how often the agent runs out of context before finishing. See how often it reverses something you didn't want reversed.

---

## What the Rest of the Ecosystem Is Doing

Before publishing this, I searched GitHub, PyPI, arXiv, and the MCP server registry (~8,600 servers in PulseMCP) to see what already exists. My honest assessment:

**[CogniLayer](https://github.com/LakyFx/CogniLayer)** is the most complete single tool I found — 17 tools, hybrid vector+FTS5 search, crash recovery, SHA-256 hash verification for deployment safety. If you want something ready today without building your own, start here. What it doesn't have: per-file YAML graph nodes, `do_not_revert` flags, BFS blast radius traversal, or a queryable roadmap.

**[Axon](https://github.com/harshkedia177/axon)** has the best blast-radius implementation I found. BFS traversal with confidence scores (1.0 = direct dependency, 0.8 = receiver, 0.5 = fuzzy). No context graph, no search, no session tracking — but if blast-radius awareness is the one thing you need, Axon is excellent.

**[Aura](https://github.com/Naridon-Inc/aura)** is the only tool I found that tackles the "agent reverting protected decisions" problem. It hard-blocks a commit if the AI's stated intent doesn't match what it actually changed. The right instinct — but at commit time, not edit time. Codevira surfaces the protection flag *before the edit*, which prevents the mistake rather than catching it afterward.

**[CONTINUITY](https://lobehub.com/mcp/duke-of-beans-continuity)** handles the session memory problem with 8 tools for decision tracking and crash recovery. No codebase awareness — it remembers decisions from conversation, not which files those decisions apply to.

The overall picture:

| Tool | Context Graph | Semantic Search | Session Tracking | Queryable Roadmap | Blast Radius |
|---|---|---|---|---|---|
| **Codevira MCP** | ✅ | ✅ | ✅ | ✅ | ✅ |
| CogniLayer | Partial | ✅ | ✅ | ❌ | Partial |
| Axon | ❌ | ❌ | ❌ | ❌ | ✅ |
| Aura | ❌ | ❌ | Partial | ❌ | ❌ |
| CONTINUITY | ❌ | ❌ | ✅ | ❌ | ❌ |

The gap that surprised me most: nobody has built a queryable project roadmap. Every team I've seen uses a markdown file or a Notion doc — read by humans, invisible to agents. Making the roadmap a structured artifact that agents can query, update, and extend inline is one of the highest-leverage improvements in Codevira, and I haven't seen anyone else do it.

---

## What's Coming Next

Codevira v1.0 covers Python fully. The next priorities:

**v1.1** — Language expansion. Tree-sitter integration to bring `get_signature`, `get_code`, and auto-generated graph stubs to TypeScript, Go, and Rust. All the other tools already work for any language; this closes the Python-only gap on AST features.

**v1.2** — Better developer experience. A `codevira` CLI (`codevira init`, `codevira status`) and a VS Code extension for one-click MCP server setup without editing JSON manually.

**v1.3** — Graph auto-maintenance. A lightweight CI check that compares `git diff` against graph node connections after each merge and flags nodes that may have drifted — so maintenance debt surfaces before it becomes silent errors.

Full roadmap: [ROADMAP.md](../ROADMAP.md)

---

## The Part That Surprised Me Most

When I started this project, I framed it as a token efficiency problem. Save tokens, save money, ship faster.

That framing is correct, but it undersells what actually changes.

The bigger shift is how you feel about starting a session. Before — there was always a low-level anxiety about whether the agent would remember what mattered, whether it would undo something, whether the session would run out before the task was done. You'd spend mental energy double-checking the agent's work for things it might have missed.

After — you start a session, the agent orients itself in seconds, and you mostly just do the work. The context window exhaustion problem becomes rare. The "agent undid my decision" problem becomes rare. You start trusting the sessions more because the sessions are more consistent.

That shift from supervision to collaboration — that's what the tooling actually buys you. The token reduction is how you measure it. The feeling is why it matters.

---

*Codevira MCP is open source under the MIT license. The [GitHub repo](https://github.com/sachinshelke/codevira) has the full setup guide, 26-tool reference, agent persona definitions, and contribution guide. If you try it and have results — good or bad — share them in [GitHub Discussions](https://github.com/sachinshelke/codevira/discussions). The numbers above are from one project; the more data points from different codebases, the better picture we all get.*
