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
