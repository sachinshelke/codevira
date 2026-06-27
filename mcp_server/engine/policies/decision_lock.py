"""
decision_lock.py — Hero 1: Active Decision Lock policy.

When the AI tries to Edit / Write / MultiEdit a file marked with
``do_not_revert`` in the graph, refuse the edit and surface the
locked decisions so the user can re-engage.

See ``docs/heroes/01-decision-lock.md`` for the spec — decision tree,
configuration knobs, and acceptance scenarios.
"""

from __future__ import annotations

import os
import re
from typing import Any

from mcp_server.engine.events import EventType, HookEvent
from mcp_server.engine.policy import Policy, PolicyVerdict
from mcp_server.engine.signals import SignalContext


_DEFAULT_MODE = "block"
_MODES = ("off", "warn", "block")

# ---------------------------------------------------------------------
# Content-aware orthogonality (v3.5.0) — the lock used to be PRESENCE-based:
# any non-additive edit to a file holding a do_not_revert decision was
# hard-blocked, even when the change had nothing to do with what was
# locked (e.g. editing a tool's description on server.py while the locked
# decisions are about the background watcher). We now compare the edit's
# diff envelope against each locked decision's text: only a change that
# actually references the decision's subject blocks; a provably-orthogonal
# change downgrades to a warn (decisions still surfaced for self-check).
#
# The tokenizer mirrors check_conflict._tokenize (D000004) but is kept
# local so this safety policy stays self-contained and never imports a
# private symbol from the tools layer. Tokens are additionally expanded
# across ``_``/``-`` so a compound code identifier (``validate_input``)
# matches a decision that spells it out (``validate input``) — closing the
# most dangerous false-NEGATIVE in a pure exact-match scheme.
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]+")
_MIN_SHARED_FOR_CONFLICT = 2  # < this many shared salient tokens ⇒ orthogonal
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "and",
        "or",
        "but",
        "of",
        "in",
        "on",
        "at",
        "to",
        "from",
        "for",
        "by",
        "with",
        "as",
        "it",
        "this",
        "that",
        "these",
        "those",
        "we",
        "you",
        "i",
        "they",
        "should",
        "must",
        "may",
        "can",
        "will",
        "would",
        "do",
        "does",
        "did",
        "use",
        "using",
        "used",
        "not",
        "def",
        "self",
        "return",
    }
)


