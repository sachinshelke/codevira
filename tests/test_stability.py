import yaml

import mcp_server.paths as paths
from indexer import index_codebase
from mcp_server.tools import changesets, graph, roadmap, search


def _set_project_root(monkeypatch, root):
    monkeypatch.setattr(paths, "_project_dir_override", None)
    monkeypatch.chdir(root.resolve())


def test_get_roadmap_migrates_legacy_current_phase(tmp_path, monkeypatch):
    project_root = tmp_path / "legacy-project"
    data_dir = project_root / ".codevira"
    data_dir.mkdir(parents=True)
    _set_project_root(monkeypatch, project_root)

    legacy_roadmap = {
        "current_phase": 1,
        "next_action": "Finish bootstrapping",
        "open_changesets": ["cs-1"],
        "phases": [
            {
                "number": 1,
                "name": "Bootstrap",
                "description": "Initialize the project",
                "status": "in_progress",
            },
            {
                "number": 2,
                "name": "Next Phase",
                "description": "Follow-up work",
                "status": "pending",
            },
        ],
        "deferred": [],
    }
    with open(data_dir / "roadmap.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(legacy_roadmap, f, sort_keys=False)

    compact = roadmap.get_roadmap()
    full = roadmap.get_full_roadmap()

    assert compact["current_phase"]["number"] == 1
    assert compact["current_phase"]["name"] == "Bootstrap"
    assert compact["current_phase"]["open_changesets"] == ["cs-1"]
    assert compact["upcoming"][0]["phase"] == 2
    assert full["current_phase"]["number"] == 1
    assert full["upcoming_phases"][0]["number"] == 2

    with open(data_dir / "roadmap.yaml", encoding="utf-8") as f:
        migrated = yaml.safe_load(f)

    assert isinstance(migrated["current_phase"], dict)
    assert migrated["current_phase"]["number"] == 1
    assert migrated["upcoming_phases"][0]["phase"] == 2


def test_get_project_root_discovers_codevira_from_subdirectory(tmp_path, monkeypatch):
    project_root = tmp_path / "subdir-project"
    nested_dir = project_root / "src" / "feature"
    (project_root / ".codevira").mkdir(parents=True)
    nested_dir.mkdir(parents=True)
    _set_project_root(monkeypatch, nested_dir)

    assert paths.get_project_root() == project_root.resolve()
    assert paths.get_data_dir() == (project_root / ".codevira").resolve()


def test_update_node_after_change_updates_sqlite_graph(tmp_path, monkeypatch):
    project_root = tmp_path / "graph-project"
    (project_root / ".codevira").mkdir(parents=True)
    _set_project_root(monkeypatch, project_root)

    graph.add_node(
        file_path="src/example.py",
        role="Example module",
        layer="service",
        rules=["Keep responses stable"],
    )

    result = changesets.update_node_after_change(
        "src/example.py",
        {
            "new_rules": ["Preserve legacy payload shape"],
            "new_connections": [{"target": "src/other.py", "edge": "uses", "via": "import"}],
            "do_not_revert": True,
            "key_functions": ["run"],
        },
    )
    node = graph.get_node("src/example.py")["node"]

    assert result["success"] is True
    assert "Preserve legacy payload shape" in node["rules"]
    assert any(dep["target"] == "src/other.py" for dep in node["dependencies"])
    assert "run" in node["key_functions"]
    assert bool(node["do_not_revert"]) is True


def test_refresh_index_passes_explicit_file_paths(monkeypatch):
    calls = []

    def fake_cmd_incremental(*, quiet, file_paths=None):
        calls.append({"quiet": quiet, "file_paths": file_paths})
        return 0

    monkeypatch.setattr(index_codebase, "cmd_incremental", fake_cmd_incremental)

    result = search.refresh_index(["src/app.py"])

    assert calls == [{"quiet": True, "file_paths": ["src/app.py"]}]
    assert result["mode"] == "targeted"
    assert result["file_paths"] == ["src/app.py"]
