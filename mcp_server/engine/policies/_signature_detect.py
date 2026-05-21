"""
_signature_detect.py — does a code diff modify a public signature?

This is the predicate Hero 4 (Blast-Radius Veto) uses to decide whether
to escalate from "edit a high-impact file" to "block — public signature
changed and N callers depend on it." Pure functions, no I/O — testable
in isolation.

Diff format
-----------

The wiring layer (Claude Code hooks) produces the same shape codevira
already uses elsewhere:

    --- before
    <old text>
    --- after
    <new text>

We extract every line matching a per-language signature regex from each
block, then compare. Any difference → signature change.

If the diff doesn't follow that format, ``parse_diff`` returns
``(None, None)`` and ``change_touches_signature`` returns False
("don't false-block on un-parseable input").

Languages
---------

v2.0-alpha covers Python, JS/TS, Go, Rust, Java/C# (best-effort). Each
language has its own regex. The regexes are intentionally conservative
— we'd rather miss a signature change (false negative → policy
no-ops) than block a body-only change (false positive → user friction).

A "language" is detected by the file extension passed to
``change_touches_signature``. Unknown extensions fall back to "any
language" (the union of all regexes), which is also conservative.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------

#: Anchored regex for the codevira diff envelope. Matches the same shape
#: indexer/fix_history.py:_EDIT_FORMAT_RE matches — keep these in sync
#: if either changes.
_EDIT_FORMAT_RE = re.compile(
    r"^--- before\n(?P<before>.*?)\n^--- after\n(?P<after>.*)\Z",
    re.DOTALL | re.MULTILINE,
)


def parse_diff(diff_text: str) -> tuple[str | None, str | None]:
    """Return ``(before_block, after_block)`` for a codevira-format diff.

    Returns ``(None, None)`` if the input doesn't match the expected
    envelope. Conservative — better to skip the policy than to act on
    a malformed diff.
    """
    if not diff_text:
        return None, None
    match = _EDIT_FORMAT_RE.match(diff_text)
    if not match:
        return None, None
    return match.group("before"), match.group("after")


# ---------------------------------------------------------------------
# Per-language signature regexes
# ---------------------------------------------------------------------

# Each pattern is anchored to start-of-line (with optional leading
# whitespace) so we don't match inside strings or comments unless the
# string/comment IS the whole line. The capture group is the
# **canonicalized** form we compare across before/after.
#
# Why we don't strip strings/comments first: the cost is real (need a
# language-aware tokenizer for accurate stripping) and the false-positive
# rate for "signature inside a string literal" is extremely low in real
# code. Documented as a known limitation; see the Risks section of
# docs/heroes/04-blast-radius.md.

_SIG_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "python": [
        # def / async def — capture name + paren-open so renaming or
        # parameter changes both register as different sig lines.
        re.compile(r"^[ \t]*(?:async\s+)?def\s+\w+\s*\("),
        re.compile(r"^[ \t]*class\s+\w+(?:\s*\([^)]*\))?\s*:"),
    ],
    "javascript": [
        re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+\s*\("),
        re.compile(r"^[ \t]*(?:export\s+)?class\s+\w+"),
        # const foo = (...) => — arrow function assigned to const at
        # module level.  Common pattern in modern TS.
        re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:async\s+)?(?:\(|function)"),
    ],
    "typescript": [
        re.compile(r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\w+\s*[<(]"),
        re.compile(r"^[ \t]*(?:export\s+)?(?:abstract\s+)?class\s+\w+"),
        re.compile(r"^[ \t]*(?:export\s+)?interface\s+\w+"),
        re.compile(r"^[ \t]*(?:export\s+)?type\s+\w+\s*="),
        re.compile(r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+\w+\s*[:=]"),
    ],
    "go": [
        # `func name(...)` and `func (recv) name(...)`
        re.compile(r"^[ \t]*func\s+(?:\([^)]+\)\s+)?\w+\s*\("),
        re.compile(r"^[ \t]*type\s+\w+\s+(?:struct|interface)\b"),
    ],
    "rust": [
        re.compile(r"^[ \t]*(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+\w+\s*[<(]"),
        re.compile(r"^[ \t]*(?:pub(?:\([^)]+\))?\s+)?struct\s+\w+"),
        re.compile(r"^[ \t]*(?:pub(?:\([^)]+\))?\s+)?enum\s+\w+"),
        re.compile(r"^[ \t]*(?:pub(?:\([^)]+\))?\s+)?trait\s+\w+"),
    ],
    "java": [
        # Best-effort: any method/class declaration with access modifier.
        # Not perfect — Java's syntax is verbose — but catches the common
        # cases without false-positiving on ``return foo;``.
        re.compile(r"^[ \t]*(?:@\w+\s+)*(?:public|protected|private|abstract|static|final|synchronized|\s)*\s+(?:class|interface|enum)\s+\w+"),
        re.compile(
            r"^[ \t]*(?:@\w+\s+)*(?:public|protected|private)\s+"
            r"(?:static\s+|final\s+|abstract\s+|synchronized\s+|native\s+)*"
            r"[\w<>,\s\[\]]+\s+\w+\s*\("
        ),
    ],
    "csharp": [
        re.compile(r"^[ \t]*(?:\[[^\]]+\]\s*)*(?:public|protected|private|internal)\s+(?:static\s+|abstract\s+|virtual\s+|sealed\s+|override\s+|async\s+|\s)*(?:class|interface|struct|enum|record)\s+\w+"),
        re.compile(
            r"^[ \t]*(?:\[[^\]]+\]\s*)*(?:public|protected|private|internal)\s+"
            r"(?:static\s+|abstract\s+|virtual\s+|sealed\s+|override\s+|async\s+)*"
            r"[\w<>,\s\[\]?]+\s+\w+\s*\("
        ),
    ],
}

#: File extension → language key in ``_SIG_PATTERNS``. Lowercase; the
#: lookup normalizes input.
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
}


def language_for_path(path: str | None) -> str | None:
    """Return the language key for a file path's extension, or None.

    None means "we don't have a per-language regex set." The caller
    can fall back to "any language" by passing ``None`` to
    ``change_touches_signature``.
    """
    if not path:
        return None
    # Find the LAST dot — handles ``foo.spec.ts`` correctly.
    idx = path.rfind(".")
    if idx == -1:
        return None
    ext = path[idx:].lower()
    return _EXT_TO_LANG.get(ext)


# ---------------------------------------------------------------------
# Public predicate
# ---------------------------------------------------------------------


def signature_lines(text: str, language: str | None) -> list[str]:
    """Return all signature-matching lines from ``text``, in order.

    If ``language`` is None, runs every regex (the union — broader but
    safer for unknown languages). If ``language`` is unrecognized,
    same fallback.

    Each match is the LINE containing the signature, stripped of
    trailing whitespace. We compare lines (not just the matched span)
    because a line like ``def foo(a, b):`` vs ``def foo(a, b, c):``
    differs in the FULL line content even though the regex prefix
    matches both.
    """
    if not text:
        return []

    patterns: list[re.Pattern[str]]
    if language and language in _SIG_PATTERNS:
        patterns = _SIG_PATTERNS[language]
    else:
        # Union of all patterns — for unknown languages or "be conservative."
        patterns = [p for plist in _SIG_PATTERNS.values() for p in plist]

    out: list[str] = []
    for line in text.splitlines():
        for p in patterns:
            if p.match(line):
                out.append(line.rstrip())
                break
    return out


#: Maximum diff size we will analyze. Beyond this we bail to "allow" —
#: matches the safety cap pattern from ``indexer.fix_history`` and prevents
#: a malicious / generated 100-MiB diff from spending unbounded CPU
#: across ~30 regex patterns. Caught by Week-4 R1 #7 (regex complexity).
_MAX_DIFF_BYTES = 1_000_000  # 1 MB


def change_touches_signature(
    diff_text: str | None,
    *,
    language: str | None = None,
) -> bool:
    """Return True if a public signature line differs between
    the diff's `before` and `after`.

    Both forward (sig in before that's missing in after) and reverse
    (sig in after that's missing in before) directions count — additions,
    deletions, modifications all qualify as "touches signature."

    Returns False if the diff can't be parsed (conservative — better
    to allow the edit than false-block on a malformed diff).

    Diffs larger than ``_MAX_DIFF_BYTES`` (1 MB) bail to False — this is
    a defense-in-depth cap; real source-code diffs are KB at most.
    """
    if not diff_text:
        return False
    if len(diff_text) > _MAX_DIFF_BYTES:
        return False
    before, after = parse_diff(diff_text)
    if before is None or after is None:
        return False

    before_sigs = set(signature_lines(before, language))
    after_sigs = set(signature_lines(after, language))

    return before_sigs != after_sigs


def signature_change_summary(
    diff_text: str,
    *,
    language: str | None = None,
) -> dict[str, list[str]]:
    """Diagnostic helper for the verdict's metadata.

    Returns ``{"added": [...], "removed": [...], "modified": [...]}``.
    "modified" is heuristic: a removed signature whose name (first
    identifier after the language keyword) matches an added signature
    is classified as "modified" rather than "added + removed".
    """
    if not diff_text or len(diff_text) > _MAX_DIFF_BYTES:
        return {"added": [], "removed": [], "modified": []}
    before, after = parse_diff(diff_text)
    if before is None or after is None:
        return {"added": [], "removed": [], "modified": []}

    before_sigs = signature_lines(before, language)
    after_sigs = signature_lines(after, language)

    before_set = set(before_sigs)
    after_set = set(after_sigs)

    added = [s for s in after_sigs if s not in before_set]
    removed = [s for s in before_sigs if s not in after_set]

    # Try to pair removes with adds by extracting the first identifier
    # after a language keyword. Mostly useful for renames / signature
    # changes where the function name stays the same.
    name_re = re.compile(
        r"\b(?:def|class|function|func|fn|interface|type|struct|enum|trait|record)\s+(\w+)"
    )

    def _name(line: str) -> str | None:
        m = name_re.search(line)
        return m.group(1) if m else None

    modified: list[str] = []
    paired_added: set[str] = set()
    final_added: list[str] = []
    final_removed: list[str] = []

    for r in removed:
        rn = _name(r)
        if rn is None:
            final_removed.append(r)
            continue
        match = next(
            (a for a in added if a not in paired_added and _name(a) == rn), None
        )
        if match is not None:
            modified.append(f"{r}  →  {match}")
            paired_added.add(match)
        else:
            final_removed.append(r)

    final_added = [a for a in added if a not in paired_added]

    return {
        "added": final_added,
        "removed": final_removed,
        "modified": modified,
    }
