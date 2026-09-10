"""
test_register_all.py — v3.8.0 `codevira register-all`.

The clean-slate, one-MCP-per-project model that replaces the fragile shared
auto-detect entry (D000128). Guarantees pinned here:
  1. slug() names each project uniquely;
  2. rewriting a surface ZEROES every codevira* key and writes one named
     entry per project, while preserving unrelated servers;
  3. discovery excludes nested monorepo sub-stores;
  4. register_all() spans Claude Code (project-scope) + Desktop (top-level).
"""

from __future__ import annotations

import json
from pathlib import Path


from mcp_server import register_all as ra


class TestSlug:
    def test_basename_lowercased(self):
        assert ra.slug("/a/b/UDAP") == "codevira-udap"

    def test_spaces_and_specials_sanitized(self):
        assert ra.slug("/x/AI Business Builder") == "codevira-ai-business-builder"

    def test_distinct_per_project(self):
        assert ra.slug("/x/LH") != ra.slug("/x/UDAP")


class TestNamedEntry:
    def test_pins_project_dir(self):
        e = ra._named_entry(
            "/bin/codevira", "/usr/bin/python", "/p/UDAP", "claude_code"
        )
        assert e["args"] == ["--project-dir", "/p/UDAP"]
        assert e["env"]["CODEVIRA_IDE"] == "claude_code"
        assert e["command"] == "/bin/codevira"

    def test_python_fallback_uses_module(self):
        e = ra._named_entry(
            "/usr/bin/python", "/usr/bin/python", "/p/LH", "claude_code"
        )
        assert e["args"] == ["-m", "mcp_server", "--project-dir", "/p/LH"]


class TestRewriteSurface:
    def test_zeroes_codevira_adds_named_preserves_others(self, tmp_path):
        cfg = tmp_path / "cc.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "codevira": {"command": "x", "args": []},  # bare -> removed
                        "other-server": {"command": "keep"},  # preserved
                    },
                    "projects": {
                        "/p/LH": {
                            "mcpServers": {
                                "codevira": {"args": ["--project-dir", "/wrong"]}
                            }
                        },
                    },
                }
            )
        )
        r = ra._rewrite_surface(
            cfg,
            ["/p/LH", "/p/UDAP"],
            "/bin/codevira",
            "/usr/bin/python",
            "claude_code",
            scope=ra.SCOPE_PER_PROJECT,
            dry_run=False,
        )
        assert r["removed"] == 2  # bare + the project-scoped codevira
        d = json.loads(cfg.read_text())
        # unrelated server preserved, no generic 'codevira' left at top level
        assert d["mcpServers"]["other-server"] == {"command": "keep"}
        assert "codevira" not in d["mcpServers"]
        # each project got its OWN named entry pinned to itself
        assert d["projects"]["/p/LH"]["mcpServers"]["codevira-lh"]["args"] == [
            "--project-dir",
            "/p/LH",
        ]
        assert d["projects"]["/p/UDAP"]["mcpServers"]["codevira-udap"]["args"] == [
            "--project-dir",
            "/p/UDAP",
        ]
        # a backup was written
        assert list(tmp_path.glob("cc.json.bak-registerall-*"))

    def test_flat_scope_fans_out_for_clients_without_dynamic_binding(self, tmp_path):
        """Antigravity: flat config, but no per-tool-call binding, so a single
        entry could not resolve any project. Pinned fan-out is correct here."""
        cfg = tmp_path / "antigravity.json"
        cfg.write_text(
            json.dumps(
                {"mcpServers": {"codevira": {"args": ["--project-dir", "/only/LH"]}}}
            )
        )
        ra._rewrite_surface(
            cfg,
            ["/p/LH", "/p/UDAP"],
            "/bin/codevira",
            "/usr/bin/python",
            "antigravity",
            scope=ra.SCOPE_FLAT,
            dry_run=False,
        )
        d = json.loads(cfg.read_text())
        assert set(k for k in d["mcpServers"] if "codevira" in k) == {
            "codevira-lh",
            "codevira-udap",
        }


