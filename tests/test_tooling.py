"""Tool registration: profiles, annotations, errors, and not blocking the loop."""

import asyncio
import importlib

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

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
        assert "user_prompt" not in tool.inputSchema.get("properties", {}), tool.name


def test_every_tool_is_annotated(monkeypatch):
    tools = _server_tools(monkeypatch, "full")
    for tool in tools.values():
        hints = tool.annotations
        assert hints is not None, tool.name
        assert hints.openWorldHint is False
        if hints.readOnlyHint:
            assert hints.destructiveHint is False
    assert tools["lom_get_object"].annotations.readOnlyHint is True
    assert tools["get_session_overview"].annotations.readOnlyHint is True
    assert tools["manage_track"].annotations.destructiveHint is True
    mixer = tools["set_track_mixer"].annotations
    assert (mixer.readOnlyHint, mixer.destructiveHint, mixer.idempotentHint) == (
        False, False, True)


def test_results_are_not_wrapped_in_an_output_schema(monkeypatch):
    tools = _server_tools(monkeypatch, "core")
    assert all(t.outputSchema is None for t in tools.values())


def test_sync_tools_run_off_the_event_loop():
    mcp = FastMCP("t")
    loop_threads = []

    def slow(x: int) -> str:
        import threading
        loop_threads.append(threading.current_thread().name)
        return str(x)

    tooling.register_tool(mcp, slow, read_only=True)
    tool = mcp._tool_manager.get_tool("slow")
    assert tool.is_async

    async def main():
        import threading
        result = await mcp.call_tool("slow", {"x": 3})
        return threading.current_thread().name, result

    loop_thread, _ = asyncio.run(main())
    assert loop_threads and loop_threads[0] != loop_thread


def test_legacy_failure_text_becomes_a_tool_error():
    mcp = FastMCP("t")

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
            "release_check", "explain_my_set"} <= set(prompts)
    rendered = asyncio.run(server.mcp.get_prompt("release_check", {"platform": "apple_music"}))
    text = rendered.messages[0].content.text
    assert "-16 LUFS" in text and "render_and_analyze" in text
