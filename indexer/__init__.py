# Codevira Indexer package
#
# IMPORTANT: ``_fork_safety`` must be imported FIRST so that its env-var
# and multiprocessing-start-method side effects run before any module in
# this package (or downstream) imports chromadb / sentence-transformers /
# torch. Tracked as Bug 7 in v2.0-rc.3 close-out.
from indexer import _fork_safety  # noqa: F401