class DecisionLock(Policy):
    """Block edits to files with locked architectural decisions.

    The lock comes from a ``do_not_revert`` decision attached to the file.
    Granularity (most to least precise): a decision may be scoped to a single
    SYMBOL (v3.6.0 — blocks only edits inside that function/class); otherwise
    it is FILE-scoped and the v3.5.0 content-aware token check decides whether
    an edit touches its subject. Symbol scoping only ever *relaxes* a
    token-clean edit — it never overrides a token-positive block, because
    region detection is fallible.
    """

    name = "decision_lock"
    handles = (EventType.PRE_TOOL_USE,)
    enabled_by_default = True
    # Higher priority than Hero 4 — Decision Lock is a HARD lock. Both
    # can fire on the same event; the runner combines verdicts (any
    # block wins). Priority only affects ordering in warn/inject
    # composition.
    priority = 100

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _config(self) -> dict[str, Any]:
        """Read env-var configuration. v2.0-alpha.2; YAML config in v2.1.

        Invalid values fall back to defaults; we never crash on bad config.
        """
        mode_raw = (
            os.environ.get("CODEVIRA_DECISION_LOCK_MODE", _DEFAULT_MODE).strip().lower()
        )
        mode = mode_raw if mode_raw in _MODES else _DEFAULT_MODE
        # v3.5.0: content-aware orthogonality is ON by default. Set
        # CODEVIRA_DECISION_LOCK_CONTENT_AWARE=0 (false/off/no) to restore the
        # strict pre-v3.5.0 behavior where every non-additive edit to a locked
        # file hard-blocks regardless of whether it touches the decision.
        ca_raw = (
            os.environ.get("CODEVIRA_DECISION_LOCK_CONTENT_AWARE", "1").strip().lower()
        )
        content_aware = ca_raw not in ("0", "false", "off", "no")
        return {"mode": mode, "content_aware": content_aware}

    def config_schema(self) -> dict[str, Any]:
        return {
            "mode": {
                "type": "string",
                "enum": list(_MODES),
                "default": _DEFAULT_MODE,
                "env": "CODEVIRA_DECISION_LOCK_MODE",
                "description": "off | warn | block",
            },
            "content_aware": {
                "type": "boolean",
                "default": True,
                "env": "CODEVIRA_DECISION_LOCK_CONTENT_AWARE",
                "description": (
                    "When true, an edit whose diff doesn't reference a locked "
                    "decision's subject downgrades block→warn instead of hard "
                    "blocking. Set 0 to restore strict file-level locking."
                ),
            },
        }

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(
        self,
        event: HookEvent,
        signals: SignalContext | None = None,
    ) -> PolicyVerdict:
        # Stage 1: structural filters
        if not event.is_edit():
            return PolicyVerdict.allow()
        if event.target_file is None:
            return PolicyVerdict.allow()

        config = self._config()
        if config["mode"] == "off":
            return PolicyVerdict.allow()

        # Stage 2: signal lookup
        if signals is None:
            return PolicyVerdict.allow()

        # The graph stores file_path as project-relative. Convert.
        try:
            target_rel = str(event.target_file.relative_to(event.project_root))
        except ValueError:
            target_rel = str(event.target_file)

        # First — is the file locked at all? Check via decisions(file=X,
        # locked_only=True). Empty result = either unlocked OR no decisions.
        # We need to distinguish those for edge case #5.
        locked_decisions = signals.decisions(
            file=target_rel,
            locked_only=True,
            limit=20,
        )
        if locked_decisions:
            # v3.3.0 Phase 7 precision: a pure-INSERTION edit (every
            # existing line survives, order preserved) cannot REVERT a
            # decision — reverting requires removing or changing existing
            # behavior. Added code could still contradict a decision's
            # spirit, so downgrade to warn (decisions surfaced for the AI
            # to self-check) instead of hard-blocking. Found by dogfooding
            # 2026-06-12: file-granular block stopped tool registrations
            # being ADDED to server.py over unrelated locked decisions.
            if self._is_pure_insertion(event):
                return self._make_verdict(
                    event=event,
                    config=config,
                    decisions=locked_decisions,
                    target_rel=target_rel,
                    downgrade_kind="insertion",
                )

            # v3.5.0 content-aware orthogonality: a modify/delete edit only
            # REVERTS a locked decision if it touches that decision's subject.
            # Compare the diff envelope's salient tokens against each locked
            # decision; if the change is provably orthogonal to ALL of them,
            # downgrade to warn. We only RELAX on positive evidence of
            # orthogonality — an unparseable / token-empty diff keeps the hard
            # block (we can't prove it safe).
            if config["content_aware"]:
                changed = self._changed_tokens(event)

                # v3.6.0 symbol-level scoping: only pay the file-parse cost when
                # a locked decision is actually symbol-scoped. ``touched`` is the
                # set of symbol(s) the edit lands in (possibly empty = "module
                # level"), or None when undeterminable (unparseable diff / Write
                # / before-text not locatable). File-scoped-only locks skip this
                # entirely, so existing behavior is byte-for-byte unchanged.
                has_symbol_scope = any(
                    (d.get("symbol") or "").strip() for d in locked_decisions
                )
                touched = self._symbols_touched(event) if has_symbol_scope else None

                # We can assess orthogonality with token evidence (``changed``)
                # OR determinate region evidence (``touched is not None``).
                if changed or touched is not None:
                    conflicting: list[dict[str, Any]] = []
                    shared_all: set[str] = set()
                    conflict_symbols: set[str] = set()
                    region_orthogonal_syms: set[str] = set()
                    for d in locked_decisions:
                        sym = (d.get("symbol") or "").strip()
                        region_determinate = bool(sym) and touched is not None
                        # In-region is a DEFINITIVE conflict: the edit lands
                        # inside the locked symbol.
                        if region_determinate and self._symbol_in_touched(sym, touched):
                            conflicting.append(d)
                            conflict_symbols.add(sym)
                            continue
                        # Otherwise (file-scoped, region-miss, OR region
                        # undeterminable) the v3.5.0 token check STILL governs.
                        # Region may only RELAX a token-clean edit — it must never
                        # OVERRIDE a token-positive block, because region detection
                        # is itself fallible (duplicate anchors, decorators,
                        # module-level dependencies, whitespace-fuzzy matches).
                        if changed:
                            is_conf, shared = self._decision_conflicts(changed, d)
                            if is_conf:
                                conflicting.append(d)
                                shared_all |= shared
                            elif region_determinate:
                                # Tokens clean AND the edit is positively in a
                                # different symbol → genuinely orthogonal by region.
                                region_orthogonal_syms.add(sym)
                            # else file-scoped + token-clean → orthogonal (omitted).
                        elif region_determinate:
                            # No token evidence, but region positively places the
                            # edit outside the locked symbol → orthogonal.
                            region_orthogonal_syms.add(sym)
                        else:
                            # No token evidence AND no determinate region → can't
                            # prove safe → conservative conflict.
                            conflicting.append(d)

                    if not conflicting:
                        # Prefer the region message when the only thing we
                        # relaxed was a symbol-scope miss.
                        if region_orthogonal_syms:
                            return self._make_verdict(
                                event=event,
                                config=config,
                                decisions=locked_decisions,
                                target_rel=target_rel,
                                downgrade_kind="region",
                                scope_symbols=region_orthogonal_syms,
                            )
                        return self._make_verdict(
                            event=event,
                            config=config,
                            decisions=locked_decisions,
                            target_rel=target_rel,
                            downgrade_kind="orthogonal",
                        )
                    return self._make_verdict(
                        event=event,
                        config=config,
                        decisions=conflicting,
                        target_rel=target_rel,
                        downgrade_kind=None,
                        conflict_tokens=shared_all or None,
                        scope_symbols=conflict_symbols or None,
                    )

            # Strict block: content-aware disabled, or neither token nor region
            # evidence was available (can't prove safe).
            return self._make_verdict(
                event=event,
                config=config,
                decisions=locked_decisions,
                target_rel=target_rel,
                downgrade_kind=None,
            )

        # No locked decisions — but is the file marked do_not_revert
        # without any recorded rationale? (Edge case #5: surface a
        # gentler warn so the user understands what's happening.)
        if self._file_is_locked_without_decisions(signals, target_rel):
            return self._make_verdict_no_rationale(
                event=event,
                config=config,
                target_rel=target_rel,
            )

        return PolicyVerdict.allow()

    @staticmethod
    def _is_pure_insertion(event: HookEvent) -> bool:
        """True if the proposed diff only ADDS lines — every line of
        ``before`` appears in ``after`` in the same order (subsequence
        test, trailing whitespace ignored).

        False on missing/oversized/malformed diffs and on full Writes
        (no diff envelope) — conservative: unknown edits keep the full
        block semantics.
        """
        from mcp_server.engine.policies._signature_detect import parse_diff

        diff = event.proposed_diff
        if not diff or len(diff) > 1_000_000:
            return False
        before, after = parse_diff(diff)
        if before is None or after is None:
            return False
        before_lines = [ln.rstrip() for ln in before.splitlines() if ln.strip()]
        after_iter = iter(ln.rstrip() for ln in after.splitlines())
        return all(any(b == a for a in after_iter) for b in before_lines)

    # ------------------------------------------------------------------
    # Content-aware orthogonality (v3.5.0)
    # ------------------------------------------------------------------

    @staticmethod
    def _salient_tokens(text: str) -> set[str]:
        """Subject-bearing tokens of ``text``: lowercased, ≥3 chars, no
        stop-words, with ``_``/``-`` compounds also split into their parts
        (so ``validate_input`` matches a decision phrased ``validate input``).
        """
        out: set[str] = set()
        for raw in _TOKEN_RE.findall(text or ""):
            tok = raw.lower()
            for piece in (tok, *re.split(r"[_-]", tok)):
                if len(piece) >= 3 and piece not in _STOPWORDS:
                    out.add(piece)
        return out

    @classmethod
    def _changed_tokens(cls, event: HookEvent) -> set[str] | None:
        """Salient tokens of the edit's diff envelope (``before`` + ``after``).

        Returns ``None`` when the diff is absent / oversized / unparseable —
        the caller treats that as "cannot prove orthogonal" and keeps the
        hard block. An empty set (envelope parsed but no salient tokens) is
        also treated conservatively by the caller.
        """
        from mcp_server.engine.policies._signature_detect import parse_diff

        diff = event.proposed_diff
        if not diff or len(diff) > 1_000_000:
            return None
        before, after = parse_diff(diff)
        if before is None or after is None:
            return None
        return cls._salient_tokens(f"{before}\n{after}")

    @staticmethod
    def _symbols_touched(event: HookEvent) -> set[str] | None:
        """The named symbol(s) the edit lands in, or ``None`` when that can't
        be determined (the caller then keeps file-level behavior).

        Thin wrapper over ``_region_detect.symbols_touched_by_edit`` — lazy
        import to match this module's pattern and keep engine startup cheap.
        """
        if event.target_file is None:
            return None
        from mcp_server.engine.policies._region_detect import symbols_touched_by_edit

        return symbols_touched_by_edit(event.target_file, event.proposed_diff)

    @staticmethod
    def _symbol_in_touched(sym: str, touched: set[str]) -> bool:
        """Whether a locked decision's ``symbol`` matches a symbol the edit
        landed in. The region detector emits BARE names, so a bare scope matches
        directly and a qualified scope (``Class.method``) matches on its final
        segment — otherwise ``record_decision(symbol="Auth.login")`` would
        silently never fire."""
        if sym in touched:
            return True
        return sym.rsplit(".", 1)[-1] in touched

    @classmethod
    def _decision_conflicts(
        cls, changed: set[str], decision: dict[str, Any]
    ) -> tuple[bool, set[str]]:
        """Does the changed code touch this decision's subject?

        True when the diff envelope shares ≥ ``_MIN_SHARED_FOR_CONFLICT``
        salient tokens with the decision's text (+context). A decision with
        no analyzable tokens is treated conservatively as a conflict (we
        can't prove the edit is orthogonal to it). Returns the shared tokens
        for the explanation.
        """
        dtokens = cls._salient_tokens(
            f"{decision.get('decision') or ''} {decision.get('context') or ''}"
        )
        if not dtokens:
            return True, set()
        shared = changed & dtokens
        return (len(shared) >= _MIN_SHARED_FOR_CONFLICT), shared

    def _file_is_locked_without_decisions(
        self,
        signals: SignalContext,
        target_rel: str,
    ) -> bool:
        """True if the file's graph node has do_not_revert=1 but no
        decisions are attached. Used for edge case #5 (surface a warn
        even in block mode — blocking on no-rationale would be confusing).
        """
        try:
            graph = signals.graph
            if graph is None:
                return False
            row = graph.conn.execute(
                "SELECT do_not_revert FROM nodes WHERE file_path = ? LIMIT 1",
                (target_rel,),
            ).fetchone()
            if row is None:
                return False
            return bool(row["do_not_revert"])
        except Exception:  # noqa: BLE001 — signal layer must never break a policy
            return False

    # ------------------------------------------------------------------
    # Verdict construction
    # ------------------------------------------------------------------

    def _make_verdict(
        self,
        *,
        event: HookEvent,
        config: dict[str, Any],
        decisions: list[dict[str, Any]],
        target_rel: str,
        downgrade_kind: str | None = None,
        conflict_tokens: set[str] | None = None,
        scope_symbols: set[str] | None = None,
    ) -> PolicyVerdict:
        """Build the warn-or-block verdict for the locked-with-decisions case.

        ``downgrade_kind`` selects the outcome:

        * ``None`` — genuine block (mode permitting). When ``scope_symbols`` is
          given the message names the locked symbol the edit lands in; else
          when ``conflict_tokens`` is given it names the shared subject.
        * ``"insertion"`` — pure-insertion warn (v3.3.0 Phase 7): the edit
          only adds lines, so nothing is reverted.
        * ``"orthogonal"`` — content-aware warn (v3.5.0): the edit's diff
          doesn't reference any locked decision's subject, so it's allowed
          with the decisions surfaced for self-check.
        * ``"region"`` — symbol-level warn (v3.6.0): the locked decision(s) are
          scoped to a symbol the edit doesn't touch, so it can't revert them.
          ``scope_symbols`` names those symbols for the message.
        """
        target_name = event.target_file.name if event.target_file else target_rel
        is_downgrade = downgrade_kind is not None

        # Top-3 decisions for the message
        sample_lines: list[str] = []
        for d in decisions[:3]:
            decision_text = (d.get("decision") or "").strip()
            # Truncate long decisions to keep message readable
            if len(decision_text) > 120:
                decision_text = decision_text[:117] + "..."
            ts = self._format_timestamp(d.get("timestamp"))
            did = d.get("id", "?")
            sample_lines.append(f"  • #{did}: {decision_text!r}{ts}")
        more = f"\n  ... and {len(decisions) - 3} more" if len(decisions) > 3 else ""

        if downgrade_kind == "insertion":
            message = (
                f"⚠️  Decision-lock notice on {target_name}: this file has "
                f"{len(decisions)} locked decision(s), but your edit only "
                f"ADDS lines, so nothing is being reverted.\n\n"
                f"Locked decisions (self-check that your addition doesn't "
                f"contradict them):\n"
                f"{chr(10).join(sample_lines)}{more}"
            )
        elif downgrade_kind == "orthogonal":
            message = (
                f"⚠️  Decision-lock notice on {target_name}: this file has "
                f"{len(decisions)} locked decision(s), but your change doesn't "
                f"reference their subject matter, so it doesn't appear to "
                f"revert them — allowed as orthogonal.\n\n"
                f"Locked decisions (self-check that your change truly doesn't "
                f"contradict them):\n"
                f"{chr(10).join(sample_lines)}{more}\n\n"
                f"(If this is wrong, set CODEVIRA_DECISION_LOCK_CONTENT_AWARE=0 "
                f"to restore strict file-level blocking.)"
            )
        elif downgrade_kind == "region":
            syms = ", ".join(sorted(scope_symbols or set())) or "the locked symbol(s)"
            message = (
                f"⚠️  Decision-lock notice on {target_name}: this file has "
                f"{len(decisions)} symbol-scoped locked decision(s), but your "
                f"edit lands outside {syms}, so it can't revert them — allowed "
                f"as orthogonal by region.\n\n"
                f"Locked decisions (self-check that your change truly doesn't "
                f"contradict them):\n"
                f"{chr(10).join(sample_lines)}{more}\n\n"
                f"(Restore strict file-level blocking with "
                f"CODEVIRA_DECISION_LOCK_CONTENT_AWARE=0.)"
            )
        else:
            reason = ""
            if scope_symbols:
                shown = ", ".join(sorted(scope_symbols)[:6])
                reason = (
                    f"Your change edits the locked symbol(s) {shown} — the "
                    f"exact region the decision(s) below protect.\n\n"
                )
            elif conflict_tokens:
                shown = ", ".join(sorted(conflict_tokens)[:6])
                reason = (
                    f"Your change touches code referencing {shown} — the "
                    f"subject of the locked decision(s) below.\n\n"
                )
            message = (
                f"🔒 Decision-lock veto on {target_name}: this file is marked "
                f"do_not_revert with {len(decisions)} locked decision(s).\n\n"
                f"{reason}"
                f"Locked decisions:\n"
                f"{chr(10).join(sample_lines)}{more}\n\n"
                f"To proceed safely:\n"
                f"  1. Surface the decision(s) to the user. The decision was\n"
                f"     locked for a reason — they may have context the AI lacks.\n"
                f"  2. If the user confirms the decision should be revisited,\n"
                f"     unlock the file first via codevira's CLI / API, OR\n"
                f"     override this policy session with\n"
                f"     CODEVIRA_DECISION_LOCK_MODE=warn (warns instead of blocks)\n"
                f"     or =off (disables this policy)."
            )

        metadata = {
            "policy": self.name,
            "target_file": str(event.target_file),
            "target_rel": target_rel,
            "mode": config["mode"],
            "locked_decision_count": len(decisions),
            "locked_decision_ids": [d.get("id") for d in decisions[:20]],
            # Back-compat: callers/tests still read ``pure_insertion``.
            "pure_insertion": downgrade_kind == "insertion",
            "content_orthogonal": downgrade_kind == "orthogonal",
            "region_orthogonal": downgrade_kind == "region",
            "downgrade_kind": downgrade_kind,
            "conflict_tokens": sorted(conflict_tokens) if conflict_tokens else [],
            "scope_symbols": sorted(scope_symbols) if scope_symbols else [],
        }

        if config["mode"] == "block" and not is_downgrade:
            return PolicyVerdict.block(message=message, metadata=metadata)
        return PolicyVerdict.warn(message=message, metadata=metadata)

    def _make_verdict_no_rationale(
        self,
        *,
        event: HookEvent,
        config: dict[str, Any],
        target_rel: str,
    ) -> PolicyVerdict:
        """Edge case #5: file is locked but no decisions are recorded.

        We deliberately downgrade block → warn here. Blocking with no
        rationale to surface would just frustrate the user. They get a
        warning so they know the file IS locked, with a note that they
        should record their reasoning.
        """
        target_name = event.target_file.name if event.target_file else target_rel
        message = (
            f"⚠️  Decision-lock warn on {target_name}: this file is marked "
            f"do_not_revert but has no recorded decisions.\n\n"
            f"Codevira would normally block, but blocking with no rationale\n"
            f"to show would be unhelpful. Recommendation: record at least\n"
            f"one decision on this file (codevira's record_decision tool)\n"
            f"so future AI sessions understand WHY the lock exists."
        )
        metadata = {
            "policy": self.name,
            "target_file": str(event.target_file),
            "target_rel": target_rel,
            "mode": config["mode"],
            "locked_decision_count": 0,
            "locked_without_rationale": True,
        }
        # Always warn, never block — even in block mode.
        return PolicyVerdict.warn(message=message, metadata=metadata)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _format_timestamp(ts: Any) -> str:
        """Format a stored timestamp for the user-visible message.
        Returns " (locked YYYY-MM-DD)" or empty string if unparseable.
        """
        if ts is None:
            return ""
        try:
            # signals.decisions returns the raw timestamp from SQL —
            # could be epoch seconds OR ISO string depending on how
            # the decision was recorded. Try both.
            from datetime import datetime, timezone

            if isinstance(ts, (int, float)):
                dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            else:
                # ISO-ish string
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            return f" (locked {dt.date().isoformat()})"
        except Exception:  # noqa: BLE001
            return ""
