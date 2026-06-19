# Antigravity — torch dlopen sandbox issue (resolved in v2.2.0)

**Status: no longer applicable.**

This page documented a macOS hardened-runtime sandbox failure where Google
Antigravity could not `dlopen()` PyPI's unsigned `torch` dylibs, which
degraded Codevira's semantic search to keyword-only and (in the worst case)
failed the `tools/list` step entirely.

**v2.2.0 removed semantic search entirely** — there is no ChromaDB, no
sentence-transformers, and no `torch`, so Codevira ships **no native ML
dylibs**. Decision search is pure SQLite FTS5 (keyword + BM25). The dlopen
failure described here can no longer occur, and Codevira works fully in
Antigravity like any other MCP client — no workaround needed.

If you hit a *different* native-library load error in Antigravity (for
example a tree-sitter grammar), please open an issue with the full
`tools/list` error output.

_The original analysis is preserved in the v2.1.2 / v2.2.0 CHANGELOG entries
and [Issue #10](https://github.com/sachinshelke/codevira/issues/10)._
