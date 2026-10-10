"""Tool registration: profiles, annotations, errors, and not blocking the loop."""

import asyncio
import importlib

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from MCP_Server import tooling


def _server_tools(monkeypatch, profile):
    monkeypatch.setenv("ABLETON_MCP_PROFILE", profile)
    import MCP_Server.server as server
    importlib.reload(server)
    try:
        return {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    finally:
        monkeypatch.delenv("ABLETON_MCP_PROFILE")
        importlib.reload(server)


def test_core_profile_drops_duplicated_original_tools(monkeypatch):
    tools = _server_tools(monkeypatch, "core")
    for duplicate in ("set_tempo", "fire_clip", "get_clip_notes", "create_midi_track",
                      "start_playback", "set_device_parameter", "get_session_snapshot"):
        assert duplicate not in tools
    # Originals with no newer equivalent stay
    for kept in ("load_instrument_or_effect", "get_browser_tree", "create_audio_clip",
                 "duplicate_to_arrangement", "create_locator", "get_remote_script_info"):
        assert kept in tools
    assert "get_session_overview" in tools


def test_full_profile_keeps_every_original_tool(monkeypatch):
    tools = _server_tools(monkeypatch, "full")
    for name in ("set_tempo", "fire_clip", "get_clip_notes", "create_midi_track",
                 "get_session_overview"):
        assert name in tools


def test_dataset_tools_need_recording_to_be_possible(monkeypatch):
    # No MCP_Server/config.py in the repository, so telemetry is off
    tools = _server_tools(monkeypatch, "full")
    for name in ("set_dataset_consent", "submit_intent", "rate_last_action",
                 "record_audition"):
        assert name not in tools


def test_no_tool_asks_for_the_user_prompt(monkeypatch):
    tools = _server_tools(monkeypatch, "full")
    for tool in tools.values():
        assert "user_prompt" not in tool.input_schema.get("properties", {}), tool.name


def test_every_tool_is_annotated(monkeypatch):
    tools = _server_tools(monkeypatch, "full")
    for tool in tools.values():
        hints = tool.annotations
        assert hints is not None, tool.name
        assert hints.open_world_hint is False
        if hints.read_only_hint:
            assert hints.destructive_hint is False
    assert tools["lom_get_object"].annotations.read_only_hint is True
    assert tools["get_session_overview"].annotations.read_only_hint is True
    assert tools["manage_track"].annotations.destructive_hint is True
    mixer = tools["set_track_mixer"].annotations
    assert (mixer.read_only_hint, mixer.destructive_hint, mixer.idempotent_hint) == (
        False, False, True)


def test_results_are_not_wrapped_in_an_output_schema(monkeypatch):
    tools = _server_tools(monkeypatch, "core")
    assert all(t.output_schema is None for t in tools.values())


def test_sync_tools_run_off_the_event_loop():
    mcp = MCPServer("t")
    loop_threads = []

    def slow(x: int) -> str:
        import threading
        loop_threads.append(threading.current_thread().name)
        return str(x)

    tooling.register_tool(mcp, slow, read_only=True)

    async def main():
        import threading
        result = await mcp.call_tool("slow", {"x": 3})
        return threading.current_thread().name, result

    loop_thread, _ = asyncio.run(main())
    assert loop_threads and loop_threads[0] != loop_thread


def test_legacy_failure_text_becomes_a_tool_error():
    mcp = MCPServer("t")

    def legacy() -> str:
        return "Error getting clip notes: boom"

    tooling.register_tool(mcp, legacy, legacy=True)
    with pytest.raises(ToolError, match="Error getting clip notes: boom"):
        asyncio.run(mcp.call_tool("legacy", {}))


def test_unknown_profile_falls_back_to_core(monkeypatch):
    monkeypatch.setenv("ABLETON_MCP_PROFILE", "everything")
    assert tooling.active_profile() == "core"
    assert tooling.in_profile("core") and not tooling.in_profile("full")


def test_workflow_prompts_are_listed_and_render(monkeypatch):
    import MCP_Server.server as server
    prompts = {p.name: p for p in asyncio.run(server.mcp.list_prompts())}
    assert {"mix_check", "beat_from_scratch", "follow_the_harmony", "arrange_from_loop",
            "release_check", "explain_my_set", "match_reference"} <= set(prompts)
    rendered = asyncio.run(server.mcp.get_prompt("release_check", {"platform": "apple_music"}))
    text = rendered.messages[0].content.text
    assert "-16 LUFS" in text and "render_and_analyze" in text


def test_any_failure_reaches_the_model_with_its_message():
    """mcp 2.x shows only "Error executing tool <name>" for an exception that is
    not a ToolError; the message is what tells the model how to fix the call."""
    mcp = MCPServer("t")

    def picky(track: str) -> str:
        raise ValueError(f"No item named {track!r}. Available: Drums, Bass")

    async def coro() -> str:
        raise RuntimeError("Live is busy")

    coro.__name__ = "busy"
    tooling.register_tool(mcp, picky)
    tooling.register_tool(mcp, coro)
    with pytest.raises(ToolError, match="Error executing tool picky: No item named 'Nope'. "
                                        "Available: Drums, Bass"):
        asyncio.run(mcp.call_tool("picky", {"track": "Nope"}))
    with pytest.raises(ToolError, match="busy: Live is busy"):
        asyncio.run(mcp.call_tool("busy", {}))


def test_ui_tools_point_at_their_panel(monkeypatch):
    tools = _server_tools(monkeypatch, "core")
    panels = {t.name: (t.meta or {}).get("ui", {}).get("resourceUri") for t in tools.values()}
    assert panels["get_session_overview"] is None or panels["get_session_overview"].startswith("ui://")
