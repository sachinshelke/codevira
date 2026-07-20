"""
test_path_glob_and_scrub.py — v3.7.1 read-layer defects 1 and 6.

**Literal paths were treated as glob patterns.** ``list_all`` passed the
caller's path straight into ``fnmatch`` as a PATTERN, so any path containing
``[ ] * ?`` failed to match itself. ``decision_lock`` passes the concrete target
path, so a decision recorded against ``app/[slug]/page.tsx`` was BOTH invisible
to ``list_decisions`` AND silently unprotected — no error on either side. That
is every dynamic route in Next.js / SvelteKit / Remix.

**record_many skipped secret scrubbing.** ``record()`` runs
``sanitize.scrub_sensitive``; ``record_many()`` did not. A credential in a
bulk-imported decision was written verbatim to decisions.jsonl, the FTS5 index,
the digest and AGENTS.md — a file that is COMMITTED, so a secret could reach git
through the memory layer.
"""

from __future__ import annotations

import pytest

from mcp_server.storage import decisions_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / ".codevira").mkdir(parents=True)
    (proj / ".codevira" / "config.yaml").write_text("schema_version: 1\n")
    monkeypatch.setenv("CODEVIRA_PROJECT_DIR", str(proj))
    import mcp_server.paths as paths_mod

    paths_mod.set_project_dir(proj)
    paths_mod.reset_pinned_root()
    paths_mod.invalidate_data_dir_cache()
    yield proj
    paths_mod._project_dir_override = None
    paths_mod.reset_pinned_root()
    paths_mod.invalidate_data_dir_cache()


GLOB_PATHS = [
    "app/[slug]/page.tsx",  # Next.js dynamic route
    "app/[...catchall]/route.ts",  # catch-all
    "src/routes/[id]/+page.svelte",  # SvelteKit
    "what?.py",  # literal question mark
    "star*file.py",  # literal asterisk
]


class TestLiteralPathsWithGlobChars:
    @pytest.mark.parametrize("fp", GLOB_PATHS)
    def test_decision_is_findable_by_its_own_path(self, store, fp):
        """THE regression: the decision could not be found by its exact path."""
        decisions_store.record(decision=f"rule for {fp}", file_path=fp)

        result = decisions_store.list_all(file_pattern=fp)

        assert result["total"] == 1, f"decision on {fp} is invisible to its own path"

    def test_protected_decision_on_a_dynamic_route_is_visible(self, store):
        """decision_lock filters by the concrete target path, so invisibility
        here means the do_not_revert lock silently never fires."""
        fp = "apps/console/app/(auth)/invite/[token]/page.tsx"
        decisions_store.record(
            decision="invite page must keep server-side token validation",
            file_path=fp,
            do_not_revert=True,
        )

        result = decisions_store.list_all(file_pattern=fp, protected_only=True)

        assert (
            result["total"] == 1
        ), "protected decision on a dynamic route is invisible"

    def test_glob_patterns_still_work(self, store):
        """Exact-match-first must not break intentional glob callers."""
        decisions_store.record(decision="a", file_path="src/one.py")
        decisions_store.record(decision="b", file_path="src/two.py")
        decisions_store.record(decision="c", file_path="other/three.py")

        assert decisions_store.list_all(file_pattern="src/*.py")["total"] == 2

    def test_non_matching_path_still_excluded(self, store):
        decisions_store.record(decision="a", file_path="app/[slug]/page.tsx")
        assert decisions_store.list_all(file_pattern="app/other/page.tsx")["total"] == 0


class TestRecordManyScrubsSecrets:
    def test_secret_is_redacted(self, store):
        """A credential must never reach the store via the bulk path."""
        secret = "sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJKKKKLLLL"
        decisions_store.record_many(
            [{"decision": f"use {secret} for auth", "tags": ["x"]}]
        )

        raw = (store / ".codevira" / "decisions.jsonl").read_text()
        assert secret not in raw, "record_many wrote a raw credential to the store"

    def test_secret_in_context_is_redacted(self, store):
        secret = "sk-ant-api03-ZZZZYYYYXXXXWWWWVVVVUUUUTTTTSSSSRRRRQQQQPPPP"
        decisions_store.record_many(
            [{"decision": "rotate the key", "context": f"old value was {secret}"}]
        )

        raw = (store / ".codevira" / "decisions.jsonl").read_text()
        assert secret not in raw, "record_many wrote a raw credential in context"

    def test_normal_text_is_unchanged(self, store):
        decisions_store.record_many([{"decision": "use bcrypt for passwords"}])
        raw = (store / ".codevira" / "decisions.jsonl").read_text()
        assert "use bcrypt for passwords" in raw
