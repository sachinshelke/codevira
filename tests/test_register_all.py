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
            per_project_scope=True,
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

    def test_top_level_scope_for_non_project_aware(self, tmp_path):
        cfg = tmp_path / "desktop.json"
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
            "claude_desktop",
            per_project_scope=False,
            dry_run=False,
        )
        d = json.loads(cfg.read_text())
        assert set(k for k in d["mcpServers"] if "codevira" in k) == {
            "codevira-lh",
            "codevira-udap",
        }

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
            per_project_scope=True,
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
        dtd = json.loads(desktop.read_text())
        assert {"codevira-lh", "codevira-udap"} <= set(dtd["mcpServers"])
