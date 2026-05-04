# Codevira vs other AI memory tools

Honest comparison vs the agent-memory tools shipping in 2026. Not marketing — for users picking one tool, sized to fit on one HN comment.

## TL;DR

**Pick codevira if you**: code with AI agents, switch between Claude Code / Cursor / Windsurf / Antigravity on the same project, want decision protection (`do_not_revert`) + outcome scoring + scope locks, and want it all local-first with no signup.

**Pick something else if you**: need cross-process memory across machines (Mem0 cloud), need temporal knowledge graphs at enterprise scale (Zep), or want the highest LongMemEval benchmark score (MemPalace).

## The matrix

| Tool | Pitch | Coverage | Local-first | Decision protection | Cross-tool memory per project | LongMemEval |
|---|---|---|---|---|---|---|
| **codevira** | One memory layer for every AI coding tool | 7+ tier-1 IDEs, AGENTS.md fallback for any MCP client | ✅ MIT, no signup | ✅ `do_not_revert` + 10 hero policies | ✅ **the wedge** | not benchmarked (coding-specific data) |
| Mem0 | General agent memory (managed cloud + OSS) | Cloud SDK; user wires up | ⚠ self-host OSS or cloud | ❌ | ❌ (cross-session, not cross-tool) | published |
| claude-mem | Auto-captures Claude Code sessions | **Claude Code only** | ✅ | ❌ | ❌ (single-tool) | not published |
| MemPalace | 96.6% LongMemEval, 28 MCP tools | Mostly Claude Code | ⚠ local but heavy | ❌ | ❌ | **96.6%** (state-of-art) |
| MemClaw / MemoClaw | Per-workspace isolated memory | Claude Code | ✅ | ❌ | ⚠ per-workspace, not per-project-cross-tool | not published |
| Zep | Enterprise temporal knowledge graph | SDK | ⚠ self-host or cloud | ❌ | ❌ | published |
| Väinämöinen | Filesystem markdown, zero deps | Claude Code | ✅ | ❌ | ❌ | not published |
| memsearch (Zilliz) | Markdown + Milvus | SDK | ⚠ requires Milvus | ❌ | ❌ | not published |

## What codevira does that nothing else does

### 1. Per-project memory across **every** AI tool

The wedge. You're working on Project A. You open Claude Code, ask "what did I decide about retries last week?" Then close Claude Code, open Cursor on the same project, ask the same question — **same answer**. Switch to Windsurf — same answer. Antigravity — same answer.

Then open Project B. Different memory.

This isn't "universal memory across all your projects" — that's a different (lesser) thing. Each project has its own context, decisions, roadmap, graph. Each AI tool you use sees that one project's memory.

claude-mem nails this for Claude Code specifically, and nothing else. Mem0 has cross-session memory but not cross-tool. **Codevira's wedge is per-project, multi-tool.**

### 2. Decision protection (`do_not_revert`)

Hero 1 (Decision Lock): mark a decision as locked, and codevira refuses any AI edit that would undo it. Comes with a searchable decision log.

> "Last week you debugged a retry policy for 3 hours. Today's AI session refactors it to a simpler version because it has no idea why the complexity exists."

Codevira blocks that with a clear message + the original reasoning. **No other tool in this matrix has this.**

### 3. AI guardians (10 of them)

Beyond `do_not_revert`, codevira intercepts every AI tool call with 10 policies that work as a single engine:

| Trigger | Policy |
|---|---|
| Before Edit | Decision Lock, Anti-Regression Memory, Blast-Radius Veto, Scope Contract Lock |
| User submits a prompt | Cross-Session Consistency (surfaces past decisions), Proactive Intent Inference (pre-fetches relevant fixes / impact / outcomes) |
| After Edit | Live Style Enforcement (snake_case vs camelCase, quote-style, indent — vs your project's preferences) |
| Session start | AI Promotion Score (top stable decisions injected) |
| Stop | Token Budget logged |
| On demand | Decision Replay (timeline browser) |

This is the "active guardian" model. Claude-mem captures sessions. Mem0 retrieves. Codevira **intervenes**.

### 4. Token-efficient by design

`get_session_context()` is a single ~500-800 token call that catches any AI agent up. Tools return summaries by default; opt-in to full data. MemPalace returns 4-8K tokens of context per call by comparison.

### 5. Local-first, MIT, no signup

No cloud account. No API key. No data leaves your machine. Mem0's cloud has signup. Zep is enterprise-positioned. Codevira is `pipx install codevira && codevira setup` — two commands, two minutes.

## What other tools do better

Honest:

- **MemPalace** has higher LongMemEval scores. Their 28 MCP tools are more capable for general agent memory. If you want maximum benchmark performance regardless of token cost, pick MemPalace.
- **Mem0** has a polished managed service if you want zero local setup.
- **Zep** has enterprise-grade temporal reasoning if you're at a company that needs that.
- **claude-mem** has the most mature Claude-Code-specific session capture.

## Choosing

```
Want decision protection + scope locks?         → codevira
Need it to work across Claude/Cursor/Windsurf?   → codevira
Want max LongMemEval score?                      → MemPalace
Need cloud + non-coding memory?                  → Mem0
Need enterprise temporal graph?                  → Zep
Only use Claude Code, want zero-config capture?  → claude-mem (or codevira)
```

## When NOT to use codevira

Be honest about this. Codevira is **not** the right fit if:

- You need cross-machine memory (your laptop + your colleague's laptop seeing the same memory). Codevira is single-machine.
- You're not coding — codevira's tools are coding-specific (graph, decisions, blast-radius). For general-purpose AI memory pick Mem0 or LangMem.
- You want a managed service. Codevira is local-first, no SaaS.
- You only use one AI tool and one IDE forever. The cross-tool wedge is the value-prop; if you don't switch tools, claude-mem (Claude Code) is simpler.

## Star history disclosure

Codevira is **early** (~10 stars at v2.0). Mem0, MemPalace, Zep are weeks-to-months ahead in user base. The trade-off: codevira ships features the others don't (decision protection, scope locks, cross-tool wedge) at the cost of less polish + smaller community.

If you try codevira and hit a bug, file it on GitHub. Response time is hours, not days.

## Sources

- [Memory tool comparison: Mem0 vs Zep vs LangMem vs MemoClaw](https://dev.to/anajuliabit/mem0-vs-zep-vs-langmem-vs-memoclaw-ai-agent-memory-comparison-2026-1l1k)
- [Source-code comparison: Väinämöinen vs MemPalace vs claude-mem](https://dev.to/vainamoinen/vainamoinen-vs-mempalace-vs-claude-mem-a-source-code-level-comparison-of-ai-agent-memory-systems-4bk4)
- [claude-mem](https://github.com/thedotmack/claude-mem)
- [Mem0](https://mem0.ai)
- [Zep](https://www.getzep.com)
- [MemPalace](https://github.com/mempalace/mempalace) (assumed handle; check)
- [AGENTS.md spec](https://agents.md/) — the Linux Foundation cross-tool standard codevira's tier-2 fallback uses
