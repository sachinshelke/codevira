"""
egress.py — the only module in codevira allowed to touch the network.

ROADMAP.md publishes "local-first" as a non-negotiable. Until now that was
a property of the code that happened to be true, not one anything checked:
nothing stopped a future module from adding an ``import httpx`` and quietly
turning a local-first memory tool into one that ships decision text to a
third party. The claim needs an implementation to point at.

So every outbound request funnels through here, and
``tests/test_egress_boundary.py`` walks the import graph and FAILS if any
other module imports a network client. A guardrail nobody can enforce is a
README sentence; this one breaks the build.

# What is actually here today

One thing: the PyPI version check. That is the entire network surface of
the product, and it is advisory — it tells you a newer codevira exists.
No decision text, no file paths, no code, no telemetry leaves the machine,
and this module is the place to look to confirm that rather than a claim
you have to take on faith.

# Two independent off switches

- ``CODEVIRA_NO_NETWORK=1`` — absolute. Checked first, honoured by every
  call, no config can re-enable it. This is the switch for someone who
  needs to state that a tool made no connections, and it has to be
  answerable without reading a config file.
- ``network.enabled: false`` in ``.codevira/config.yaml`` — the ordinary
  project-level preference.

Denial is not an error. A blocked call returns ``None`` exactly as a failed
one does, because a memory tool must work identically offline — that is
what "local-first" means, and a version check that raises when the network
is off would break the CLI for the people most likely to have turned it off.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: Absolute kill switch. Deliberately env-only: a person answering "did
#: this tool phone home?" should be able to set one variable and be sure,
#: without auditing config precedence.
NO_NETWORK_ENV = "CODEVIRA_NO_NETWORK"

#: Only these hosts may ever be contacted. An allowlist rather than a
#: blocklist: the failure mode of forgetting to add a host is a feature
#: that does not work, and the failure mode of forgetting to block one is
#: a user's decision log arriving somewhere they did not choose.
ALLOWED_HOSTS = frozenset({"pypi.org"})

#: Nothing here should ever be slow enough to notice. A network call on a
#: CLI path that hangs is indistinguishable from the tool being broken.
DEFAULT_TIMEOUT_S = 5.0

#: Cap on any response we will read. A hostile or broken endpoint must not
#: be able to exhaust memory.
MAX_RESPONSE_BYTES = 1_000_000


def blocked_reason() -> str | None:
    """Why egress is disabled right now, or None if it is permitted."""
    if os.environ.get(NO_NETWORK_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return f"{NO_NETWORK_ENV} is set"
    try:
        from mcp_server.storage.config import get_flag

        if get_flag("network.enabled", True) is False:
            return "network.enabled is false in .codevira/config.yaml"
    except Exception:  # noqa: BLE001
        # An unreadable config leaves the default (permitted), matching
        # every other flag in the product. The env kill switch above is
        # the one that must not depend on parsing anything.
        pass
    return None


def is_enabled() -> bool:
    return blocked_reason() is None


def _host_of(url: str) -> str:
    from urllib.parse import urlparse  # stdlib string parsing, not a client

    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def get_json(
    url: str,
    *,
    user_agent: str,
    timeout: float = DEFAULT_TIMEOUT_S,
    on_error: Callable[[str], None] | None = None,
) -> dict[str, Any] | None:
    """GET ``url`` and parse JSON, or return None.

    Returns None — never raises — when egress is disabled, the host is not
    allowlisted, the URL is not https, or the request fails for any reason.
    Callers treat all of those identically, which is the point: offline is
    a normal operating state for this product, not an error condition.

    ``on_error`` receives the specific reason when there is one. It exists
    because collapsing every failure to "it didn't work" costs real
    diagnostic ground: a user asking why their update check is quiet is
    much better served by ``OSError: network down`` than by a generic
    string. A callback rather than shared state, so concurrent callers
    cannot read each other's failures.
    """

    def _fail(msg: str) -> None:
        if on_error is not None:
            try:
                on_error(msg)
            except Exception:  # noqa: BLE001 — diagnostics never break a call
                pass

    reason = blocked_reason()
    if reason:
        logger.debug("egress: refused %s (%s)", url, reason)
        _fail(reason)
        return None

    if not url.startswith("https://"):
        logger.warning("egress: refused non-https URL %s", url)
        _fail(f"refused non-https URL: {url}")
        return None

    host = _host_of(url)
    if host not in ALLOWED_HOSTS:
        logger.warning("egress: refused non-allowlisted host %r", host)
        _fail(f"refused non-allowlisted host: {host!r}")
        return None

    try:
        # Imported HERE, not at module scope, so the import-graph test can
        # attribute the one legitimate network-client import to this file
        # and to nothing else.
        from urllib.request import Request, urlopen

        req = Request(url, headers={"User-Agent": user_agent})

        # macOS python.org builds ship without a system CA bundle wired
        # into OpenSSL, so a bare urlopen fails CERTIFICATE_VERIFY_FAILED.
        # certifi arrives transitively (mcp -> httpx -> certifi); fall back
        # to the default context if it ever does not.
        context = None
        try:
            import ssl

            import certifi

            context = ssl.create_default_context(cafile=certifi.where())
        except Exception:  # noqa: BLE001
            context = None

        with urlopen(req, timeout=timeout, context=context) as resp:  # noqa: S310
            raw = resp.read(MAX_RESPONSE_BYTES)
        parsed = json.loads(raw.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:  # noqa: BLE001 — offline is not an error
        logger.debug("egress: %s failed: %s", url, exc)
        _fail(f"{type(exc).__name__}: {exc}")
        return None
