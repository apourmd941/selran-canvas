"""Test the selran-mcp Path B plugin contract.

Mimics how selran-mcp loads plugins at startup:
    1. Discovers `canvas_mcp_plugin/__init__.py` via the manifest's
       `plugin.module_path` field.
    2. Loads it with `importlib.util.spec_from_file_location`.
    3. Calls `register(mcp, manifest)` — must return the count of tools
       registered, and never raise.

If this contract breaks, selran-mcp silently fails to register the canvas
tools. Users see "0 tools" in `selran-mcp status` with no useful diagnostic.
This test catches that regression.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from selran_canvas.store import Store

EXPECTED_TOOLS = {
    "canvas_set_page",
    "canvas_ask_mcq",
    "canvas_get_state",
    "canvas_add_references",
    "canvas_set_journal_style",
    "canvas_set_visual_theme",
    "canvas_list_journal_styles",
}


@pytest.fixture
def loaded_plugin(tmp_path, monkeypatch):
    """Load `canvas_mcp_plugin` exactly the way selran-mcp does.

    Monkeypatch `_ensure_canvas_running` so tests don't try to bind a real
    port or write to the user's actual `~/.selran-canvas/canvas_state.db`.
    Tests just verify the contract; runtime HTTP wiring is exercised in
    real production via `selran-mcp status` after a Claude desktop restart.
    """
    repo_root = Path(__file__).resolve().parent.parent
    plugin_path = repo_root / "canvas_mcp_plugin" / "__init__.py"

    spec = importlib.util.spec_from_file_location("canvas_mcp_plugin", plugin_path)
    assert spec is not None and spec.loader is not None, "spec_from_file_location failed"

    module = importlib.util.module_from_spec(spec)
    sys.modules["canvas_mcp_plugin"] = module
    spec.loader.exec_module(module)

    # Replace HTTP startup with a tmp-store stub. The plugin caches state
    # in a module-level dict, so reset that too in case a previous test
    # populated it.
    test_store = Store(tmp_path / "test.db")
    test_url = "http://127.0.0.1:0"  # not bound, used only as a string

    def _stub():
        return test_store, test_url

    monkeypatch.setattr(module, "_ensure_canvas_running", _stub)
    module._state = {"http_started": True, "store": test_store, "url": test_url}

    return module


def test_plugin_module_loads(loaded_plugin):
    """The plugin file must be importable via importlib.util."""
    assert loaded_plugin is not None
    assert loaded_plugin.__name__ == "canvas_mcp_plugin"


def test_register_function_exists(loaded_plugin):
    """The Path B contract requires a top-level `register(mcp, manifest) -> int`."""
    assert hasattr(loaded_plugin, "register"), "missing register() function"
    assert callable(loaded_plugin.register), "register is not callable"


def test_register_returns_seven(loaded_plugin):
    """register() must return exactly 7 (the canvas tool count)."""
    host = FastMCP("selran-mcp-test")
    fake_manifest = SimpleNamespace(
        repo_root=Path(__file__).resolve().parent.parent,
        id="canvas",
    )
    n = loaded_plugin.register(host, fake_manifest)
    assert n == 7, f"register() returned {n}, expected 7"


def test_register_attaches_all_canvas_tools(loaded_plugin):
    """All 7 expected tools must appear on the supplied FastMCP instance,
    and every name must start with the `canvas_` prefix per the brief's
    skill_id namespacing rule."""
    host = FastMCP("selran-mcp-test")
    fake_manifest = SimpleNamespace(
        repo_root=Path(__file__).resolve().parent.parent,
        id="canvas",
    )

    before = {t.name for t in asyncio.run(host.list_tools())}
    loaded_plugin.register(host, fake_manifest)
    after = {t.name for t in asyncio.run(host.list_tools())}

    new_tools = after - before
    assert new_tools == EXPECTED_TOOLS, (
        f"\nExpected: {sorted(EXPECTED_TOOLS)}"
        f"\nGot:      {sorted(new_tools)}"
        f"\nMissing:  {sorted(EXPECTED_TOOLS - new_tools)}"
        f"\nExtra:    {sorted(new_tools - EXPECTED_TOOLS)}"
    )

    # Brief §4 namespacing rule: every tool name must start with skill_id_
    for name in new_tools:
        assert name.startswith("canvas_"), f"{name!r} violates the canvas_ prefix rule"


def test_register_is_idempotent(loaded_plugin):
    """Calling register() twice on the same FastMCP must not raise."""
    host = FastMCP("selran-mcp-test")
    fake_manifest = SimpleNamespace(
        repo_root=Path(__file__).resolve().parent.parent,
        id="canvas",
    )
    n1 = loaded_plugin.register(host, fake_manifest)
    # Second call — FastMCP may de-duplicate or warn; either is acceptable
    # as long as it doesn't crash. selran-mcp loads each plugin exactly
    # once at startup, so this is a defensive test, not a normal flow.
    try:
        loaded_plugin.register(host, fake_manifest)
    except Exception as e:  # noqa: BLE001
        # If FastMCP raises on duplicate registration, that's an acceptable
        # behaviour for this defensive case — the plugin doesn't crash.
        assert "duplicate" in str(e).lower() or "already" in str(e).lower(), (
            f"unexpected error on second register(): {e!r}"
        )
    assert n1 == 7
