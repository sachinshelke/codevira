"""
signals.py — SignalContext lazy accessor.

Every HookEvent carries a SignalContext that policies use to read codevira's
data sources (graph, decisions, recent fixes, token budget). Two
non-negotiable design properties:

1. **Lazy.** Loading the graph SQLite or running a `get_impact` query is
   expensive. SignalContext defers ALL of these until a policy actually
   asks. A policy that doesn't read ``signals.graph`` doesn't pay for it.

2. **Cached.** Within one event, multiple policies asking the same
   question (e.g. "decisions for file X") get one query, not N.

This file is intentionally small — it provides the accessor interface
and per-event cache. The actual data sources live in their existing
modules (``indexer.sqlite_graph``, ``mcp_server.tools.search``, etc.).
We import them lazily inside accessor methods to keep ``import
mcp_server.engine`` fast.

See docs/heroes/00-engine.md "Signals" for the full surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Sentinel for "this lazy slot has not yet been computed" — distinct from
# ``None`` because some signals legitimately resolve to ``None`` (e.g.,
# scope contract is None when Hero 3 isn't enabled).
_UNCOMPUTED: Any = object()


@dataclass
class SignalContext:
    """Lazy, cached accessor over codevira's data sources for one event.

    Created by the runner and passed to each policy's ``evaluate``. Lives
    only for the duration of one event — no cross-event state leaks.

    Usage from inside a policy::

        impact = signals.impact(target_file)         # cached after 1st call
        decisions = signals.decisions(file=target)   # cached
        fixes = signals.fixes(target_file)           # cached

    Each method documents its return shape. Methods that don't apply to
    the event's project (e.g., decisions when project_root has no
    ``.codevira/`` yet) return empty results, never raise.
    """

    project_root: Path

    # Cache slots — keyed by argument tuple where applicable.
    # Plain attributes (no key needed):
    _graph: Any = field(default=_UNCOMPUTED, repr=False)
    _current_session: Any = field(default=_UNCOMPUTED, repr=False)
    _scope_contract: Any = field(default=_UNCOMPUTED, repr=False)
    _token_budget: Any = field(default=_UNCOMPUTED, repr=False)

    # Keyed caches:
    _impact_cache: dict[Path, Any] = field(default_factory=dict, repr=False)
    _decisions_cache: dict[tuple, Any] = field(default_factory=dict, repr=False)
    _fixes_cache: dict[Path, Any] = field(default_factory=dict, repr=False)
    # v3.0.0 audit cleanup: _prefs_cache dropped. The preferences()
    # method was deleted along with the get_preferences MCP tool in
    # the 2026-05-22 surface-cut audit; no remaining policy reads it.

    # ---------------------------------------------------------------
    # Graph + impact
    # ---------------------------------------------------------------

    @property
    def graph(self) -> Any:
        """The project's SQLiteGraph instance (lazy, cached).

        Returns the existing ``indexer.sqlite_graph.SQLiteGraph`` opened
        against ``<data_dir>/graph/graph.db``. ``None`` if the project
        hasn't been initialized (no graph.db on disk yet).

        Policies that need raw SQL access can use ``self.graph.conn``,
        but most policies should use the higher-level accessors below.
        """
        if self._graph is _UNCOMPUTED:
            self._graph = self._load_graph()
        return self._graph

    def _load_graph(self) -> Any:
        """Locate and open the project's graph.db.

        Resolution priority (matches mcp_server.paths.get_data_dir):
          1. Centralized: ``~/.codevira/projects/<slug>/graph/graph.db``
             (v1.6+ default)
          2. Legacy in-project: ``<project_root>/.codevira/graph/graph.db``
             (v1.5 and earlier — still in use on un-migrated projects)
          3. None — uninitialized project; signal returns None to policies.

        We resolve manually (rather than calling ``get_data_dir()``) so
        that this signal works for ANY ``project_root``, not just the
        process-global one. Multi-project use cases (running tests,
        future daemon) need this.
        """
        try:
            from indexer.sqlite_graph import (
                SQLiteGraph,
            )  # local import — slow on cold path
            from mcp_server.paths import _sanitize_path_key, get_global_home

            # 1. Centralized location (v1.6+).
            key = _sanitize_path_key(self.project_root)
            centralized = get_global_home() / "projects" / key / "graph" / "graph.db"
            if centralized.exists():
                return SQLiteGraph(centralized)

            # 2. Legacy in-project location (v1.5 and earlier). Honors
            #    users who haven't been migrated yet — without this
            #    fallback, every signal-using policy silently no-ops on
            #    legacy projects.
            legacy = self.project_root / ".codevira" / "graph" / "graph.db"
            if legacy.exists():
                return SQLiteGraph(legacy)

            # 3. Uninitialized — return None so policies skip silently.
            return None
        except Exception:  # noqa: BLE001 — signal layer must never crash a policy
            return None

    def impact(self, file_path: Path) -> dict[str, Any]:
        """Return the blast-radius / impact set for a file.

        Output shape (matches existing ``tools.graph.get_impact``):

            {"affected_files": [...], "affected_count": N, "callers": [...]}

        Cached per file_path. Returns empty dict if graph unavailable.
        """
        key = file_path
        if key in self._impact_cache:
            return self._impact_cache[key]
        result: dict[str, Any] = {}
        try:
            graph = self.graph
            if graph is None:
                self._impact_cache[key] = result
                return result
            # Use the existing get_impact function — it already returns a dict.
            # We pass project-relative path the way the existing tool expects.
            from mcp_server.tools.graph import get_impact

            try:
                rel = str(file_path.relative_to(self.project_root))
            except ValueError:
                rel = str(file_path)
            result = get_impact(rel, summary_only=False)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            result = {}
        self._impact_cache[key] = result
        return result

    # ---------------------------------------------------------------
    # Decisions
    # ---------------------------------------------------------------

    def decisions(
        self,
        *,
        file: Path | str | None = None,
        locked_only: bool = False,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return decisions matching filters.

        Shape: list of dicts with keys ``id``, ``file_path``, ``decision``,
        ``context``, ``locked`` (bool, mirrors do_not_revert), ``timestamp``.

        v3.0.0 (2026-05-22): rewired to read ``.codevira/decisions.jsonl``
        via ``mcp_server.storage.decisions_store`` — the v2.x SQL path
        that queried ``graph.db`` was structurally broken in v2.2.0 onward
        because record_decision writes to JSONL, not the SQL table. The
        bug went undetected because every engine-policy unit test mocked
        SignalContext with ``_FakeSignals``; the only real exercise was
        this round-2 G5 verification, which surfaced ``DecisionLock``
        silently failing open. Don't add back-compat for the SQL path:
        the JSONL is the v3.0.0 source of truth.

        Round-4 QA HIGH #3: ``limit`` is clamped to [1, 1000] to prevent
        a misbehaving policy from issuing ``limit=-1`` or
        ``limit=10**9`` (memory exhaustion).

        Cached by argument tuple.
        """
        limit = max(1, min(int(limit), 1000))
        cache_key = (str(file) if file is not None else None, locked_only, limit)
        if cache_key in self._decisions_cache:
            return self._decisions_cache[cache_key]

        result: list[dict[str, Any]] = []
        try:
            from mcp_server.storage import (
                decisions_store,
                paths as store_paths,
            )

            decisions_path = store_paths.decisions_path()
            if not decisions_path.is_file():
                # .codevira/ not initialised; no decisions to evaluate.
                self._decisions_cache[cache_key] = result
                return result

            # Build a normalised file-path filter. The decision_lock policy
            # passes the project-relative form; the JSONL stores the same.
            file_str = str(file) if file is not None else None

            # decisions_store.list_all applies amendments + superseded
            # filtering automatically. We use the public API rather than
            # re-reading the JSONL ourselves so any future schema changes
            # land in one place.
            raw = decisions_store.list_all(
                limit=limit,
                file_pattern=file_str,
                protected_only=locked_only,
                include_superseded=False,
                full=True,
            )
            for d in raw.get("decisions", []):
                # The engine-policy contract expects these specific keys.
                # Map v3.0.0 JSONL shape → engine shape.
                result.append(
                    {
                        "id": d.get("id"),
                        "file_path": d.get("file_path"),
                        "decision": d.get("decision"),
                        "context": d.get("context"),
                        "locked": bool(d.get("do_not_revert")),
                        "timestamp": d.get("ts"),
                    }
                )
        except Exception:  # noqa: BLE001 — signals must never raise
            result = []
        self._decisions_cache[cache_key] = result
        return result

    def search_decisions(
        self,
        query: str,
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """BM25-ranked FTS5 search over the JSONL decision store.

        v2.2.0+: uses ``mcp_server.storage.decisions_store.search()``
        (JSONL + FTS5 backend). The legacy SQLiteGraph fallback was
        removed once the v2.1.x carryover user base dropped to zero.

        Cached by ``(query, limit)`` so multiple policies asking the
        same question pay once. Returns empty list on any error or
        when ``.codevira/`` is not initialised.

        Limit is clamped to [1, 20].
        """
        limit = max(1, min(int(limit), 20))
        cache_key = ("search", query, limit)
        cached = self._decisions_cache.get(cache_key)
        if cached is not None:
            return cached
        result: list[dict[str, Any]] = []
        try:
            from mcp_server.storage import decisions_store, paths as store_paths

            if store_paths.is_initialized():
                result = decisions_store.search(query, limit=limit)
        except Exception:  # noqa: BLE001
            result = []
        self._decisions_cache[cache_key] = result
        return result

    # ---------------------------------------------------------------
    # Fix history (NEW — backed by indexer/fix_history.py)
    # ---------------------------------------------------------------

    def fixes(self, file_path: Path) -> list[dict[str, Any]]:
        """Return known fixes touching the given file.

        Shape: list of dicts (see ``indexer.fix_history.FixRecord``).
        Empty if no fix history yet (Week 1 returns []; Week 2 wires git
        log scanning).
        """
        if file_path in self._fixes_cache:
            return self._fixes_cache[file_path]
        result: list[dict[str, Any]] = []
        try:
            from indexer.fix_history import lookup as fix_lookup

            result = fix_lookup(self.project_root, file_path)
        except Exception:  # noqa: BLE001
            result = []
        self._fixes_cache[file_path] = result
        return result

    # v3.0.0 audit cleanup: preferences(), outcomes(), learned_rules()
    # all deleted. They were no-op stubs (or, in the preferences case,
    # a broken stub that imported a non-existent symbol) left over
    # from the v2.2.0 surface cut. The corresponding MCP tools
    # (get_preferences, get_learned_rules) were removed in the
    # 2026-05-22 audit; the AIPromotionScore policy that consumed
    # outcomes() was deleted in the same audit. No remaining code
    # reads any of these signals.

    # ---------------------------------------------------------------
    # Token budget meter
    # ---------------------------------------------------------------

    @property
    def token_budget(self) -> Any:
        """The per-session token meter (lazy, shared across policies).

        Returns a ``TokenMeter`` instance scoped to the current session.
        Hero 6 (Token Budget Live View) reads/writes here. Other policies
        may read the current usage via ``token_budget.summary()``.
        """
        if self._token_budget is _UNCOMPUTED:
            try:
                from mcp_server.engine.token_meter import get_session_meter

                self._token_budget = get_session_meter()
            except Exception:  # noqa: BLE001
                self._token_budget = None
        return self._token_budget

    # ---------------------------------------------------------------
    # Scope contract
    # ---------------------------------------------------------------

    @property
    def scope_contract(self) -> Any:
        """v2.2.0+: always returns None.

        Hero 3 (ProactiveScopeContractLock) was deleted in the
        2026-05-22 surface-cut audit. The signal slot is retained
        as a no-op so existing policy code that probes it doesn't
        need updating.
        """
        return None

    # ---------------------------------------------------------------
    # Current session context
    # ---------------------------------------------------------------

    @property
    def current_session(self) -> dict[str, Any]:
        """Output of ``get_session_context()`` for this project."""
        if self._current_session is _UNCOMPUTED:
            try:
                from mcp_server.tools.learning import get_session_context  # type: ignore[attr-defined]

                self._current_session = get_session_context() or {}
            except Exception:  # noqa: BLE001
                self._current_session = {}
        return self._current_session
