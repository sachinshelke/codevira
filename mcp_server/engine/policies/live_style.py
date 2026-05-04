"""
live_style.py — Hero 7: Live Style Enforcement policy.

Fires on POST_TOOL_USE Edit/Write/MultiEdit. Reads `signals.preferences()`,
scans the AI's just-applied diff for violations of recorded style
preferences (naming, quotes, indent), surfaces them as warns.

NEVER blocks — style is advisory. Warning the AI lets it self-correct;
forcing a block would be too aggressive for advisory signals.

See ``docs/heroes/07-live-style.md`` for the spec.
"""
from __future__ import annotations

import os
import re
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext


_DEFAULT_MODE = "warn"
_DEFAULT_MIN_FREQ = 3
_MODES = ("off", "warn")

#: Tools that this policy treats as edits (PostToolUse).
_EDIT_TOOLS = frozenset(["Edit", "Write", "MultiEdit", "NotebookEdit"])

#: Cap on per-detector violations to keep messages readable + bound CPU.
_MAX_VIOLATIONS_PER_DETECTOR = 50

#: Diff-size cap; over this we skip enforcement (huge diffs are rare,
#: usually generated/data files where style enforcement is noise).
_MAX_DIFF_BYTES = 100_000


# ---------------------------------------------------------------------
# Diff parsing — extract just the AFTER block
# ---------------------------------------------------------------------

_AFTER_BLOCK_RE = re.compile(
    r"^--- after\n(?P<after>.*)\Z",
    re.DOTALL | re.MULTILINE,
)


def _extract_after_block(diff: str | None) -> str:
    """Pull the AFTER block from a codevira-format diff. Returns '' if
    the diff is missing or empty.

    Two input shapes are supported (Bug-4 fix, Week-9 integration QA):
      1. **Edit format** (Claude Code Edit/MultiEdit hook): the wiring
         layer wraps old_string/new_string in markers::

             --- before
             {old}
             --- after
             {new}

         We extract everything after ``^--- after\\n``.

      2. **Write format** (Claude Code Write hook): the wiring layer
         passes the file's full new contents directly, with no markers.
         The whole diff IS the after-text.

      The original implementation only handled shape #1. As a result,
      Hero 7 silently no-op'd on every Write event in production while
      the entire test suite passed (every test used shape #1). This is
      the same Bug-3-shape failure as Bugs 1, 2, 3 — declared support
      that isn't integrated.

    Detection rule: if a `^--- after\\n` line is present in the diff,
    use shape #1; otherwise treat as shape #2. The check is line-anchored
    (MULTILINE), so a Write whose content happens to embed the literal
    string ``--- after`` mid-content would NOT trigger shape #1 unless
    that string starts a line.
    """
    if not diff:
        return ""
    if len(diff) > _MAX_DIFF_BYTES:
        return ""  # too large; skip enforcement
    match = _AFTER_BLOCK_RE.search(diff)
    if match:
        return match.group("after")
    # Shape #2: no marker → diff is raw post-write content (Write tool).
    return diff


# ---------------------------------------------------------------------
# Style detectors
# ---------------------------------------------------------------------

# All detectors take (after_text, target_file_suffix) → list of
# violation dicts. Each violation is {"line": int (1-indexed within
# the after block), "snippet": str, "rule": str}.

