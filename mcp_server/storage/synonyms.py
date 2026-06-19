"""No-dependency synonym widening for FTS5 recall — E5 (Phase 23, part b').

The opt-in ``[semantic]`` embedding extra is DEFERRED (on-device model on
hold, D0000XY). This is the model-free complement the phase calls for: a small
CURATED code/dev-domain synonym map that widens a query so a concept asked in
one vocabulary still recalls a decision recorded in another — e.g. searching
"database" finds a decision about "postgres", or "auth" finds "login". Porter
stemming (already in the FTS5 tokenizer) handles morphology; this handles the
non-morphological relations stemming can't.

Off by default (``CODEVIRA_SYNONYM_WIDENING``) so the read surface is
unchanged unless opted in — widening trades some precision for recall, so the
default is the conservative one. Zero dependencies; ~O(1) per token.
"""

from __future__ import annotations

# Each set is a synonym group; every member expands to the whole group.
# Deliberately conservative + dev-domain — broad/ambiguous words are excluded
# so widening doesn't drown precision.
_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "auth",
            "authentication",
            "authorization",
            "login",
            "signin",
            "credential",
            "credentials",
        }
    ),
    frozenset({"db", "database", "sql", "sqlite", "postgres", "postgresql", "mysql"}),
    frozenset({"config", "configuration", "settings"}),
    frozenset({"async", "asynchronous", "await", "coroutine"}),
    frozenset({"env", "environment"}),
    frozenset({"dir", "directory", "folder"}),
    frozenset({"init", "initialize", "initialization", "bootstrap"}),
    frozenset({"repo", "repository"}),
    frozenset({"ts", "typescript"}),
    frozenset({"js", "javascript"}),
    frozenset({"fn", "func", "function", "method"}),
    frozenset({"var", "variable"}),
    frozenset({"err", "error", "exception", "failure"}),
    frozenset({"perf", "performance", "latency"}),
    frozenset({"ci", "pipeline", "workflow"}),
    frozenset({"deps", "dependencies", "dependency"}),
    frozenset({"docs", "documentation"}),
    frozenset({"pkg", "package"}),
    frozenset({"cache", "caching", "memoize", "memoization"}),
    frozenset({"lint", "linter", "linting"}),
    frozenset({"deploy", "deployment", "release"}),
    frozenset({"hash", "hashing", "digest"}),
    frozenset({"encrypt", "encryption", "crypto"}),
    frozenset({"token", "jwt"}),
    frozenset({"queue", "queuing"}),
    frozenset({"retry", "retries", "backoff"}),
    frozenset({"log", "logging", "logger"}),
    frozenset({"schema", "table"}),
)

# token → group (built once at import).
_BY_TOKEN: dict[str, frozenset[str]] = {}
for _g in _GROUPS:
    for _t in _g:
        _BY_TOKEN[_t] = _g


def expand(token: str) -> list[str]:
    """Return ``token`` plus its synonyms (``token`` first, then the rest of
    its group sorted for determinism). Just ``[token]`` if it has no group."""
    tl = (token or "").lower()
    group = _BY_TOKEN.get(tl)
    if not group:
        return [tl] if tl else []
    return [tl] + sorted(group - {tl})