class TestClaudeDesktopGetsExactlyOneEntry:
    """Claude Desktop's ``mcpServers`` map is FLAT — every entry loads in every
    conversation, with no project scoping.

    Fanning out one pinned entry per project therefore spawned a server per
    project and advertised tool_count x projects tools in EVERY conversation
    (12 servers / 432 tools measured on the reference machine), all but one
    bound to a project the user was not in. Desktop is also the only client
    with per-tool-call project binding, so one unpinned entry is both
    sufficient and what the rest of the product already assumes:
    ``_maybe_bind_from_tool_path`` is gated on ``CODEVIRA_IDE=claude_desktop``,
    and the opt-in gate's docstring says a "single global MCP registration
    stays fully inert outside opted-in projects".
    """

    def _write(self, tmp_path, n_projects=12):
        cfg = tmp_path / "desktop.json"
        cfg.write_text(json.dumps({"mcpServers": {}, "preferences": {"x": 1}}))
        ra._rewrite_surface(
            cfg,
            [f"/p/proj{i}" for i in range(n_projects)],
            "/bin/codevira",
            "/usr/bin/python",
            "claude_desktop",
            scope=ra.SCOPE_SINGLE_DYNAMIC,
            dry_run=False,
        )
        return json.loads(cfg.read_text())

    def test_twelve_projects_yield_one_entry(self, tmp_path):
        """Fails on the pre-fix code, which wrote 12."""
        d = self._write(tmp_path)
        cv = [k for k in d["mcpServers"] if "codevira" in k.lower()]
        assert cv == ["codevira"], (
            f"Claude Desktop got {len(cv)} codevira entries: {cv}. Its config is "
            f"flat, so every one of them loads in every conversation."
        )

    def test_the_entry_is_not_pinned_to_a_project(self, tmp_path):
        """A --project-dir here would pin every conversation to ONE project,
        which is worse than the fan-out it replaces."""
        e = self._write(tmp_path)["mcpServers"]["codevira"]
        assert "--project-dir" not in e["args"], e["args"]

    def test_the_entry_carries_the_ide_that_enables_binding(self, tmp_path):
        """_maybe_bind_from_tool_path returns early unless this is exact."""
        e = self._write(tmp_path)["mcpServers"]["codevira"]
        assert e["env"]["CODEVIRA_IDE"] == "claude_desktop"

    def test_unrelated_config_is_preserved(self, tmp_path):
        assert self._write(tmp_path).get("preferences") == {"x": 1}

    def test_a_prior_fan_out_is_cleaned_up(self, tmp_path):
        """Upgrading from the buggy shape must collapse the old entries."""
        cfg = tmp_path / "desktop.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "codevira-lh": {"args": ["--project-dir", "/p/LH"]},
                        "codevira-udap": {"args": ["--project-dir", "/p/UDAP"]},
                        "other-server": {"args": []},
                    }
                }
            )
        )
        r = ra._rewrite_surface(
            cfg,
            ["/p/LH", "/p/UDAP"],
            "/bin/codevira",
            "/usr/bin/python",
            "claude_desktop",
            scope=ra.SCOPE_SINGLE_DYNAMIC,
            dry_run=False,
        )
        d = json.loads(cfg.read_text())
        assert r["removed"] == 2 and r["added"] == 1
        assert [k for k in d["mcpServers"] if "codevira" in k] == ["codevira"]
        assert "other-server" in d["mcpServers"], "unrelated MCP must survive"


class TestSurfaceScopesAreDeclaredCorrectly:
    def test_each_surface_gets_the_scope_its_config_supports(self, monkeypatch):
        """The scope is a property of the CLIENT's config format. Collapsing
        'flat' and 'single-dynamic' into one boolean is what caused the bug."""
        seen = {}

        def fake_rewrite(path, projects, cmd, py, ide, *, scope, dry_run):
            seen[ide] = scope
            return {"removed": 0, "added": 0, "backup": "n/a"}

        monkeypatch.setattr(ra, "_rewrite_surface", fake_rewrite)
        monkeypatch.setattr(ra.Path, "is_file", lambda self: True)
        monkeypatch.setattr(
            ra, "discover_projects", lambda *a, **k: ra.Discovery(projects=["/p/LH"])
        )
        monkeypatch.setattr(ra, "_resolve_command", lambda: ("/bin/codevira", "/py"))
        ra.register_all()

        assert seen.get("claude_code") == ra.SCOPE_PER_PROJECT
        assert seen.get("claude_desktop") == ra.SCOPE_SINGLE_DYNAMIC
        if "antigravity" in seen:
            assert seen["antigravity"] == ra.SCOPE_FLAT

    def test_dry_run_writes_nothing(self, tmp_path):
        cfg = tmp_path / "cc.json"
        original = json.dumps({"mcpServers": {"codevira": {"args": []}}})
        cfg.write_text(original)
        ra._rewrite_surface(
            cfg,
            ["/p/LH"],
            "/bin/codevira",
            "/usr/bin/python",
            "claude_code",
            scope=ra.SCOPE_PER_PROJECT,
            dry_run=True,
        )
        assert cfg.read_text() == original
        assert not list(tmp_path.glob("cc.json.bak-*"))


