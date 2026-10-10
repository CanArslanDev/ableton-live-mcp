"""Interactive panels (MCP Apps) that clients supporting the
`io.modelcontextprotocol/ui` extension show next to a tool's result.

Each panel is one self-contained HTML document served as a `ui://` resource;
tools point at theirs with `register_tool(..., ui=...)`. A panel only renders
what the tool returned (and may call further tools through the host), so a
client without MCP Apps still gets the complete answer as text.
"""

from __future__ import annotations

from mcp.server.apps import Apps

# ui:// resources, filled in by .panels
PANELS: dict[str, tuple[str, str, str]] = {}  # uri: (title, description, html)


def ui_extension() -> Apps:
    from . import panels  # noqa: F401  (registers the panels)

    apps = Apps()
    for uri, (title, description, html) in PANELS.items():
        apps.add_html_resource(uri, html, name=uri.rsplit("/", 1)[-1], title=title,
                               description=description, prefers_border=True)
    return apps
