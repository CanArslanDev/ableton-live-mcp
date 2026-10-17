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
                      "start_playback", "set_device_parameter", "get_session_snapshot",
                      # browser_search/browser_load, build_drum_kit, doctor do these
                      "load_instrument_or_effect", "get_browser_tree", "get_browser_items_at_path",
                      "load_drum_kit", "get_remote_script_info", "get_arrangement_clips"):
        assert duplicate not in tools
    # Originals with no newer equivalent stay
    for kept in ("create_audio_clip", "duplicate_to_arrangement", "create_locator"):
        assert kept in tools
    assert "get_session_overview" in tools


def test_manage_track_lists_its_actions_in_the_schema(monkeypatch):
    # create_midi_track is not in the core profile; the schema shows what replaces it
    tools = _server_tools(monkeypatch, "core")
    action = tools["manage_track"].input_schema["properties"]["action"]
    assert {"create_midi", "create_audio", "create_return", "delete"} <= set(action["enum"])
    assert "lom_call_method" in tools


def test_full_profile_keeps_every_original_tool(monkeypatch):
    tools = _server_tools(monkeypatch, "full")
    for name in ("set_tempo", "fire_clip", "get_clip_notes", "create_midi_track",
                 "get_session_overview"):
        assert name in tools


def test_no_dataset_tools(monkeypatch):
    # the upstream telemetry/dataset recorder was removed (2.2.0)
    tools = _server_tools(monkeypatch, "full")
    for name in ("set_dataset_consent", "submit_intent", "rate_last_action", "prefer_candidate",
                 "reject_last_action", "record_audition"):
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


def test_schemas_are_compacted_without_losing_parameters():
    from MCP_Server.tooling import compact_schema

    schema = {"title": "fArguments", "type": "object", "required": ["message"], "properties": {
        "message": {"title": "Message", "type": "string"},
        "title": {"title": "Title", "type": "string", "default": "AbletonMCP"},
        "track": {"title": "Track", "anyOf": [{"type": "integer"}, {"type": "string"}]},
        "clip": {"title": "Clip", "anyOf": [{"type": "integer"}, {"type": "null"}], "default": None},
        "notes": {"anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "null"}]},
    }}
    assert compact_schema(schema) == {"type": "object", "required": ["message"], "properties": {
        "message": {"type": "string"},
        "title": {"type": "string", "default": "AbletonMCP"},
        "track": {"type": ["integer", "string"]},
        "clip": {"type": ["integer", "null"]},
        "notes": {"anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "null"}]},
    }}


def test_note_rows_keep_every_field_and_drop_defaults():
    from MCP_Server.extended_tools import note_table

    table = note_table([
        {"note_id": 3, "pitch": 60, "start_time": 0.0, "duration": 0.25, "velocity": 100.0,
         "mute": False, "probability": 1.0, "velocity_deviation": 0.0, "release_velocity": 64.0},
        {"note_id": 4, "pitch": 64, "start_time": 0.123456789, "duration": 0.5, "velocity": 90.5,
         "mute": True, "probability": 0.5, "velocity_deviation": 0.0, "release_velocity": 64.0},
    ])
    assert table == {"note_columns": ["note_id", "pitch", "name", "start_time", "duration", "velocity"],
                     "notes": [[3, 60, "C3", 0, 0.25, 100], [4, 64, "E3", 0.1235, 0.5, 90.5]],
                     "note_extras": {"1": {"mute": True, "probability": 0.5}}}
    assert note_table([])["notes"] == []


def test_undo_steps_follow_what_live_records():
    """Measured on Live 12.4 (one probe per row): parameter changes and other
    changes are separate steps; same values, transport and selection none."""
    from MCP_Server.journal import undo_steps

    def put(prop, old=0, new=1, path="live_set tracks 0"):
        return ("lom_set", {"path": path, "property": prop, "value": new},
                {"property": prop, "old_value": old, "value": new})

    def batch(*steps):
        return ("batch", {"commands": [{"type": c, "params": p} for c, p, _ in steps]},
                {"results": [{"status": "success", "result": r} for _, _, r in steps]})

    volume, pan = put("value", 0.85, 0.5), put("value", 0.0, 0.3)
    rename, color = put("name", "A", "B"), put("color_index", 1, 5)
    mute, solo = put("mute", False, True), put("solo", False, True)
    cases = {
        "tempo": (put("tempo", 120.0, 121.0), 1),
        "same tempo again": (put("tempo", 121.0, 121.0), 0),
        "solo": (solo, 0), "arm": (put("arm", False, True), 0),
        "loop": (put("loop", False, True), 0), "metronome": (put("metronome", False, True), 0),
        "playhead": (put("current_song_time", 0.0, 8.0), 0),
        "selection": (put("selected_track", {"$ref": "id 1"}, {"$ref": "id 2"}), 0),
        "start_playing": (("lom_call", {"path": "live_set", "method": "start_playing"}, {}), 0),
        "fire": (("lom_call", {"path": "live_set tracks 0 clip_slots 0", "method": "fire"}, {}), 0),
        "create_clip": (("lom_call", {"path": "live_set tracks 0 clip_slots 0",
                                      "method": "create_clip", "args": [4]}, {}), 1),
        "volume + pan": (batch(volume, pan), 1),
        "rename + color": (batch(rename, color), 1),
        "volume + rename": (batch(volume, rename), 2),
        "mute + rename": (batch(mute, rename), 2),
        "mute + solo + volume": (batch(mute, solo, volume), 1),
        "same values": (batch(put("name", "A", "A"), put("tempo", 121.0, 121.0)), 0),
        "display unchanged": (("param_set_display", {"path": "id 3", "value": "-6 dB"},
                               {"old": "-6.0 dB", "new": "-6.0 dB"}), 0),
        "display changed": (("param_set_display", {"path": "id 3", "value": "-8 dB"},
                             {"old": "-6.0 dB", "new": "-8.0 dB"}), 1),
        "fire_clip": (("fire_clip", {"track_index": 0, "clip_index": 0}, {}), 0),
        "read": (("lom_get", {"path": "live_set", "properties": ["tempo"]}, {}), 0),
    }
    for label, ((command, params, result), steps) in cases.items():
        assert undo_steps(command, params, result) == steps, label
    # A batch step that failed, or never ran after one that did, changed nothing
    command, params, result = batch(volume, rename)
    result["results"][1] = {"status": "error", "message": "no"}
    assert undo_steps(command, params, result) == 1
    result["results"].pop()
    assert undo_steps(command, params, result) == 1

