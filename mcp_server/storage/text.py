"""
text.py — the one clipper every user-visible surface shares.

Codevira grew three of these. ``decisions_store.one_line_summary`` (E1,
Phase 19) was the good one: collapse whitespace, prefer a sentence
boundary past the halfway mark, fall back to a word boundary, and return
short text verbatim rather than appending a spurious ellipsis. Then 4.0
added ``digest._clip_why`` and ``decision_lock._clip`` — the same idea,
written twice more, in the same change, because ``one_line_summary``
lives in ``decisions_store`` and ``digest`` cannot import it (circular:
``decisions_store`` imports ``digest``).

That is a packaging accident producing duplicated logic, not a design.
All three render the SAME field (`context`) to the SAME reader, so they
drifting apart is a user-visible inconsistency: the block message and the
injected line would clip the same sentence differently.

One definition, no cycles. ``decisions_store`` re-exports it under the
old name so existing callers and tests are untouched.
"""

from __future__ import annotations


def clip(text: str | None, cap: int = 140) -> str:
    """Collapse ``text`` to a single line of at most ``cap`` characters.

    Newlines and whitespace runs collapse to single spaces. Text that
    already fits comes back verbatim — no ellipsis on something that was
    never truncated. Otherwise the cut prefers a sentence boundary past
    the halfway mark, then the last word boundary, and is marked with an
    ellipsis.

    Returns ``""`` for empty/None input.
    """
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= cap:
        return collapsed
    cut = collapsed[:cap]
    dot = cut.rfind(". ")
    if dot >= cap // 2:
        return cut[: dot + 1]
    space = cut.rfind(" ")
    if space >= cap // 2:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "…"
