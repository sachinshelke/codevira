"""
mcp_server.storage — v2.2.0 lean storage layer.

In v2.2.0 codevira's data authority moved from ``~/.codevira/projects/<key>/
graph.db`` (binary SQLite blob, machine-local) to ``<repo>/.codevira/``
(JSONL + YAML, repo-committed, human-readable, team-shareable via git).

This package provides the read/write primitives:

- ``jsonl_store`` — atomic append-only writes + line-by-line reads with
  file locking. Used for decisions/outcomes/sessions/changesets/etc.
- ``token_estimator`` — fast char-based proxy for token-budget enforcement
  in the injection hook (within ~10% of tiktoken on English+code).
- ``digest`` — generate slim ``digest.jsonl`` from ``decisions.jsonl``.
- ``manifest`` — read/write ``manifest.yaml`` tag/file → ids index.
- ``fts5_index`` — SQLite FTS5 index over decisions.jsonl for fast keyword
  search.
- ``agents_md_generator`` (Phase D) — render AGENTS.md from decisions
  with hard 5 KB cap + marker preservation.
- ``outcomes_writer`` (Phase F) — git observer that classifies decisions
  as kept/modified/reverted.

Why JSONL + YAML and not SQLite for the source-of-truth tier:

- ``git diff`` shows decision changes as line diffs (PR-reviewable)
- No binary blob in the repo
- Append-only writes are merge-conflict-resistant
- Other AI tools can read the JSONL without any codevira dependency
- Codevira's cache (``.codevira-cache/``) remains SQLite for query speed

The cache is rebuilt from the source-of-truth files; sources never depend
on cache state. If cache is deleted, ``codevira sync`` regenerates it.
"""

from __future__ import annotations

__all__ = [
    "jsonl_store",
    "token_estimator",
    "digest",
    "manifest",
    "fts5_index",
]
