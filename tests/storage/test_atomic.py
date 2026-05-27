"""
test_atomic.py — unit tests for storage.atomic helpers.

Pins:
- ``atomic_write_text`` is crash-safe: tmp lives in same dir, fsync
  attempted, replace is the rename, tmp cleaned on failure.
- ``file_lock`` serializes concurrent writers in-process AND across
  processes (cross-process tested in test_cross_process_writes.py).
- Mode bits applied after rename for secret-handling sites.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mcp_server.storage import atomic


# =====================================================================
# atomic_write_text / atomic_write_bytes
# =====================================================================


class TestAtomicWriteText:
    def test_basic_write_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        n = atomic.atomic_write_text(target, "hello world\n")
        assert n == len("hello world\n".encode("utf-8"))
        assert target.read_text() == "hello world\n"

    def test_utf8_default(self, tmp_path: Path) -> None:
        target = tmp_path / "emoji.txt"
        atomic.atomic_write_text(target, "🎉 codevira ✓")
        assert target.read_text(encoding="utf-8") == "🎉 codevira ✓"

    def test_overwrite_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        target.write_text("old content")
        atomic.atomic_write_text(target, "new content")
        assert target.read_text() == "new content"

    def test_mkdir_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c" / "out.txt"
        atomic.atomic_write_text(target, "deep")
        assert target.read_text() == "deep"

    def test_mode_applied(self, tmp_path: Path) -> None:
        target = tmp_path / "secret.txt"
        atomic.atomic_write_text(target, "shh", mode=0o600)
        st_mode = target.stat().st_mode & 0o777
        assert st_mode == 0o600

    def test_no_lingering_tmp_files_on_success(self, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        atomic.atomic_write_text(target, "content")
        # No <name>.* files left behind in the dir.
        leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".out.txt.")]
        assert leftovers == [], f"tmp files leaked: {leftovers}"

    def test_concurrent_writes_no_collision(self, tmp_path: Path) -> None:
        """50 threads writing to the SAME target produce no
        FileNotFoundError; final content is one of the inputs (no
        partial content; the last-completed write wins by design)."""
        target = tmp_path / "shared.txt"

        def write_one(i: int) -> None:
            atomic.atomic_write_text(target, f"content-{i}\n")

        n = 50
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(write_one, range(n)))

        # Final content matches one of the writes (no partial / no
        # FileNotFoundError on the rename target).
        final = target.read_text()
        assert final.startswith("content-")
        assert final.endswith("\n")


class TestAtomicWriteBytes:
    def test_binary_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "out.bin"
        payload = bytes(range(256))
        n = atomic.atomic_write_bytes(target, payload)
        assert n == 256
        assert target.read_bytes() == payload


# =====================================================================
# file_lock
# =====================================================================


class TestFileLock:
    def test_serializes_in_process_writers(self, tmp_path: Path) -> None:
        """Two threads grabbing file_lock on the same path serialize
        — provable by recording entry/exit order and asserting
        non-overlap."""
        target = tmp_path / "shared.lock"
        events: list[tuple[str, int]] = []
        events_guard = threading.Lock()

        def critical_section(worker_id: int) -> None:
            with atomic.file_lock(target):
                with events_guard:
                    events.append(("enter", worker_id))
                # Hold the lock long enough that a real concurrent
                # entry would interleave.
                import time as _t

                _t.sleep(0.05)
                with events_guard:
                    events.append(("exit", worker_id))

        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(critical_section, range(10)))

        # For each worker, its enter must be followed by its exit
        # (no other worker between).
        for i in range(0, len(events), 2):
            assert events[i][0] == "enter"
            assert events[i + 1][0] == "exit"
            assert (
                events[i][1] == events[i + 1][1]
            ), f"workers interleaved at events[{i}:{i + 2}]: {events[i:i + 2]}"

    def test_creates_anchor_file_if_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "does-not-exist.yaml"
        assert not target.exists()
        with atomic.file_lock(target):
            # Inside the lock, the anchor must exist.
            assert target.exists()

    def test_release_on_exception(self, tmp_path: Path) -> None:
        """If the body raises, the lock must release — provable
        because a second acquire shouldn't deadlock."""
        target = tmp_path / "lock.yaml"

        with pytest.raises(RuntimeError):
            with atomic.file_lock(target):
                raise RuntimeError("body failed")

        # If the lock leaked, this would deadlock. Pytest's timeout
        # config (configured at suite level) would fire.
        with atomic.file_lock(target):
            pass  # second acquire OK

    def test_windows_sentinel_path_runs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Monkey-patch sys.platform to simulate Windows and verify the
        sentinel-file fallback path executes without error.

        We can't actually run on Windows here, but we can prove the
        Windows-only codepath is well-formed (no NameError / undefined
        attribute / syntax bugs). If the v3.0.0 spec ever loses its
        only-Posix-tested status, this gives a small smoke signal.
        """
        import sys

        monkeypatch.setattr(sys, "platform", "win32")
        target = tmp_path / "winlock.yaml"

        # Single acquire-release should leave no sentinel file behind.
        with atomic.file_lock(target):
            sentinel = target.with_suffix(target.suffix + ".lock")
            assert sentinel.exists(), (
                "Windows sentinel codepath should create the .lock "
                "marker inside the with-block"
            )
        sentinel = target.with_suffix(target.suffix + ".lock")
        assert (
            not sentinel.exists()
        ), "Windows sentinel must be unlinked on context exit"