class TestDiscoveryNestedExclusion:
    def _mk(self, root: Path, rel: str):
        d = root / rel
        (d / ".codevira").mkdir(parents=True, exist_ok=True)
        (d / ".codevira" / "decisions.jsonl").write_text("")
        return str(d.resolve())

    def test_excludes_nested_substores(self, tmp_path, monkeypatch):
        lh = self._mk(tmp_path, "Projects/LH")
        self._mk(tmp_path, "Projects/LH/packages/db")  # nested — must be excluded
        udap = self._mk(tmp_path, "Projects/UDAP")

        # no IDE registrations; drive discovery purely off a scan root.
        # tmp_path lives under /var/folders (a junk fragment), so disable the
        # junk filter for this synthetic tree.
        monkeypatch.setattr(
            ra, "_claude_global_config_path", lambda: tmp_path / "none.json"
        )
        monkeypatch.setattr(ra, "_antigravity_write_targets", lambda: [])
        monkeypatch.setattr(ra, "_is_junk", lambda p: False)

        disc = ra.discover_projects(extra_scan_roots=[str(tmp_path / "Projects")])
        assert lh in disc.projects and udap in disc.projects
        assert any("packages/db" in p for p in disc.nested_excluded)
        assert not any("packages/db" in p for p in disc.projects)


class TestRegisterAllEndToEnd:
    def test_spans_surfaces(self, tmp_path, monkeypatch):
        # real project dirs (discovery filters non-existent paths)
        lh = str((tmp_path / "LH"))
        (tmp_path / "LH").mkdir()
        udap = str((tmp_path / "UDAP"))
        (tmp_path / "UDAP").mkdir()
        cc = tmp_path / "claude.json"
        desktop = tmp_path / "desktop.json"
        cc.write_text(
            json.dumps(
                {
                    "mcpServers": {},
                    "projects": {
                        lh: {
                            "mcpServers": {"codevira": {"args": ["--project-dir", lh]}}
                        },
                        udap: {
                            "mcpServers": {
                                "codevira": {"args": ["--project-dir", udap]}
                            }
                        },
                    },
                }
            )
        )
        desktop.write_text(json.dumps({"mcpServers": {}}))

        monkeypatch.setattr(ra, "_claude_global_config_path", lambda: cc)
        monkeypatch.setattr(ra, "_claude_desktop_config_path", lambda: desktop)
        monkeypatch.setattr(ra, "_antigravity_write_targets", lambda: [])
        monkeypatch.setattr(
            ra, "_resolve_command", lambda: ("/bin/codevira", "/usr/bin/python")
        )
        monkeypatch.setattr(
            ra, "_is_junk", lambda p: False
        )  # tmp is under /var/folders
        # projects come from the registrations above; skip filesystem scan
        monkeypatch.setattr(ra, "_scan_for_stores", lambda roots, max_depth=6: set())

        report = ra.register_all(dry_run=False)
        slugs = {p["slug"] for p in report["projects"]}
        assert slugs == {"codevira-lh", "codevira-udap"}

        ccd = json.loads(cc.read_text())
        assert ccd["projects"][lh]["mcpServers"]["codevira-lh"]["args"] == [
            "--project-dir",
            lh,
        ]
        # Claude Code fans out (its config scopes by project); Claude Desktop
        # gets ONE unpinned entry (its config is flat — every entry would load
        # in every conversation) and binds per tool call instead.
        dtd = json.loads(desktop.read_text())
        assert [k for k in dtd["mcpServers"] if "codevira" in k.lower()] == ["codevira"]
        assert "--project-dir" not in dtd["mcpServers"]["codevira"]["args"]


