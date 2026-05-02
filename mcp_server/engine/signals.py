"""
signals.py — SignalContext lazy accessor.

Every HookEvent carries a SignalContext that policies use to read codevira's
data sources (graph, decisions, fix history, preferences, etc.). Two
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
    _prefs_cache: dict[str, Any] = field(default_factory=dict, repr=False)

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
        try:
            from indexer.sqlite_graph import SQLiteGraph  # local import — slow on cold path
            from mcp_server.paths import _sanitize_path_key, get_global_home

            key = _sanitize_path_key(self.project_root)
            db_path = get_global_home() / "projects" / key / "graph" / "graph.db"
            if not db_path.exists():
                return None  # uninitialized project — skip silently
            return SQLiteGraph(db_path)
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

        Cached by argument tuple.
        """
        cache_key = (str(file) if file is not None else None, locked_only, limit)
        if cache_key in self._decisions_cache:
            return self._decisions_cache[cache_key]
        result: list[dict[str, Any]] = []
        try:
            graph = self.graph
            if graph is None:
                self._decisions_cache[cache_key] = result
                return result
            # Direct SQL — the existing search_decisions doesn't expose a
            # locked_only filter cleanly, so we go to the source.
            sql = """
                SELECT d.id, d.file_path, d.decision, d.context,
                       COALESCE(n.do_not_revert, 0) AS locked,
                       d.timestamp
                FROM decisions d
                LEFT JOIN nodes n ON n.file_path = d.file_path
                WHERE 1=1
            """
            params: list[Any] = []
            if file is not None:
                sql += " AND d.file_path = ?"
                params.append(str(file) if isinstance(file, Path) else file)
            if locked_only:
                sql += " AND COALESCE(n.do_not_revert, 0) = 1"
            sql += " ORDER BY d.timestamp DESC LIMIT ?"
            params.append(limit)
            rows = graph.conn.execute(sql, params).fetchall()
            result = [dict(r) for r in rows]
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

    # ---------------------------------------------------------------
    # Preferences
    # ---------------------------------------------------------------

    def preferences(self, category: str = "") -> list[dict[str, Any]]:
        """Return preferences in the given category (or all if empty)."""
        if category in self._prefs_cache:
            return self._prefs_cache[category]
        result: list[dict[str, Any]] = []
        try:
            from mcp_server.tools.learning import get_preferences  # type: ignore[attr-defined]

            data = get_preferences(category=category) if category else get_preferences()
            # Existing tool returns a dict with "preferences" key.
            if isinstance(data, dict):
                result = data.get("preferences", [])  # type: ignore[assignment]
            elif isinstance(data, list):
                result = data
        except Exception:  # noqa: BLE001
            result = []
        self._prefs_cache[category] = result
        return result

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
        """The current session's scope contract, or ``None``.

        Populated by Hero 3 (Scope Contract Lock) on UserPromptSubmit.
        Other policies (esp. Hero 4 Blast-Radius) may read this to refine
        their own decisions ("AI is in tight scope; don't be lenient").
        Returns ``None`` until Hero 3 ships and is enabled.
        """
        if self._scope_contract is _UNCOMPUTED:
            try:
                from mcp_server.engine.scope_contract import current_contract
                self._scope_contract = current_contract()
            except Exception:  # noqa: BLE001
                self._scope_contract = None
        return self._scope_contract

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
