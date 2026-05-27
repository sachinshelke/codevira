"""
tests/e2e/fixtures/conftest.py — pytest collection guard.

The directories under this folder are FIXTURE PROJECTS — fake repos
that `tests/e2e/test_first_contact.py` runs codevira against. They
contain ``tests/`` subdirs with `test_*.py` files of their own (those
are part of the fixture, simulating a real project under test). When
pytest collects from the repo root it tries to import those nested
test files; they fail because they import from the fixture's
``src/`` which isn't on PYTHONPATH (and isn't supposed to be — they
exist to be exercised through codevira's subprocess invocation, not
as importable Python).

The ``collect_ignore`` hook tells pytest to skip everything below
this directory during collection. The real e2e tests in
``tests/e2e/test_first_contact.py`` still run them via subprocess,
which is the only correct way to exercise a fresh-project fixture.
"""

from __future__ import annotations

import os

# Skip every direct subdirectory of tests/e2e/fixtures (the fixture
# project roots). Pytest already does NOT recurse into a dir that has
# its own conftest declaring ignores, but we use the explicit
# collect_ignore list so a future contributor reading this file
# immediately sees what's being skipped and why.
collect_ignore = [
    name
    for name in os.listdir(os.path.dirname(__file__))
    if os.path.isdir(os.path.join(os.path.dirname(__file__), name))
]
