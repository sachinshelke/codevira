"""
origin.py — v3.1.0 M1: provenance metadata for cross-IDE memory.

Every write Codevira makes (decisions, sessions, working memory, skills,
activity, reflections) carries an ``origin`` dict that records *who*
made the write — which IDE, which agent model, which machine, when.
This is Phase A of the v3.1.0 Consensus subsystem: real provenance
that ``check_conflict`` and ``get_session_context`` can surface so
agents can answer "this decision contradicts a do_not_revert one
written by Cursor 3 days ago — what would you like to do?"

# Schema

``current_origin()`` returns::

    {
      "ide":         "claude_code" | "claude_desktop" | "cursor" |
                     "antigravity" | "unknown",
      # NOTE: "windsurf" is no longer an injection target (Windsurf was
      # discontinued / folded into Cursor) but remains a RECOGNIZED value so
      # decisions recorded by Windsurf before 4.0 still read back correctly.
      "agent_model": "<model-id>" | None,
      "device_id":   "<16 hex chars>",   # 4.0 — the STABLE machine identity
      "host_hash":   "<12 hex chars>",   # legacy; drifts, see below
      "ts":          "2026-05-28T10:00:00+00:00",
    }

# Field sources

- ``ide``: read from the ``CODEVIRA_IDE`` env var, which
  ``ide_inject.py`` writes into each detected IDE's MCP server config.
  Defaults to ``"unknown"`` when unset (e.g., bare ``codevira`` CLI
  invocations or pre-v3.1 IDE configs).
- ``agent_model``: ``CODEVIRA_AGENT_MODEL`` env var (optional; most
  IDEs don't expose model id to MCP servers in v3.1, so this is
  commonly ``None``).
- ``device_id``: 16 random hex chars minted ONCE and persisted to
  ``~/.codevira/device_id``. This is the machine identity anything
  correctness-critical should use — see the section below.
- ``host_hash``: ``sha1(uuid.getnode() bytes + username)[:12]``. Kept
  for backward compatibility (every pre-4.0 record carries it and
  nothing else) but **no longer trustworthy as an identity**. The SHA1
  truncation is privacy-preserving — no plaintext hostname or
  username leaks if a team commits a ``decisions.jsonl`` to a public
  repo.
- ``ts``: ISO 8601 UTC timestamp of the call.

# Why ``device_id`` exists (4.0 Step 9 · S1)

``host_hash`` was documented as "stable per machine across reboots".
It is not. ``uuid.getnode()`` returns whichever MAC-bearing interface
enumerates first, and a developer laptop has many: this one exposes 16.
A VPN connecting, Docker starting, or a USB-ethernet dongle being
plugged in silently re-identifies the machine.

Measured on the codevira repo itself before this fix: **4 distinct
``host_hash`` values from a single machine over 7 weeks**, with two of
them live *concurrently* (one ran to 2026-07-20 while another started
2026-07-13). That is not a stable identity; it is a fingerprint of the
network stack at the moment of writing.

That matters because ``id_repair`` uses writer identity to decide
whether an amendment follows its renumbered base across a two-host
merge. A drifting identity makes a developer look like a stranger to
their own earlier decision, so their amendment lands on someone
else's record.

``device_id`` is deliberately **random**, not derived. There is nothing
about the hardware worth hashing: derived identity is what broke, and a
random value has no inputs that can change underneath it. It is also
strictly *less* identifying than a MAC hash — it reveals nothing about
the machine at all.

Old records are NEVER back-filled. A pre-4.0 record has no
``device_id``, and readers fall back to ``host_hash`` for it; inventing
a ``device_id`` for a record written before this field existed would
falsely attest which machine wrote it.

# Backward compatibility

v3.0.x records have no ``origin`` field. All readers MUST treat the
absence as ``ide="unknown"`` — never raise, never migrate. This file
deliberately does not provide a "fill missing origin" helper because
the value of provenance is in NEW records; back-filling fake origins
on old records would falsely attest authorship.

# Non-goals (v3.1.0)

- Cross-machine *sync*. ``device_id`` makes two machines
  distinguishable and merges deterministic; it does not move records
  between them. Sync is out of 4.0 scope.
- Tamper resistance. Neither ``device_id`` nor ``host_hash`` is a
  security primitive — a malicious actor can set ``CODEVIRA_IDE`` or
  ``CODEVIRA_DEVICE_ID`` to whatever they want, and ``device_id`` is a
  plain file anyone can edit. These fields are informational
  provenance, not authentication.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path


# Sentinel returned when the IDE env var is unset.
_IDE_UNKNOWN = "unknown"

#: Override for containers and CI, where ``~`` is frequently a fresh
#: layer and a persisted file would mint a new identity every run.
_DEVICE_ID_ENV = "CODEVIRA_DEVICE_ID"

#: Lives beside ``global.db`` — machine-scoped state, not project state.
#: Deliberately NOT inside any ``.codevira/`` project dir: committing a
#: device id would give every clone of that repo the same identity.
_DEVICE_ID_FILE = "device_id"

#: 64 bits. Two developers colliding needs ~4 billion machines.
_DEVICE_ID_WIDTH = 16


def _normalize_agent_model(raw: str | None) -> str | None:
    """Strip whitespace; coerce the strings 'null' / 'none' / '' to None.

    v3.1.x fix: previously a CODEVIRA_AGENT_MODEL set to whitespace OR
    the literal string 'null'/'None' passed through to the origin
    record. Downstream consensus checks string-compare agent_model;
    those bogus values would polute the provenance chain.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s or s.lower() in {"null", "none"}:
        return None
    return s


