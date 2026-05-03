"""
policies.py — Policy base class + PolicyVerdict.

A Policy is the unit of v2.0 hero behavior. Each of the 10 heroes registers
one (or more) Policy subclasses with the engine. The engine calls
``Policy.evaluate(event)`` and combines the verdicts.

Policies are *stateless* across events — any state they need lives in
the engine's signal layer (graph, decisions, fix history, token meter,
etc.). This makes policies trivially testable and lets the engine cache
signals across the policies that share an event.

See docs/heroes/00-engine.md "Policy plugin API" for the contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

from mcp_server.engine.events import EventType, HookEvent

# Verdict actions — string-literal enum for easy JSON-encoding when
# verdicts cross the hook-script boundary.
VerdictAction = Literal["allow", "warn", "block", "inject"]


@dataclass(frozen=True)
class PolicyVerdict:
    """The result of one policy evaluating one event.

    The engine combines verdicts from all policies that handled an event:
      - Any ``block`` is final (first by priority).
      - All ``warn`` are concatenated.
      - All ``inject`` are concatenated, prepended to AI context.
      - Otherwise ``allow``.

    Construction helpers (``allow()``, ``warn()``, ``block()``, ``inject()``)
    are the preferred way to create verdicts — they fill in sensible
    defaults and read better at call sites than the raw constructor.

    Attributes:
        action: ``"allow"`` | ``"warn"`` | ``"block"`` | ``"inject"``.
        message: human-readable explanation. Required for non-allow.
        inject_context: only used when ``action == "inject"``; this string
            is added to the AI's next-turn context.
        policy: name of the policy that produced this verdict. Auto-filled
            by the runner; policies don't need to set it.
        metadata: arbitrary extras (decision IDs violated, callers count,
            etc.) — surfaces in logs and `codevira doctor` output.
    """

    action: VerdictAction
    message: str | None = None
    inject_context: str | None = None
    policy: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # ---- Constructors (the canonical way for policies to return) ----

    @classmethod
    def allow(cls, *, metadata: dict[str, Any] | None = None) -> "PolicyVerdict":
        return cls(action="allow", metadata=metadata or {})

    @classmethod
    def warn(
        cls, message: str, *, metadata: dict[str, Any] | None = None
    ) -> "PolicyVerdict":
        return cls(action="warn", message=message, metadata=metadata or {})

    @classmethod
    def block(
        cls, message: str, *, metadata: dict[str, Any] | None = None
    ) -> "PolicyVerdict":
        return cls(action="block", message=message, metadata=metadata or {})

    @classmethod
    def inject(
        cls, context: str, *, message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "PolicyVerdict":
        return cls(
            action="inject",
            inject_context=context,
            message=message,
            metadata=metadata or {},
        )

    # ---- Predicates ----

    def is_blocking(self) -> bool:
        return self.action == "block"

    def is_allowing(self) -> bool:
        return self.action == "allow"


class Policy:
    """Base class for engine policies.

    Subclasses MUST set:
      - ``name``: stable snake_case identifier
      - ``handles``: which event types to receive

    Subclasses MUST implement:
      - ``evaluate(event) -> PolicyVerdict``

    Subclasses MAY override:
      - ``enabled_by_default`` (bool, default True)
      - ``priority`` (int, default 0; higher runs first)
      - ``config_schema()`` (dict for `codevira doctor`-friendly config docs)

    Lifecycle:
      - One instance per process. The engine instantiates policies via
        ``register_policy(MyPolicy())`` at import time.
      - ``evaluate()`` runs synchronously inside hook scripts — keep it
        fast (see Performance budget in docs/heroes/00-engine.md).
      - Exceptions are caught by the runner; one bad policy never breaks
        the others.
    """

    # --- Subclass overrides (defaults are conservative) ---

    name: str = ""
    handles: Iterable[EventType] = ()
    enabled_by_default: bool = True
    priority: int = 0

    # --- Identity helpers ---

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # Allow Policy itself to be subclassed without name; but warn if a
        # registered subclass has no name.
        if cls.__module__ == __name__ and cls.__name__ == "Policy":
            return  # the base class itself
        # We don't enforce name presence at __init_subclass__ — wait until
        # registration so test fixtures can build anonymous subclasses.

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return f"<Policy {self.name or self.__class__.__name__} priority={self.priority}>"

    # --- Required override ---

    def evaluate(self, event: HookEvent) -> PolicyVerdict:
        """Decide what to do about this event.

        Default: allow. Subclasses override.
        """
        return PolicyVerdict.allow()

    # --- Optional override ---

    def config_schema(self) -> dict[str, Any]:
        """Return a JSON-schema-like dict describing this policy's config knobs.

        Used by ``codevira doctor`` and ``codevira config policy <name>``
        to surface available settings to the user. Empty dict means "no
        configurable knobs."
        """
        return {}
