# Codevira reflection prompt — v1

You are reflecting on a slice of a software project's recent history
to produce a concise *abstraction* the next AI agent (or human
reader) can use to orient. The slice contains:

- Decisions recorded during the period (each: id, decision text, file
  context if any, tags).
- Sessions logged (each: task summary, task_type if set, outcome).

## Output

Respond with a single block in the following shape:

```yaml
abstraction: |
  <2-6 sentences. What pattern emerges across these decisions and
  sessions? What is the team's evolving stance? Avoid restating the
  facts — synthesize the higher-level *belief* or *trajectory*.>
tags: [<3-5 short topical tags>]
confidence: <0.0 to 1.0 — how strongly the input justifies this
             abstraction. Lower confidence = more inference; higher =
             clearly grounded in the source records.>
```

## Constraints

- Markdown is fine inside `abstraction:`, but keep it tight.
- Do not invent decisions, files, or commits not present in the
  source. If the input is too thin to support a pattern, return
  confidence < 0.3 and say so plainly in the abstraction.
- If the source records contain potential secrets that were stripped
  (you'll see `<redacted:*>` markers), do not try to guess what was
  there.
- Stay in this YAML output block. Do not preface or append commentary
  outside the fenced block.

## Source records

<<<SOURCE_CONTEXT>>>
