"""Shared y/n prompt helper — Bug 22 (rc.4 dogfood, 2026-05-13).

All y/n CLI prompts in codevira route through :func:`confirm` so they share:

1. ``sys.stdout.flush()`` before :func:`input` — prompt renders before stdin
   blocks (fixes the case where a buffered prior print + an immediate read
   left the user staring at an invisible prompt).
2. Retry loop on unrecognized input — only ``y/yes/n/no/<Enter>`` advance;
   anything else reprompts with ``Please answer 'y' or 'n'.`` instead of
   silently returning False.
3. :class:`EOFError` (non-interactive context, stdin closed) → return the
   safe default (False = abort) with a clear ``pass --yes`` hint.
4. :class:`KeyboardInterrupt` (Ctrl+C) → return False cleanly; no traceback.

Before Bug 22, every per-site prompt body looked like::

    answer = input("  Proceed? [Y/n] ").strip().lower()
    return answer in ("", "y", "yes")

Any input that wasn't exactly empty/``y``/``yes`` returned False. So a user
who typed something with a paste artifact, an unexpected whitespace char, or
even ``"yy"`` got a silent "no" — surface symptom: *"I typed Y and nothing
happened"*. The retry loop here makes that impossible: invalid input
reprompts visibly instead.
"""

from __future__ import annotations

import sys


def confirm(question: str, *, default: bool = True, indent: str = "  ") -> bool:
    """Ask a y/n question; return True for yes, False for no.

    Parameters
    ----------
    question
        The text of the question (caller supplies its own punctuation).
    default
        Value returned on a bare Enter. ``True`` renders as ``[Y/n]``,
        ``False`` renders as ``[y/N]``.
    indent
        Leading whitespace on the prompt line. Default two spaces matches
        the existing codevira CLI output style.

    Returns
    -------
    bool
        ``True`` if the user answered yes (``y`` / ``yes``) or accepted the
        default when ``default=True``; ``False`` if the user answered no
        (``n`` / ``no``), accepted the default when ``default=False``, hit
        EOF on a non-interactive stdin, or pressed Ctrl+C.
    """
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            # Flush so any preceding print() output is visible before input()
            # blocks. Without this, terminals can show an empty cursor with
            # no apparent prompt — looks like "the prompt isn't accepting".
            sys.stdout.flush()
            raw = input(f"{indent}{question} {suffix} ")
        except EOFError:
            # Non-interactive context — abort cleanly with a hint so the
            # user sees what to do (the calling command's --yes flag).
            print()
            print(f"{indent}Non-interactive shell — pass --yes to skip the prompt.")
            return False
        except KeyboardInterrupt:
            # Ctrl+C: return False cleanly. The caller is responsible for
            # turning that into a user-facing "Aborted." message + exit code.
            print()
            return False
        answer = raw.strip().lower()
        if answer == "":
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        # Unexpected input — reprompt (the Bug 22 fix). The previous code
        # silently returned False here, which the user reads as "Y didn't
        # work" instead of "I typed something funky".
        print(f"{indent}Please answer 'y' or 'n'.")


def confirm_typed(question: str, expected: str, *, indent: str = "  ") -> bool:
    """2026-05-18 v2.1.2 Item 3d: ask the user to TYPE a specific word
    (literal, case-sensitive) to confirm a destructive operation.

    Used by `codevira reset` so a slip of the hand can't trash decisions
    on a `y` keystroke. Field-test reports + your own feedback flagged
    `heal`/`reset` as the data-loss footgun; this prompt makes accidental
    triggering structurally much harder.

    Behaviour:
      - Renders the question, then `Type '<expected>' to proceed (or
        anything else to abort): `
      - Returns True only if the user types `expected` exactly (after
        strip()).
      - On EOFError (non-interactive) → return False with a hint to use
        --yes.
      - On Ctrl+C → return False cleanly.

    Args:
        question: prompt text the user sees first.
        expected: the literal word the user must type (e.g. "reset",
                  "vectors", "all"). Case-sensitive by design — the user
                  must DELIBERATELY type it.
        indent: leading whitespace on prompt lines (default two spaces).
    """
    try:
        sys.stdout.flush()
        print(f"{indent}{question}")
        raw = input(
            f"{indent}Type '{expected}' to proceed (or anything else to abort): "
        )
    except EOFError:
        print()
        print(
            f"{indent}Non-interactive shell — pass --yes to skip the typed confirmation."
        )
        return False
    except KeyboardInterrupt:
        print()
        return False
    if raw.strip() == expected:
        return True
    print(f"{indent}Did not type {expected!r} — aborted.")
    return False
