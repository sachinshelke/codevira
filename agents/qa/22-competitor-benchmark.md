# QA Angle 22 — Competitor Benchmark

**Subagent type:** Explore (with WebFetch)
**Time-box:** 1 hr per competitor
**Catches:** Best-practice gaps the implementer didn't know about

## Prompt

```
Compare codevira's {scope} to {competitor}'s equivalent implementation.

Competitor candidates for AI memory tools:
  - claude-mem (https://github.com/thedotmack/claude-mem)
  - MemPalace (https://github.com/.../mempalace)
  - Mem0 (https://github.com/mem0ai/mem0)
  - Väinämöinen (https://github.com/.../vainamoinen)
  - memsearch by Zilliz

For each, examine:

1. **Architecture choices** — daemon vs stdio vs hooks-only?
   How do they handle multi-project? Multi-IDE?

2. **Lifecycle hook integration** (if applicable) — what events do
   they hook? What's their JSON output shape? Are they doing anything
   we're not?

3. **Memory model** — full session text? Summaries? Decision-only?
   Vector search? Token-efficiency strategy?

4. **Configuration** — How does the user install? Configure?
   Per-project vs global? Snippet-based vs auto-injection?

5. **Observability** — how does the user know it's working? See what
   it's doing? Debug when broken?

6. **Failure modes** — what do they do on engine errors? Permission
   denied? Database corrupt? Network down?

7. **Performance** — claimed/measured numbers? Cold-start cost?
   Per-event overhead? Their full-stack hook latency?

8. **Documentation maturity** — README quality, install guide,
   troubleshooting docs, FAQ.

For each comparison:
  **[ADVANTAGE - codevira]**: {what we do better}
  **[GAP - codevira]**: {what they do better; should we adopt?}
  **[NEUTRAL]**: {different choices, both valid}

Output the gaps as a prioritized list of "things to consider for v2.x":
each with severity + estimated effort + reasoning.

Don't copy their implementation; understand the WHY behind their
choices and decide what fits codevira's vision (per-project memory,
multi-IDE, decision protection, local-first, MIT).

Cap at 1500 words.
```

## Trigger

Once per major release before GA. Catches "we missed an obvious
pattern" — e.g., R5 was inspired by claude-mem's lifecycle-hook
integration that we hadn't fully studied.

## Expected output

3–8 gaps, ranked. Most actionable: small UX/observability things
that compound into noticeably better polish vs. competition.
