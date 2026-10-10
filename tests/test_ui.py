"""MCP Apps panels: served right, wired to tools, self-contained, valid JS."""

import asyncio
import re
import shutil
import subprocess

import pytest

from MCP_Server.ui import PANELS, ui_extension
from MCP_Server.ui.base import PROTOCOL_VERSION


def _server():
    import MCP_Server.server as server
    return server.mcp


def test_panels_are_served_as_mcp_apps():
    mcp = _server()
    resources = {str(r.uri): r for r in asyncio.run(mcp.list_resources())}
    for uri in PANELS:
        assert uri.startswith("ui://")
        assert resources[uri].mime_type == "text/html;profile=mcp-app"
    caps = mcp._lowlevel_server.get_capabilities()
    assert "io.modelcontextprotocol/ui" in (caps.extensions or {})


def test_every_ui_tool_points_at_a_served_panel():
    mcp = _server()
    tools = asyncio.run(mcp.list_tools())
    wired = {t.name: t.meta["ui"]["resourceUri"] for t in tools if (t.meta or {}).get("ui")}
    assert {"get_session_overview", "mix_doctor", "compare_to_reference", "snapshot",
            "measure_levels", "build_drum_kit", "turkish_makam", "generate_chord_progression",
            "read_clip_notes", "render_and_analyze"} <= set(wired)
    assert set(wired.values()) <= set(PANELS)


def test_panels_are_self_contained_and_never_use_inner_html():
    ui_extension()
    for uri, (_title, _description, html) in PANELS.items():
        assert not re.search(r"""(src|href)=["']?https?://""", html), uri
        assert "innerHTML" not in html and "insertAdjacentHTML" not in html, uri
        assert f'protocolVersion: "{PROTOCOL_VERSION}"' in html, uri
        assert "ui/notifications/initialized" in html and "ui/notifications/size-changed" in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_panel_scripts_parse(tmp_path):
    ui_extension()
    for uri, (_title, _description, html) in PANELS.items():
        script = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
        target = tmp_path / (uri.rsplit("/", 1)[-1] + ".js")
        target.write_text(script, encoding="utf-8")
        check = subprocess.run(["node", "--check", str(target)], capture_output=True, text=True)
        assert check.returncode == 0, f"{uri}: {check.stderr}"
