"""Sync test: verifies .claude-plugin/tools.json matches the @mcp.tool() definitions in server.py."""

import ast
import json
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
TOOLS_JSON_PATH = os.path.join(REPO_ROOT, ".claude-plugin", "tools.json")
SERVER_PY_PATH = os.path.join(REPO_ROOT, "coordinator", "server.py")


def _load_tools_json() -> list[dict]:
    with open(TOOLS_JSON_PATH) as f:
        return json.load(f)["tools"]


def _extract_mcp_tool_names() -> list[str]:
    """Parse server.py and return function names decorated with @mcp.tool()."""
    with open(SERVER_PY_PATH) as f:
        source = f.read()
    tree = ast.parse(source)
    tool_names = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                # Match @mcp.tool() calls
                if (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "tool"
                ):
                    tool_names.append(node.name)
    return tool_names


def _extract_function_param_names(func_name: str) -> list[str]:
    """Return parameter names for a function in server.py."""
    with open(SERVER_PY_PATH) as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            args = node.args
            all_args = [a.arg for a in args.args + args.posonlyargs + args.kwonlyargs]
            return all_args
    return []


class TestToolsSync:
    def test_tools_json_exists(self):
        assert os.path.exists(TOOLS_JSON_PATH), f"Missing {TOOLS_JSON_PATH}"

    def test_server_py_exists(self):
        assert os.path.exists(SERVER_PY_PATH), f"Missing {SERVER_PY_PATH}"

    def test_all_server_tools_in_tools_json(self):
        """Every @mcp.tool() in server.py must have an entry in tools.json."""
        server_tools = set(_extract_mcp_tool_names())
        json_tool_names = {t["name"] for t in _load_tools_json()}
        missing = server_tools - json_tool_names
        assert not missing, (
            f"Tools in server.py but missing from tools.json: {sorted(missing)}\n"
            "Add them to .claude-plugin/tools.json with the correct needs_agent_id flag."
        )

    def test_all_tools_json_in_server(self):
        """Every entry in tools.json must correspond to a real @mcp.tool() in server.py."""
        server_tools = set(_extract_mcp_tool_names())
        json_tool_names = {t["name"] for t in _load_tools_json()}
        extra = json_tool_names - server_tools
        assert not extra, (
            f"Tools in tools.json but not found as @mcp.tool() in server.py: {sorted(extra)}\n"
            "Remove them from .claude-plugin/tools.json or add @mcp.tool() to server.py."
        )

    def test_needs_agent_id_tools_have_agent_id_param(self):
        """Every tool with needs_agent_id=true must have an agent_id parameter in server.py."""
        tools = _load_tools_json()
        failures = []
        for tool in tools:
            if tool["needs_agent_id"]:
                params = _extract_function_param_names(tool["name"])
                if "agent_id" not in params:
                    failures.append(tool["name"])
        assert not failures, (
            f"Tools marked needs_agent_id=true but missing agent_id param in server.py: {failures}"
        )

    def test_tools_json_no_duplicates(self):
        """tools.json should not have duplicate tool names."""
        tools = _load_tools_json()
        names = [t["name"] for t in tools]
        duplicates = [n for n in names if names.count(n) > 1]
        assert not duplicates, f"Duplicate tool names in tools.json: {set(duplicates)}"
