"""
test_concurrent_writes.py — v3.0.0 round-2 regression guard.

These tests prove that the cache-write helpers (manifest.yaml,
AGENTS.md, digest.jsonl) stay consistent under concurrent
``record_decision`` calls. They protect against two distinct bug
shapes both surfaced during the round-2 G5 audit:

1. **Atomic-write race on the temp suffix.** The original
   ``<path>.tmp`` was a fixed name; two threads' ``replace()`` calls
   raced on the rename target. Fixed by per-write ``tempfile.mkstemp``
   in manifest.py / agents_md_generator.py / digest.py.

2. **Read-modify-write lost updates in manifest.incremental_add.**
   Each call did load → mutate → save without locking. 10 concurrent
   calls all loaded the same starting state, mutated their copies,
   then raced on save — last writer won, losing the other 9 updates.
   Fixed by fcntl.flock around the whole read-modify-write.

The decisions themselves were always safe (jsonl_store.append uses
fcntl-locked I/O); only the CACHE files lost data. Per the P9
contract, decisions in the canonical JSONL win, and the cache can
always be rebuilt via ``codevira sync``. But silent cache divergence
is a bad UX shape — these tests assert the cache stays in step.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml


@pytest.fixture
def isolated_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Pin .codevira/ + ~/.codevira/ under tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    project = tmp_path / "proj"
    project.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    from mcp_server import paths as paths_mod

    paths_mod.set_project_dir(project)
    paths_mod.invalidate_data_dir_cache()
    monkeypatch.setattr(paths_mod, "get_global_home", lambda: fake_home / ".codevira")

    from mcp_server.storage import paths as store_paths

    store_paths.ensure_dirs()
    (project / "AGENTS.md").write_text(
        "<!-- codevira:begin -->\n<!-- codevira:end -->\n"
    )
    return project


def _capture_warnings():
    """Capture decisions_store warnings so we can assert on race
    diagnostics (the pre-fix code emitted '[Errno 2] No such file'
    when atomic-rename collided)."""
    captured: list[str] = []

    class CaptureHandler(logging.Handler):
        def emit(self, rec: logging.LogRecord) -> None:
            captured.append(rec.getMessage())

    logger = logging.getLogger("mcp_server.storage.decisions_store")
    handler = CaptureHandler()
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    return captured, handler, logger


# =====================================================================
# Atomic-write race regression
# =====================================================================


class TestAtomicWriteRace:
    """Verifies the per-write unique-tmp fix in manifest.save +
    agents_md_generator._merge_into_file + digest.regenerate.

    Pre-fix: 50 concurrent record_decision calls would each trigger
    a regenerate(), and threads would race on a fixed ``<path>.tmp``
    — manifesting as warnings like
    ``[Errno 2] No such file or directory: '...manifest.yaml.tmp'``.

    Fix verified: per-call unique tmp via tempfile.mkstemp.
    """

    def test_no_atomic_rename_warnings_under_concurrency(
        self,
        isolated_project: Path,
    ) -> None:
        captured, handler, logger = _capture_warnings()
        try:
            from mcp_server.storage import decisions_store

            def write_one(i: int) -> str:
                return decisions_store.record(
                    decision=f"concurrent decision {i}",
                    file_path=f"thread_{i}.py",
                    tags=["concurrent"],
                )

            n = 50
            with ThreadPoolExecutor(max_workers=10) as pool:
                ids = list(pool.map(write_one, range(n)))

            # All decisions land with unique IDs (proves jsonl_store
            # locking + the atomic-write fix in cache files holds).
            assert len(ids) == n
            assert len(set(ids)) == n, f"duplicate IDs in: {ids}"

            # Zero atomic-rename race warnings.
            race_warns = [
                m
                for m in captured
                if "[Errno 2]" in m or "No such file or directory" in m
            ]
            assert race_warns == [], (
                f"Atomic-rename race regression detected. "
                f"{len(race_warns)} 'No such file' warnings during "
                f"{n} concurrent record_decision calls:\n"
                + "\n".join(f"  - {m[:140]}" for m in race_warns[:5])
            )
        finally:
            logger.removeHandler(handler)


# =====================================================================
# Read-modify-write lost-update regression
# =====================================================================


class TestManifestLostUpdates:
    """Verifies the fcntl-flock fix in manifest.incremental_add.

    Pre-fix: 10 concurrent incremental_add calls would each load the
    same starting manifest, mutate their copy, then race on save —
    last writer won, dropping the other 9 updates. Manifest counts
    would lag behind the JSONL canonical store.

    Fix verified: an exclusive flock around the whole read-modify-write
    serializes concurrent updates.
    """

    def test_manifest_matches_jsonl_after_concurrent_writes(
        self,
        isolated_project: Path,
    ) -> None:
        from mcp_server.storage import decisions_store

        def write_one(i: int) -> Any:
            return decisions_store.record(
                decision=f"concurrent decision {i}",
                file_path=f"thread_{i}.py",
                tags=["concurrent"],
            )

        n = 50
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(write_one, range(n)))

        cv = isolated_project / ".codevira"
        # Truth: count base records in JSONL (non-amendment).
        lines = [
            json.loads(line)
            for line in (cv / "decisions.jsonl").read_text().splitlines()
            if line.strip()
        ]
        base = [r for r in lines if not r.get("_amendment_to_id")]
        assert len(base) == n

        # Manifest must match — pre-fix this would lag (e.g. 37/50).
        manifest = yaml.safe_load((cv / "manifest.yaml").read_text())
        assert manifest["total_decisions"] == n, (
            f"manifest dropped {n - manifest['total_decisions']} updates "
            f"under concurrency — lock around incremental_add regressed"
        )
        assert manifest["active_decisions"] == n

        # Tag bucket: every decision had tag 'concurrent' → bucket = n
        tag_bucket = manifest.get("tags", {}).get("concurrent", [])
        assert len(tag_bucket) == n, (
            f"manifest tag bucket missed {n - len(tag_bucket)} of {n} "
            f"decisions — lock around tag-bucket append regressed"
        )

        # File bucket: each decision had a unique file_path
        assert len(manifest.get("files", {})) == n


# =====================================================================
# Data-loss invariant (canonical-store always wins, P9)
# =====================================================================


class TestRoadmapLostUpdates:
    """Verifies the v3.0.0 round-3 atomic-write + flock fix in
    ``mcp_server/tools/roadmap.py``.

    Pre-fix the ``_save_roadmap`` helper did an unguarded
    ``with open(... "w") + yaml.dump``. Two concurrent
    ``update_phase_status`` calls (which happens at session-end when
    two IDEs are wrapping up together) both read the same starting
    state and the last save() won — losing the other update.

    Fix verified: ``_roadmap_lock()`` wraps each mutation in
    ``atomic.file_lock`` (Posix flock + Windows sentinel fallback)
    AND ``_save_roadmap`` now goes through ``atomic.atomic_write_text``.

    This test pins the same shape as ``TestManifestLostUpdates``,
    just on a different storage surface.
    """

    def test_no_lost_phases_under_concurrent_add(
        self,
        isolated_project: Path,
    ) -> None:
        from mcp_server.tools import roadmap
        from mcp_server.tools.roadmap import _roadmap_file

        def add_one(i: int) -> dict:
            return roadmap.add_phase(
                phase=100 + i,  # 100..149 — outside the placeholder's "1"
                name=f"Concurrent phase {i}",
                description=f"added by worker {i}",
            )

        n = 50
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(add_one, range(n)))

        # Every call must have returned success.
        failures = [r for r in results if not r.get("success")]
        assert failures == [], (
            f"add_phase failed for {len(failures)} of {n} calls "
            f"under concurrency — race detected. Sample: {failures[:3]}"
        )

        # roadmap.yaml must reflect ALL 50 phases (pre-fix: ~37 of 50,
        # last-writer-wins).
        rm_path = _roadmap_file()
        assert rm_path.is_file(), f"roadmap.yaml missing: {rm_path}"
        rm = yaml.safe_load(rm_path.read_text())
        upcoming = rm.get("upcoming_phases") or []
        added_numbers = {p.get("phase") for p in upcoming}
        expected = {100 + i for i in range(n)}
        missing = expected - added_numbers
        assert missing == set(), (
            f"roadmap dropped {len(missing)} of {n} phases under "
            f"concurrency — lock around _save_roadmap regressed. "
            f"Missing: {sorted(missing)[:10]}..."
        )

    def test_concurrent_update_phase_status_no_partial_yaml(
        self,
        isolated_project: Path,
    ) -> None:
        """A concurrent ``update_phase_status`` storm must never produce
        a partial or malformed roadmap.yaml. Pre-fix the un-atomic
        write could leave a half-written file if a crash hit between
        the open(..., 'w') truncate and the dump finish."""
        from mcp_server.tools import roadmap
        from mcp_server.tools.roadmap import _roadmap_file

        # Seed with one real phase.
        roadmap.add_phase(
            phase=200,
            name="Seed",
            description="seed phase for update_phase_status race test",
        )

        def update_one(i: int) -> dict:
            return roadmap.update_phase_status(
                status=("pending" if i % 2 == 0 else "in_progress"),
            )

        n = 50
        with ThreadPoolExecutor(max_workers=10) as pool:
            list(pool.map(update_one, range(n)))

        # File must still be valid YAML — a partial write would
        # surface as yaml.YAMLError here.
        rm_path = _roadmap_file()
        rm = yaml.safe_load(rm_path.read_text())
        # Status must be one of the two values; never None / missing.
        status = rm.get("current_phase", {}).get("status")
        assert status in ("pending", "in_progress"), (
            f"expected pending/in_progress, got {status!r} — implies "
            f"a partial write or last-writer corruption"
        )


class TestCanonicalStoreSurvivesEvenIfCacheFails:
    """Even when the cache update fails (simulated via a corrupted
    manifest.yaml), the decision MUST still land in decisions.jsonl.
    This is the P9 contract — never block a user write on a cache
    failure.

    This was already implicit in the existing code path (P9 wrapping)
    but the v3.0.0 round-2 fixes added new code paths; this test pins
    the invariant in place explicitly so a future refactor can't
    quietly drop it.
    """

    def test_decision_persists_when_manifest_corrupt(
        self,
        isolated_project: Path,
    ) -> None:
        from mcp_server.storage import decisions_store, paths as store_paths

        # Corrupt the manifest so the incremental update raises.
        store_paths.manifest_path().write_text("this is not valid yaml: : :")

        decision_id = decisions_store.record(
            decision="must persist",
            file_path="x.py",
            do_not_revert=True,
        )
        assert decision_id, "record_decision must succeed despite cache failure"

        # JSONL has the record.
        lines = [
            json.loads(line)
            for line in store_paths.decisions_path().read_text().splitlines()
            if line.strip()
        ]
        ids = {r.get("id") for r in lines}
        assert decision_id in ids
