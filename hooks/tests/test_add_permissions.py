"""Tests for skills/c3po/scripts/add-permissions.py."""

import json
import os
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT_REL = os.path.join("skills", "c3po", "scripts", "add-permissions.py")


@pytest.fixture
def plugin_root(tmp_path):
    """A copy of the plugin layout the script needs, so runs never touch the repo."""
    root = tmp_path / "plugin"
    (root / ".claude-plugin").mkdir(parents=True)
    shutil.copy(os.path.join(REPO_ROOT, ".claude-plugin", "tools.json"), root / ".claude-plugin")
    (root / "skills" / "c3po" / "scripts").mkdir(parents=True)
    shutil.copy(os.path.join(REPO_ROOT, SCRIPT_REL), root / SCRIPT_REL)
    (root / "scripts").mkdir()
    (root / "scripts" / "c3po-watch").write_text("")
    return root


@pytest.fixture
def project(tmp_path):
    path = tmp_path / "project"
    path.mkdir()
    return path


def run_script(plugin_root, cwd, *args, env_extra=None):
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(plugin_root / SCRIPT_REL), *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, timeout=10,
    )


def expected_rules(plugin_root):
    with open(plugin_root / ".claude-plugin" / "tools.json") as f:
        tools = json.load(f)["tools"]
    watcher = os.path.join(str(plugin_root), "scripts", "c3po-watch")
    return [f"mcp__c3po__{t['name']}" for t in tools] + [f"Bash(python3 {watcher}:*)"]


def read_allow(project):
    with open(project / ".claude" / "settings.local.json") as f:
        return json.load(f)["permissions"]["allow"]


class TestAddPermissions:
    def test_refuses_skill_dir_as_project(self, plugin_root):
        """The old SKILL.md wording led to `cd <skill_dir> && python3 scripts/add-permissions.py`."""
        skill_dir = plugin_root / "skills" / "c3po"
        result = run_script(plugin_root, skill_dir)
        assert result.returncode != 0
        assert "inside the c3po plugin" in result.stderr
        assert not (skill_dir / ".claude").exists()

    def test_explicit_project_dir(self, plugin_root, project):
        result = run_script(plugin_root, plugin_root / "skills" / "c3po", str(project))
        assert result.returncode == 0, result.stderr
        assert read_allow(project) == expected_rules(plugin_root)

    def test_cwd_default(self, plugin_root, project):
        result = run_script(plugin_root, project)
        assert result.returncode == 0, result.stderr
        assert read_allow(project) == expected_rules(plugin_root)

    def test_claude_project_dir_env(self, plugin_root, project):
        result = run_script(
            plugin_root, plugin_root / "skills" / "c3po",
            env_extra={"CLAUDE_PROJECT_DIR": str(project)},
        )
        assert result.returncode == 0, result.stderr
        assert read_allow(project) == expected_rules(plugin_root)

    def test_idempotent(self, plugin_root, project):
        run_script(plugin_root, project)
        settings = project / ".claude" / "settings.local.json"
        before = settings.read_text()
        result = run_script(plugin_root, project)
        assert result.returncode == 0
        assert "already in" in result.stdout
        assert settings.read_text() == before

    def test_preserves_existing_rules(self, plugin_root, project):
        (project / ".claude").mkdir()
        (project / ".claude" / "settings.local.json").write_text(
            json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}, "other": 1})
        )
        result = run_script(plugin_root, project)
        assert result.returncode == 0, result.stderr
        allow = read_allow(project)
        assert allow[0] == "Bash(ls:*)"
        assert allow[1:] == expected_rules(plugin_root)
        with open(project / ".claude" / "settings.local.json") as f:
            assert json.load(f)["other"] == 1

    def test_removes_stray_skill_settings(self, plugin_root, project):
        stray_dir = plugin_root / "skills" / "c3po" / ".claude"
        stray_dir.mkdir()
        (stray_dir / "settings.local.json").write_text("{}")
        result = run_script(plugin_root, project)
        assert result.returncode == 0, result.stderr
        assert "Removed stray" in result.stdout
        assert not stray_dir.exists()
