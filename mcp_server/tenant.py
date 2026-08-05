"""
tenant.py — who ``global.db`` belongs to (4.0 Step 10).

``~/.codevira/global.db`` holds cross-project state: the project registry,
distilled communication preferences, learned rules. Every row in it is
implicitly "the person whose ``$HOME`` this is". On a laptop that
assumption is correct and invisible.

It stops being correct the moment the home directory is shared:

* a CI runner or build box where several people's jobs run as one OS user
* a devcontainer image with a baked ``$HOME``
* codevira running server-side, which the roadmap points at for 4.1

In all three, preferences distilled from one person's prompts become
another person's context, silently. ``memory-strategy-2026-07.md:193``
lists this as **the one true 4.0 breaking change**, and it is breaking
because a 3.x reader cannot scope rows it does not know are scoped — it
would read every tenant's data as its own.

# The key

``CODEVIRA_TENANT`` if set, otherwise ``"local"``.

``"local"`` is deliberate. Every existing row belongs to exactly one
person already, so migrating them to a single default tenant is not a
guess — it is a statement of what is already true, and it leaves a
single-user machine behaving exactly as it does today. There is no
account system to derive an identity from, and inventing one (an email, a
machine fingerprint) would either ask for data we do not need or split
one person across their two laptops.

Explicitly NOT ``device_id`` (Step 9 · S1). That identifies a *machine*;
a tenant is a *person*, and a person with a desktop and a laptop is one
tenant with two devices. Using device_id would fragment their preferences
into two sets that never merge — the opposite of what global.db is for.
"""

from __future__ import annotations

import os

#: Overrides the tenant. Set per-user on a shared runner, or per-account
#: by a server deployment.
TENANT_ENV = "CODEVIRA_TENANT"

#: The tenant every pre-4.0 row is migrated to, and the tenant a normal
#: single-user install stays on forever.
DEFAULT_TENANT = "local"

#: Long enough for a UUID or an email, short enough that it cannot be used
#: to smuggle a payload into a column that ends up in a query.
MAX_TENANT_LEN = 128


def current_tenant() -> str:
    """The tenant owning writes made by this process. Never raises.

    A blank or whitespace-only override is treated as unset rather than
    as a tenant named ``""`` — an empty string would silently create a
    second, invisible partition that no other process would ever match.
    """
    raw = os.environ.get(TENANT_ENV, "")
    return normalize(raw) or DEFAULT_TENANT


def normalize(value: str | None) -> str:
    """Trim and bound a tenant key. ``""`` when there is nothing usable."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_TENANT_LEN]


def is_shared_home() -> bool:
    """True when the tenant is explicitly set, i.e. someone has told us
    this home is shared. Used to decide whether to SAY anything about
    tenancy in CLI output — on a laptop it is noise."""
    return current_tenant() != DEFAULT_TENANT
