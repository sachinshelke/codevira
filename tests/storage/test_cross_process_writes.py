"""
test_cross_process_writes.py — proves the fcntl.flock contract holds
across PROCESS boundaries, not just threads.

# Why this matters

The in-process locking story (TestManifestLostUpdates,
TestRoadmapLostUpdates in test_concurrent_writes.py) only proves the
threading.Lock side of the safety. In production, two codevira MCP
server processes commonly run on the same machine:
- Claude Code spawns one
- Cursor (or Antigravity, or Windsurf) spawns another
- Both wrap up at session-end at roughly the same time

If the flock contract doesn't hold across processes, the lost-update
bug returns. This test makes that risk explicit by spawning real
subprocesses (via multiprocessing) and asserting no updates are lost.

# Cost

Slower than the threaded tests (process spawn overhead ~50-200 ms
each), so we use a smaller fan-out (20 processes vs 50 threads). Still
catches the bug — any process-level race would manifest as a missing
phase in roadmap.yaml, just like the threaded case.
"""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest
import yaml


def _record_decision_worker(
    project_dir: str,
    fake_home: str,
    i: int,
) -> str | None:
    """Worker run in a SUBPROCESS. Records one decision against the
    given project + returns the decision id (or None on failure)."""
    # Re-establish env in the child process.
    os.environ["HOME"] = fake_home
    Path.home = classmethod(lambda cls: Path(fake_home))  # type: ignore[assignment]

    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(Path(project_dir))
    paths_mod.invalidate_data_dir_cache()
    paths_mod.get_global_home = lambda: Path(fake_home) / ".codevira"  # type: ignore[assignment]

    from mcp_server.storage import paths as store_paths

    store_paths.ensure_dirs()

    from mcp_server.storage import decisions_store

    try:
        return decisions_store.record(
            decision=f"cross-process decision {i}",
            file_path=f"xproc_{i}.py",
            tags=["xproc"],
        )
    except Exception as exc:  # noqa: BLE001 — child must never crash the harness
        return f"ERROR: {exc}"


def _add_phase_worker(
    project_dir: str,
    fake_home: str,
    i: int,
) -> dict:
    """Worker run in a SUBPROCESS. Calls roadmap.add_phase()."""
    os.environ["HOME"] = fake_home
    Path.home = classmethod(lambda cls: Path(fake_home))  # type: ignore[assignment]

    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(Path(project_dir))
    paths_mod.invalidate_data_dir_cache()
    paths_mod.get_global_home = lambda: Path(fake_home) / ".codevira"  # type: ignore[assignment]

    from mcp_server.storage import paths as store_paths

    store_paths.ensure_dirs()

    from mcp_server.tools import roadmap

    try:
        return roadmap.add_phase(
            phase=300 + i,
            name=f"Cross-process phase {i}",
            description=f"added by subprocess {i}",
        )
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}


@pytest.fixture
def shared_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    """Set up a v3.0.0 project + fake home in tmp_path. Returns
    ``(project_dir, fake_home)``.

    Uses ``monkeypatch`` so all attribute swaps are reverted at test
    teardown — direct assignment to ``Path.home`` would leak across
    tests and break other suites that depend on the real $HOME.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    # Pre-create the .codevira/ dirs in the centralized location so the
    # subprocesses don't all race to init it.
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    monkeypatch.setattr(paths_mod, "get_global_home", lambda: fake_home / ".codevira")
    from mcp_server.storage import paths as store_paths

    store_paths.ensure_dirs()

    return project, fake_home


# multiprocessing on macOS needs spawn (default since 3.8). We're
# explicit so the test behaves the same in CI on linux.
_MP_CTX = multiprocessing.get_context("spawn")


class TestCrossProcessConcurrentWrites:
    @pytest.mark.timeout(120)
    def test_decisions_jsonl_under_20_subprocess_record_calls(
        self,
        shared_project: tuple[Path, Path],
    ) -> None:
        project, fake_home = shared_project
        n = 20

        with _MP_CTX.Pool(processes=4) as pool:
            results = pool.starmap(
                _record_decision_worker,
                [(str(project), str(fake_home), i) for i in range(n)],
            )

        # All subprocesses returned a real decision id (no errors).
        errors = [r for r in results if r is None or r.startswith("ERROR")]
        assert (
            errors == []
        ), f"{len(errors)} of {n} subprocess record() calls failed: {errors[:3]}"
        # All IDs are unique — jsonl_store's fcntl.flock crossed
        # process boundaries successfully.
        ids = [r for r in results if r and not r.startswith("ERROR")]
        assert len(set(ids)) == n, (
            f"duplicate decision IDs across processes: "
            f"{n - len(set(ids))} collisions in {ids}"
        )

    @pytest.mark.timeout(120)
    def test_roadmap_yaml_under_20_subprocess_add_phase(
        self,
        shared_project: tuple[Path, Path],
    ) -> None:
        project, fake_home = shared_project
        n = 20

        with _MP_CTX.Pool(processes=4) as pool:
            results = pool.starmap(
                _add_phase_worker,
                [(str(project), str(fake_home), i) for i in range(n)],
            )

        failures = [r for r in results if not r.get("success")]
        assert failures == [], (
            f"{len(failures)} of {n} subprocess add_phase calls failed: "
            f"{failures[:3]}"
        )

        # All 20 phases must be in the roadmap. Pre-fix (no flock or
        # non-cross-process flock): the last write wins per process,
        # so fewer than 20 phases would land.
        from mcp_server.tools.roadmap import _roadmap_file

        rm = yaml.safe_load(_roadmap_file().read_text())
        added = {p.get("phase") for p in (rm.get("upcoming_phases") or [])}
        expected = {300 + i for i in range(n)}
        missing = expected - added
        assert missing == set(), (
            f"cross-process flock failed: roadmap dropped {len(missing)} "
            f"of {n} phases. Missing: {sorted(missing)[:5]}..."
        )
