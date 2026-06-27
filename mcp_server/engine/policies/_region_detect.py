"""
_region_detect.py — which named symbol(s) does an edit land in?

This is the predicate symbol/region-level decision locking uses. v3.5.0's
content-aware lock (Phase 18) downgrades a block→warn when the diff's salient
tokens don't reference a locked decision's subject. Symbol-level locking goes
one step finer: a decision can be scoped to a *function or class* (not the
whole file), and the lock then blocks only when the edit actually lands inside
that symbol.

The crux: at ``PRE_TOOL_USE`` the edit has NOT been applied yet, so the target
file on disk still contains the edit's ``before`` text. We can therefore locate
that text as a substring, turn its offset into a 1-based line span, and ask
which AST/tree-sitter symbol ranges overlap it.

Pure-ish: one read of the target file, no writes, no graph DB. Defensive
everywhere — a parse failure, an unreadable file, or a ``before`` block that
can't be located returns ``None`` ("undeterminable"), and the caller falls back
to file-level behavior. Never raises.

Determinate-empty vs undeterminable
------------------------------------
``symbols_touched_by_edit`` distinguishes two "no match" cases:

* ``set()``  — determinate: the edit lands in module-level code that belongs to
  no named symbol (e.g. imports, top-level constants). The caller may treat a
  symbol-scoped decision as orthogonal.
* ``None``   — undeterminable: no diff envelope, ``before`` not found, parse
  failure, full-file Write, oversized input. The caller must NOT relax on this.
"""

from __future__ import annotations

import ast
from pathlib import Path

from mcp_server.engine.policies._signature_detect import parse_diff

#: Cap on the inputs we will analyze. A pathological multi-MB file or diff
#: bails to ``None`` rather than spending unbounded CPU. Real source files +
#: edits are KB.
_MAX_BYTES = 1_000_000


def _python_symbols(source: str) -> list[tuple[str, int, int]]:
    """All named symbols (functions, async functions, classes — including
    nested and private) as ``(name, start_line, end_line)``, 1-based.

    Nested defs are included so an edit inside a method maps to BOTH the
    method and its enclosing class (a decision scoped to either matches).
    """
    out: list[tuple[str, int, int]] = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # ast.lineno points at the `def`/`class` line, EXCLUDING decorators.
            # A decorator is often the load-bearing contract (@app.route,
            # @lru_cache, @require_auth) a symbol-scoped lock means to protect,
            # so extend the start up to the first decorator line.
            start = node.lineno
            decorators = getattr(node, "decorator_list", None) or []
            if decorators:
                start = min(start, min(d.lineno for d in decorators))
            end = getattr(node, "end_lineno", None) or node.lineno
            out.append((node.name, start, end))
    return out


def _treesitter_symbols(path: Path, ext: str) -> list[tuple[str, int, int]] | None:
    """Symbol ranges for a tree-sitter-supported language (TS/JS/Go/Rust),
    or ``None`` if the extension isn't supported / parsing is unavailable."""
    from indexer.treesitter_parser import get_language, parse_file

    lang = get_language(ext)
    if lang is None:
        return None
    parsed = parse_file(str(path), lang)
    symbols = [
        (s.name, s.start_line, s.end_line)
        for s in parsed.symbols
        if s.name and s.start_line and s.end_line
    ]
    # tree-sitter is error-tolerant: a syntactically broken file (mid-refactor)
    # parses to a partial/empty symbol list. Unlike Python's ast (which raises →
    # None → conservative), that would yield a determinate-empty set and RELAX a
    # symbol-scoped lock to a warn. Treat "no symbols extracted" as
    # undeterminable (None) so the broken-file case stays conservative.
    return symbols or None


def _extract_symbols(path: Path, source: str) -> list[tuple[str, int, int]] | None:
    """Named symbols with 1-based line ranges for ``path``, or ``None`` when
    the language isn't supported or extraction fails. Never raises."""
    ext = path.suffix.lower()
    try:
        if ext in (".py", ".pyi"):
            return _python_symbols(source)
        return _treesitter_symbols(path, ext)
    except Exception:  # noqa: BLE001 — advisory predicate, never break the caller
        return None


def _edit_line_span(source: str, before: str) -> tuple[int, int] | None:
    """1-based ``(start_line, end_line)`` of ``before`` within ``source``,
    or ``None`` if it can't be located.

    Returns ``None`` if ``before`` is absent OR NON-UNIQUE. A non-unique anchor
    means we can't know which occurrence the edit targets, so the located span
    (and the symbol derived from it) would be a guess — and a wrong guess could
    mis-attribute the edit to the locked symbol (false block) or away from it
    (bypass). Undeterminable is the safe answer; the caller falls back to
    file/token logic. (Claude Code's Edit guarantees a unique old_string, but
    other MCP clients / MultiEdit do not.)
    """
    idx = source.find(before)
    if idx == -1:
        return None
    if source.find(before, idx + 1) != -1:
        return None  # non-unique anchor → can't attribute to one symbol
    start_line = source.count("\n", 0, idx) + 1
    # end line = start + number of newlines spanned by the matched text
    # (rstrip so a trailing newline in `before` doesn't over-count the span).
    span_newlines = before.rstrip("\n").count("\n")
    return start_line, start_line + span_newlines


def symbols_touched_by_edit(
    target_file: Path, diff_text: str | None
) -> set[str] | None:
    """Names of the symbol(s) whose source range overlaps the edit, or ``None``
    when that can't be determined.

    Args:
        target_file: the file being edited (read at PRE_TOOL_USE, so it still
            holds the ``before`` text).
        diff_text: the codevira ``--- before / --- after`` envelope.

    Returns:
        * ``set[str]`` of overlapping symbol names — possibly empty (determinate:
          the edit is in module-level code outside any symbol).
        * ``None`` if undeterminable: no/oversized/unparseable diff, a pure
          insertion (no ``before`` anchor), ``before`` not found in the file,
          an unreadable file, or an unsupported / unparseable language.

    Never raises.
    """
    try:
        if not diff_text or len(diff_text) > _MAX_BYTES:
            return None
        before, after = parse_diff(diff_text)
        if before is None or after is None:
            return None
        if not before.strip():
            # Pure insertion has no existing anchor to locate — let the caller's
            # insertion path handle it.
            return None

        source = target_file.read_text(encoding="utf-8", errors="replace")
        if len(source) > _MAX_BYTES:
            return None

        span = _edit_line_span(source, before)
        if span is None:
            return None
        start_line, end_line = span

        symbols = _extract_symbols(target_file, source)
        if symbols is None:
            return None

        # Overlap test: a symbol [s, e] is touched unless it lies entirely
        # before or entirely after the edit's [start, end] span.
        return {
            name for (name, s, e) in symbols if not (e < start_line or s > end_line)
        }
    except Exception:  # noqa: BLE001 — never break the hook on a detection bug
        return None