class TestHomeIsNeverATopLevelProject:
    """$HOME must never be discovered as a project.

    ``paths.is_invalid_project_root`` already refuses $HOME for BINDING,
    because codevira's own global home lives at ``~/.codevira`` — so $HOME
    always carries a ``.codevira`` marker and looks like a project to naive
    discovery. ``discover_projects`` did not apply that guard.

    The consequence is not cosmetic. $HOME sorts as top-level, and every real
    project sits underneath it, so all of them are discarded as "nested
    sub-stores". Measured on the maintainer's machine: `register-all --dry-run`
    reported ONE project (`/Users/sachin`) and excluded eleven real ones, and
    the plan was "-2 old / +1 named" — i.e. running it would de-register the
    entire machine. Removing the rogue $HOME entry took discovery from 1 to 12.

    Two layers disagreeing about "is this a valid project root" — the same
    class of split as _is_git_repo vs _discover_project_root.
    """

    def _home_with_project(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".codevira").mkdir(parents=True)  # codevira's own global home
        proj = home / "Projects" / "realapp"
        (proj / ".codevira").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
        # pytest's tmp_path lives under /private/var/folders/, which is in
        # _JUNK_FRAGMENTS. Junk-filtering is not what these tests are about,
        # and leaving it on made the first one pass VACUOUSLY on an empty
        # Discovery — a tautology, not a guard.
        from mcp_server import register_all as _ra

        monkeypatch.setattr(_ra, "_is_junk", lambda _p: False)
        return home, proj

    def test_home_is_not_returned_as_a_project(self, tmp_path, monkeypatch):
        from mcp_server import register_all

        home, proj = self._home_with_project(tmp_path, monkeypatch)
        monkeypatch.setattr(register_all, "_registered_paths", lambda *_a, **_k: set())
        monkeypatch.setattr(
            register_all, "_antigravity_registered_paths", lambda *_a, **_k: set()
        )
        monkeypatch.setattr(
            register_all,
            "_scan_for_stores",
            lambda roots: {str(home), str(proj)},
        )
        d = register_all.discover_projects()
        assert str(home) not in d.projects, (
            f"$HOME was discovered as a project: {d.projects}"
        )

    def test_the_real_project_under_home_survives(self, tmp_path, monkeypatch):
        """The failure that matters: $HOME winning does not merely add a bad
        entry, it DELETES every genuine project by making them look nested."""
        from mcp_server import register_all

        home, proj = self._home_with_project(tmp_path, monkeypatch)
        monkeypatch.setattr(register_all, "_registered_paths", lambda *_a, **_k: set())
        monkeypatch.setattr(
            register_all, "_antigravity_registered_paths", lambda *_a, **_k: set()
        )
        monkeypatch.setattr(
            register_all,
            "_scan_for_stores",
            lambda roots: {str(home), str(proj)},
        )
        d = register_all.discover_projects()
        assert str(proj) in d.projects, (
            f"the real project was swallowed as nested under $HOME: "
            f"projects={d.projects} nested={d.nested_excluded}"
        )

    def test_a_genuinely_nested_substore_is_still_excluded(self, tmp_path, monkeypatch):
        """The guard must not blunt the real nesting rule it sits next to."""
        from mcp_server import register_all

        home, proj = self._home_with_project(tmp_path, monkeypatch)
        sub = proj / "packages" / "inner"
        (sub / ".codevira").mkdir(parents=True)
        monkeypatch.setattr(register_all, "_registered_paths", lambda *_a, **_k: set())
        monkeypatch.setattr(
            register_all, "_antigravity_registered_paths", lambda *_a, **_k: set()
        )
        monkeypatch.setattr(
            register_all,
            "_scan_for_stores",
            lambda roots: {str(home), str(proj), str(sub)},
        )
        d = register_all.discover_projects()
        assert str(proj) in d.projects
        assert str(sub) in d.nested_excluded, "a true sub-store must stay excluded"
