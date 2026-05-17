# Docs-Only Fixture

This fixture represents a documentation-only repository — no code,
just markdown and JSON config. It mimics the `lh-interface` project
shape that surfaced Bug E (codevira's indexer silently produces 0
chunks for any project without parseable source code).

## Why this fixture matters

When codevira's indexer runs against a docs-only repo, the chunker
expects tree-sitter parsers for every file. Markdown has no tree-
sitter chunker today, so files get filtered out at the chunking
stage. The result: 0 chunks, with no warning to the user.

A pass on this fixture means either:

1. The markdown chunker (Bug E fix) lands and produces > 0 chunks, OR
2. The indexer fails LOUDLY with a message like "this project has no
   parseable code; codevira indexes code, see docs/limitations.md"

A silent 0-chunk result is a fail.

## What's in this fixture

- This README.md
- A few additional markdown files
- A package.json (config)
- No source code in any language

This matches the shape of many real-world docs-only repos.
