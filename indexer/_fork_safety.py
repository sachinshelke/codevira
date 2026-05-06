"""macOS fork-safety init for sentence-transformers + chromadb stack.

Imported at indexer-package load time, BEFORE any chromadb / torch /
sentence_transformers import. Sets the env vars + multiprocessing start
method that prevent native segfaults on Apple Silicon, where libdispatch
and the Objective-C runtime fork-with-libdispatch unsafely.

Background — Bug 7 from Sachin's dogfood (2026-05-06):
    Segmentation fault during `codevira index` on AgentStore project,
    macOS Apple Silicon (3.13.7). Crash happened immediately after
    sentence-transformers reported "Loading weights: 100%". Stack
    trace pointed at the Python multiprocessing/loky shutdown path
    leaking semaphores. Classic fork() + libdispatch crash.

The four mitigations applied here are independently documented as fixes
in the PyTorch / Hugging Face / chromadb issue trackers:

  - ``OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES`` — bypasses the macOS
    +[__NSCFConstantString initialize] crash when forking a process
    that already loaded Foundation.
  - ``TOKENIZERS_PARALLELISM=false`` — the HuggingFace tokenizers
    crate uses a Rust-side rayon threadpool that is unsafe under
    Python's fork() multiprocessing.
  - ``OMP_NUM_THREADS=1`` — many macOS installs end up with both
    libomp (clang) and libiomp (Intel MKL); fighting OpenMP runtimes
    can crash. One thread sidesteps the conflict.
  - ``multiprocessing.set_start_method("spawn", force=True)`` —
    Python 3.8+ already defaults to spawn on macOS, but joblib's
    loky backend can flip this back to fork during pool init.

This module only acts on macOS. On Linux/Windows it's a no-op so we
don't waste a startup penalty on platforms that don't need it.

Idempotent: setdefault means an explicit user override (e.g. in CI
or for debugging) is preserved.
"""
from __future__ import annotations

import os
import sys


def init_fork_safety() -> None:
    """Apply macOS fork-safety mitigations. Safe to call repeatedly."""
    if sys.platform != "darwin":
        return

    os.environ.setdefault("OBJC_DISABLE_INITIALIZE_FORK_SAFETY", "YES")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("OMP_NUM_THREADS", "1")

    # Switch multiprocessing to spawn. set_start_method raises RuntimeError
    # if a context was already created elsewhere — that's fine, we just
    # leave whatever's already in place.
    try:
        import multiprocessing
        current = multiprocessing.get_start_method(allow_none=True)
        if current != "spawn":
            multiprocessing.set_start_method("spawn", force=True)
    except Exception:
        # Defensive: never let fork-safety init crash the process. If
        # set_start_method fails the env vars alone are usually enough.
        pass


# Apply on import. The indexer package's __init__.py imports this module
# so fork-safety is in place before any chromadb / sentence-transformers
# import happens downstream.
init_fork_safety()
