"""
Rule Learner — Automatic rule generation from observed patterns.

Analyzes session decisions and outcomes to infer recurring patterns
and generate rules that future agents can use. Rules are stored in
SQLite and served alongside static rules from rules/*.md.

This is the engine that makes Codevira's memory adaptive:
the more sessions that happen, the less ambiguous future decisions become.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

from mcp_server.paths import get_data_dir
from indexer.sqlite_graph import SQLiteGraph

logger = logging.getLogger(__name__)


def run_rule_inference():
    """
    Main entry point: analyze all decisions and outcomes,
    detect patterns, and create or update learned rules.

    2026-05-18 v2.1.2 Item 17: skip the n-gram-prone
    ``_infer_decision_pattern_rules`` pass entirely for pre-code
    projects (zero indexed source files). On those projects the
    extractor was running over prose / filenames only and emitting
    sliding-window n-grams as fake "patterns" — trust-poisoning.
    Test pairing, import pattern, and co-change rules still run since
    they need actual file_path data, which already gates them.
    """
    db = SQLiteGraph(get_data_dir() / "graph" / "graph.db")
    try:
        _infer_test_pairing_rules(db)
        _infer_import_pattern_rules(db)
        # Item 17 pre-code guard
        try:
            file_count = db.conn.execute(
                'SELECT COUNT(*) AS c FROM nodes WHERE kind = "file"'
            ).fetchone()["c"]
        except Exception:
            file_count = 0
        if file_count > 0:
            _infer_decision_pattern_rules(db)
        else:
            logger.debug(
                "rule_learner: skipping decision_pattern_rules — 0 indexed source "
                "files (pre-code project; n-gram extractor would emit noise)"
            )
        _infer_file_co_change_rules(db)
    finally:
        db.close()


def _infer_test_pairing_rules(db: SQLiteGraph):
    """Detect test file pairing patterns (e.g., src/foo.py always has tests/test_foo.py)."""
    nodes = db.list_file_nodes()
    test_files = [n for n in nodes if n.get("layer") == "test"]
    source_files = [n for n in nodes if n.get("layer") != "test"]

    pairings: Counter = Counter()
    for tf in test_files:
        test_path = tf["file_path"]
        for sf in source_files:
            src_path = sf["file_path"]
            src_stem = Path(src_path).stem
            if src_stem in test_path:
                # Found a pairing pattern
                src_dir = str(Path(src_path).parent)
                test_dir = str(Path(test_path).parent)
                pairings[(src_dir, test_dir)] += 1

    for (src_dir, test_dir), count in pairings.items():
        if count >= 2:
            rule_text = f"Files in '{src_dir}/' should have corresponding tests in '{test_dir}/'."
            confidence = min(count / 5.0, 1.0)  # Max confidence at 5+ pairings
            _upsert_rule(
                db,
                rule_text,
                confidence,
                category="testing",
                file_pattern=f"{src_dir}/*",
            )


def _infer_import_pattern_rules(db: SQLiteGraph):
    """Detect common import patterns from the dependency graph edges."""
    edges = db.get_all_edges()
    if not edges:
        return

    # Count how many files import each target
    import_counts: Counter = Counter()
    for edge in edges:
        if edge["kind"] == "imports":
            import_counts[edge["target_id"]] += 1

    # Files imported by many others are "core" and should be stable
    for target_id, count in import_counts.items():
        if count >= 3:
            file_path = target_id.replace("file:", "")
            rule_text = f"'{file_path}' is imported by {count} files — changes here have wide blast radius. Review carefully."
            confidence = min(count / 10.0, 0.95)
            _upsert_rule(
                db, rule_text, confidence, category="imports", file_pattern=file_path
            )


def _infer_decision_pattern_rules(db: SQLiteGraph):
    """Detect recurring decision patterns from session history."""
    decisions = db.conn.execute("""
        SELECT d.decision, d.file_path, o.outcome_type
        FROM decisions d
        LEFT JOIN outcomes o ON d.id = o.decision_id
        WHERE d.decision IS NOT NULL
        ORDER BY d.created_at DESC LIMIT 200
    """).fetchall()

    if len(decisions) < 3:
        return

    # Group decisions by file directory to find area-specific patterns
    dir_decisions = defaultdict(list)
    for dec in decisions:
        if dec["file_path"]:
            dir_name = str(Path(dec["file_path"]).parent)
            dir_decisions[dir_name].append(
                {
                    "decision": dec["decision"],
                    "outcome": dec["outcome_type"],
                }
            )

    # Look for repeated decision keywords per directory
    for dir_name, decs in dir_decisions.items():
        if len(decs) < 2:
            continue

        # Extract common phrases from successful decisions
        successful = [d["decision"] for d in decs if d.get("outcome") in ("kept", None)]
        if len(successful) >= 2:
            common = _find_common_phrases(successful)
            for phrase, count in common:
                if count >= 2 and len(phrase) > 10:
                    rule_text = f"In '{dir_name}/': recurring pattern — {phrase}"
                    confidence = min(count / 5.0, 0.9)
                    _upsert_rule(
                        db,
                        rule_text,
                        confidence,
                        category="patterns",
                        file_pattern=f"{dir_name}/*",
                    )


def _infer_file_co_change_rules(db: SQLiteGraph):
    """Detect files that are frequently modified together across sessions."""
    sessions = db.conn.execute("""
        SELECT session_id, GROUP_CONCAT(DISTINCT file_path) as files
        FROM decisions
        WHERE file_path IS NOT NULL
        GROUP BY session_id
        HAVING COUNT(DISTINCT file_path) >= 2
    """).fetchall()

    if len(sessions) < 2:
        return

    co_change: Counter = Counter()
    for sess in sessions:
        files = sorted(sess["files"].split(","))
        for i, f1 in enumerate(files):
            for f2 in files[i + 1 :]:
                co_change[(f1, f2)] += 1

    for (f1, f2), count in co_change.items():
        if count >= 2:
            rule_text = f"'{Path(f1).name}' and '{Path(f2).name}' are frequently modified together. Changes to one likely require changes to the other."
            confidence = min(count / 4.0, 0.9)
            _upsert_rule(db, rule_text, confidence, category="structure")


# 2026-05-18 v2.1.2 Item 17: stopword set used to suppress n-gram noise
# in the rule extractor. Report 4 §7 flagged "recurring pattern — system
# foundation md", "recurring pattern — is a projection", etc. as
# trust-poisoning fake patterns. Filtering common English stop-words
# before grouping kills most of the noise; the density check below
# kills the rest.
_RULE_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "will",
        "with",
        "we",
        "you",
        "i",
        "it's",
        "do",
        "if",
        "but",
        "not",
        "no",
        "yes",
        "so",
        "than",
        "then",
        "when",
        "where",
        "what",
        "who",
        "how",
        "why",
        "into",
        "onto",
        "md",
        "txt",
        "py",
        "json",
        "yaml",
        "yml",  # common file-ext n-gram noise
    }
)


def _phrase_is_real_pattern(phrase: str, min_content_words: int = 2) -> bool:
    """v2.1.2 Item 17: gate against n-gram noise in the rule extractor.

    A "real pattern" must:
      1. contain ≥ ``min_content_words`` tokens that are NOT in the
         stopword list (Report 4 example: ``"is a projection"`` has 0
         content words → reject)
      2. have at least one content token that's ≥ 4 chars AND contains
         a letter (filters out short connectors like ``a``, ``the``,
         numbers, single-char glyphs)
    """
    tokens = phrase.split()
    content_tokens = [t for t in tokens if t.lower() not in _RULE_STOPWORDS]
    if len(content_tokens) < min_content_words:
        return False
    return any(len(t) >= 4 and any(c.isalpha() for c in t) for t in content_tokens)


def _find_common_phrases(texts: list[str], min_words: int = 3) -> list[tuple[str, int]]:
    """Find common multi-word phrases across a list of texts.

    2026-05-18 v2.1.2 Item 17: stopword + density filter applied so the
    rule extractor doesn't emit n-gram fragments like
    ``"recurring pattern — is a projection"``. Substring suppression
    also drops candidates that are substrings of higher-support
    candidates (so we don't ride along a sliding window).
    """
    phrase_counts: Counter = Counter()
    for text in texts:
        words = re.findall(r"\b\w+\b", text.lower())
        for length in range(min_words, min(len(words) + 1, 8)):
            for i in range(len(words) - length + 1):
                phrase = " ".join(words[i : i + length])
                phrase_counts[phrase] += 1

    # Density gate: only keep phrases with ≥2 content tokens.
    candidates = [
        (phrase, count)
        for phrase, count in phrase_counts.most_common(30)
        if count >= 2 and _phrase_is_real_pattern(phrase)
    ]

    # Substring suppression: drop any candidate that is a substring of an
    # already-accepted higher-support candidate (so "foundation md" doesn't
    # ride along with "system foundation md").
    kept: list[tuple[str, int]] = []
    for phrase, count in candidates:
        if any(phrase in stronger and phrase != stronger for stronger, _c in kept):
            continue
        kept.append((phrase, count))
        if len(kept) >= 10:
            break

    return kept


def _upsert_rule(
    db: SQLiteGraph,
    rule_text: str,
    confidence: float,
    category: str,
    file_pattern: str | None = None,
):
    """Insert a new learned rule or update confidence if a similar one exists."""
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT id, confidence FROM learned_rules WHERE rule_text = ?", (rule_text,)
        ).fetchone()

        if existing:
            # Update confidence (weighted average — new evidence matters)
            new_confidence = (existing["confidence"] * 0.7) + (confidence * 0.3)
            conn.execute(
                "UPDATE learned_rules SET confidence = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_confidence, existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO learned_rules (rule_text, confidence, source_sessions, category, file_pattern) VALUES (?, ?, ?, ?, ?)",
                (rule_text, confidence, json.dumps([]), category, file_pattern),
            )