def current_origin() -> dict[str, str | None]:
    """Build the origin dict for *this* call.

    ``ts`` is freshly computed each call so per-record timestamps
    are honest. ``host_hash`` is cached (machine identity doesn't
    change between calls in the same process). ``ide`` and
    ``agent_model`` are read each call so a test that monkeypatches
    ``CODEVIRA_IDE`` mid-process sees the override.
    """
    return {
        "ide": os.environ.get("CODEVIRA_IDE", _IDE_UNKNOWN),
        "agent_model": _normalize_agent_model(os.environ.get("CODEVIRA_AGENT_MODEL")),
        "device_id": device_id(),
        # Still written so a 4.0 record stays readable by a 3.x reader
        # that only knows this field. Do not use it for new logic.
        "host_hash": _host_hash(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


def writer_id(origin_dict: dict | None) -> str:
    """The machine identity of whoever wrote a record, or ``""``.

    THE accessor for "same machine?" questions. Prefers ``device_id``
    and falls back to ``host_hash`` so pre-4.0 records — which have
    only the latter — still compare against each other exactly as they
    did before.

    A 4.0 record and a pre-4.0 record from the *same* machine will not
    match, because the old record cannot prove which machine wrote it.
    That is the intended direction: callers treat a non-match as "can't
    attribute" and degrade to their safe branch, rather than guessing.
    """
    if not isinstance(origin_dict, dict):
        return ""
    return str(origin_dict.get("device_id") or origin_dict.get("host_hash") or "")


def device_id() -> str:
    """This machine's stable identity. Never raises.

    Env var wins (containers / CI), then the persisted file, then — only
    if the filesystem is unusable — the legacy ``host_hash``, so this is
    never *worse* than the behaviour it replaces.
    """
    raw = os.environ.get(_DEVICE_ID_ENV, "").strip()
    if raw:
        return raw[:64]
    return _persisted_device_id()


def _device_id_path() -> Path | None:
    try:
        from mcp_server.paths import get_global_home

        return Path(get_global_home()) / _DEVICE_ID_FILE
    except Exception:  # noqa: BLE001 — identity must never break a write
        return None


@lru_cache(maxsize=1)
def _persisted_device_id() -> str:
    """Read ``~/.codevira/device_id``, minting it once if absent.

    The mint is race-safe via ``os.link``, which fails atomically if the
    target exists: two processes starting together cannot end up with
    two different identities, because the loser re-reads the winner's
    file rather than overwriting it. A plain ``write_text`` here would
    let the second process clobber the first, and every record written
    in between would be attributed to an identity that no longer exists
    — the exact failure this whole change is fixing.
    """
    path = _device_id_path()
    if path is None:
        return _host_hash()

    existing = _read_device_id(path)
    if existing:
        return existing

    minted = uuid.uuid4().hex[:_DEVICE_ID_WIDTH]
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(minted + "\n", encoding="utf-8")
        try:
            os.link(tmp, path)  # atomic claim; raises if someone won first
        except FileExistsError:
            pass
        except OSError:
            # Filesystem without hard links. os.replace is still atomic
            # per-file, so the worst case is a last-writer-wins race in
            # the first milliseconds of a machine's very first run.
            os.replace(tmp, path)
    except OSError:
        return _host_hash()
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    return _read_device_id(path) or minted


def _read_device_id(path: Path) -> str:
    try:
        if not path.is_file():
            return ""
        # errors="replace": a corrupted file must degrade to a mint, not
        # raise UnicodeDecodeError on the decision write path.
        return path.read_text(encoding="utf-8", errors="replace").strip()[:64]
    except OSError:
        return ""


@lru_cache(maxsize=1)
def _host_hash() -> str:
    """sha1(uuid.getnode() bytes + username)[:12].

    Cached because both inputs are process-stable. Falls back to
    ``"unknown"`` if neither source is readable (extremely unusual —
    a container without /etc/passwd and without a usable network
    interface — but documented for completeness).
    """
    try:
        node = uuid.getnode()
        mac_bytes = node.to_bytes(6, "big")
    except Exception:  # pragma: no cover — uuid.getnode() shouldn't raise
        mac_bytes = b""

    try:
        user = getpass.getuser()
    except Exception:  # pragma: no cover
        user = ""

    raw = mac_bytes + user.encode("utf-8", errors="replace")
    if not raw:
        return "unknown"
    return hashlib.sha1(raw).hexdigest()[:12]