#: Identifier-extraction regexes per language family.
#: Captures the name in group 1.
_PYTHON_DEF_RE = re.compile(
    r"^[ \t]*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)
_PYTHON_CLASS_RE = re.compile(
    r"^[ \t]*class\s+([A-Za-z_]\w*)",
    re.MULTILINE,
)
_JS_FUNCTION_RE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(",
    re.MULTILINE,
)


def _is_camel_case(name: str) -> bool:
    """A name is camelCase if it has at least one lowercase-then-uppercase
    transition AND doesn't start with uppercase (which would be PascalCase).
    Single-word lowercase identifiers (e.g. 'foo') are NOT camelCase."""
    if not name or not name[0].islower():
        return False
    # Find a lowercase-followed-by-uppercase transition
    return bool(re.search(r"[a-z][A-Z]", name))


def _is_snake_case(name: str) -> bool:
    """A name is snake_case if it has at least one underscore between
    lowercase identifier parts AND no uppercase characters."""
    if not name or "_" not in name:
        return False
    if any(c.isupper() for c in name):
        return False
    return bool(re.match(r"^[a-z]+(_[a-z0-9]+)+$", name))


def _detect_naming_violations(
    after: str,
    suffix: str,
    expected_signal: str,
) -> list[dict[str, Any]]:
    """Find function / class names that violate the expected naming style."""
    expected = expected_signal.lower().strip()
    if expected not in ("snake_case", "camelcase"):
        return []  # unknown signal; skip

    # Collect (line, name) pairs from def + class + function
    candidates: list[tuple[int, str]] = []
    if suffix in (".py", ".pyi"):
        for m in _PYTHON_DEF_RE.finditer(after):
            line = after[:m.start()].count("\n") + 1
            candidates.append((line, m.group(1)))
        for m in _PYTHON_CLASS_RE.finditer(after):
            line = after[:m.start()].count("\n") + 1
            candidates.append((line, m.group(1)))
    elif suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
        for m in _JS_FUNCTION_RE.finditer(after):
            line = after[:m.start()].count("\n") + 1
            candidates.append((line, m.group(1)))
    else:
        return []  # unsupported language

    violations: list[dict[str, Any]] = []
    for line_no, name in candidates:
        # Skip dunders / private (leading underscore) — too many
        # legitimate exceptions.
        if name.startswith("_"):
            continue
        # Class names are conventionally PascalCase regardless. Skip
        # them for naming-style enforcement; this is a known scope.
        # (Detect PascalCase = starts with uppercase + has letters)
        if name[0].isupper():
            continue

        is_violation = False
        if expected == "snake_case" and _is_camel_case(name):
            is_violation = True
        elif expected == "camelcase" and _is_snake_case(name):
            is_violation = True

        if is_violation:
            violations.append({
                "line": line_no,
                "snippet": name,
                "rule": f"naming should be {expected}, got {name!r}",
            })
            if len(violations) >= _MAX_VIOLATIONS_PER_DETECTOR:
                break
    return violations


_QUOTE_LINE_RE = re.compile(r"^[^#'\"]*(['\"])", re.MULTILINE)


def _detect_quote_violations(
    after: str,
    suffix: str,
    expected_signal: str,
) -> list[dict[str, Any]]:
    """Find string literals using the wrong quote style.

    Conservative — only checks the FIRST quote-like character on each
    non-comment line. Fooled by f-strings with mixed quoting, but for
    v2.0-alpha that false-positive rate is acceptable.
    """
    expected = expected_signal.lower().strip()
    # Normalize signal — accept "double", "double-quotes", "double_quotes"
    if "double" in expected:
        wrong_quote = "'"
    elif "single" in expected:
        wrong_quote = '"'
    else:
        return []
    if suffix not in (".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs",
                      ".ts", ".tsx"):
        return []

    violations: list[dict[str, Any]] = []
    for m in _QUOTE_LINE_RE.finditer(after):
        if m.group(1) == wrong_quote:
            line_no = after[:m.start()].count("\n") + 1
            line_text = after.split("\n")[line_no - 1] if line_no - 1 < len(after.split("\n")) else ""
            violations.append({
                "line": line_no,
                "snippet": (line_text[:80] + "...") if len(line_text) > 80 else line_text,
                "rule": f"quotes should be {expected_signal}",
            })
            if len(violations) >= _MAX_VIOLATIONS_PER_DETECTOR:
                break
    return violations


def _detect_indent_violations(
    after: str,
    suffix: str,
    expected_signal: str,
) -> list[dict[str, Any]]:
    """Find lines using the wrong indent style."""
    expected = expected_signal.lower().strip()
    if "tab" in expected:
        # Project wants tabs; flag space-indented lines
        wrong_pattern = re.compile(r"^( {1,})", re.MULTILINE)
    elif "space" in expected:
        # Project wants spaces; flag tab-indented lines
        wrong_pattern = re.compile(r"^(\t+)", re.MULTILINE)
    else:
        return []

    violations: list[dict[str, Any]] = []
    for m in wrong_pattern.finditer(after):
        line_no = after[:m.start()].count("\n") + 1
        line_text = after.split("\n")[line_no - 1] if line_no - 1 < len(after.split("\n")) else ""
        violations.append({
            "line": line_no,
            "snippet": line_text[:80].replace("\t", "→"),
            "rule": f"indent should use {expected_signal}",
        })
        if len(violations) >= _MAX_VIOLATIONS_PER_DETECTOR:
            break
    return violations


# Map (category, signal) → detector function
def _dispatch_detector(
    category: str, signal: str, after: str, suffix: str,
) -> list[dict[str, Any]]:
    cat = category.lower().strip()
    if cat == "naming":
        return _detect_naming_violations(after, suffix, signal)
    if cat == "quotes":
        return _detect_quote_violations(after, suffix, signal)
    if cat == "indent":
        return _detect_indent_violations(after, suffix, signal)
    return []  # unrecognized category — silent no-op


def _detect_violations(
    after_text: str,
    target_file: Any,  # Path
    preferences: list[dict[str, Any]],
    min_frequency: int = _DEFAULT_MIN_FREQ,
) -> list[dict[str, Any]]:
    """Run every applicable detector against the diff. Returns flat list
    of violation dicts with an extra `pref` key showing which preference
    triggered.
    """
    suffix = target_file.suffix.lower() if hasattr(target_file, "suffix") else ""
    out: list[dict[str, Any]] = []
    for pref in preferences:
        try:
            freq = int(pref.get("frequency", 0))
        except (ValueError, TypeError):
            freq = 0
        if freq < min_frequency:
            continue
        category = pref.get("category", "")
        signal = pref.get("signal", "")
        if not category or not signal:
            continue
        violations = _dispatch_detector(category, signal, after_text, suffix)
        for v in violations:
            v["pref"] = {
                "category": category, "signal": signal,
                "frequency": freq,
            }
            out.append(v)
    return out


# ---------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------


class LiveStyleEnforcement(Policy):
    """Warn on style violations against recorded preferences."""

    name = "live_style_enforcement"
    handles = (EventType.POST_TOOL_USE,)
    enabled_by_default = True
    # Low priority — advisory, runs after any business-logic POST policies.
    priority = 20

    def _config(self) -> dict[str, Any]:
        mode_raw = os.environ.get(
            "CODEVIRA_LIVE_STYLE_MODE", _DEFAULT_MODE,
        ).strip().lower()
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE

        min_freq_raw = os.environ.get("CODEVIRA_LIVE_STYLE_MIN_FREQ")
        min_freq = _DEFAULT_MIN_FREQ
        if min_freq_raw:
            try:
                v = int(min_freq_raw)
                if 1 <= v <= 1000:
                    min_freq = v
            except (ValueError, TypeError):
                pass  # keep default

        return {"mode": mode, "min_frequency": min_freq}

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_LIVE_STYLE_MODE",
                "description": "off | warn (no block — style is advisory)",
            },
            "min_frequency": {
                "type": "integer",
                "default": _DEFAULT_MIN_FREQ,
                "env": "CODEVIRA_LIVE_STYLE_MIN_FREQ",
                "description": "Skip preferences observed fewer than N times (1-1000)",
            },
        }

    def evaluate(
        self, event: HookEvent, signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Stage 1: structural filters
        if event.event_type != EventType.POST_TOOL_USE:
            return PolicyVerdict.allow()
        if event.tool_name not in _EDIT_TOOLS:
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        if signals is None:
            return PolicyVerdict.allow()

        # Stage 2: pull preferences
        prefs = signals.preferences()
        if not prefs:
            return PolicyVerdict.allow()

        # Stage 3: extract the AI's after-block
        after_text = _extract_after_block(event.proposed_diff)
        if not after_text:
            return PolicyVerdict.allow()

        # Stage 4: detect violations
        violations = _detect_violations(
            after_text=after_text,
            target_file=event.target_file,
            preferences=prefs,
            min_frequency=config["min_frequency"],
        )
        if not violations:
            return PolicyVerdict.allow()

        return self._make_verdict(event, config, violations)

    def _make_verdict(
        self,
        event: HookEvent,
        config: dict[str, Any],
        violations: list[dict[str, Any]],
    ) -> PolicyVerdict:
        target_name = (
            event.target_file.name if event.target_file else "<unknown>"
        )

        # Top-3 violations with details
        sample_lines: list[str] = []
        for v in violations[:3]:
            line = v.get("line", "?")
            snippet = (v.get("snippet") or "").strip()
            rule = v.get("rule", "")
            pref = v.get("pref", {})
            cat = pref.get("category", "?")
            freq = pref.get("frequency", 0)
            sample_lines.append(
                f"  line {line}: {rule} (recorded {freq}× in {cat})"
            )
        more = (
            f"\n  ... and {len(violations) - 3} more"
            if len(violations) > 3 else ""
        )

        message = (
            f"⚠️  Style enforcement on {target_name}: "
            f"{len(violations)} style violation(s) detected.\n\n"
            f"Top violations:\n{chr(10).join(sample_lines)}{more}\n\n"
            f"To fix: ask the AI to rewrite using project conventions, OR\n"
            f"override with CODEVIRA_LIVE_STYLE_MODE=off."
        )

        metadata = {
            "policy": self.name,
            "target_file": str(event.target_file),
            "mode": config["mode"],
            "violation_count": len(violations),
            "violations_summary": [
                {"line": v.get("line"), "rule": v.get("rule"),
                 "category": v.get("pref", {}).get("category")}
                for v in violations[:20]
            ],
        }

        # Hero 7 always returns warn — never blocks (style is advisory).
        return PolicyVerdict.warn(message=message, metadata=metadata)
