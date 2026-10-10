"""How tools are registered with the MCP server.

Every tool, original or extended, goes through `register_tool`, which:

- reports every failure as an MCP tool error (``isError``) that carries its
  message. The SDK shows the model only "Error executing tool <name>" for an
  exception that is not a ``ToolError``, and the message ("No item named
  'Bass'. Available: …") is what lets the model fix its next call;
- turns the original tools' "Error …" result strings into tool errors too;
- attaches MCP tool annotations, which clients use to decide when to ask the
  user before running a tool, and the MCP Apps UI a tool renders, if any;
- leaves out tools that are not part of the active profile.

Synchronous tool bodies run on the SDK's worker threads (mcp 2.x), so a slow
Live round-trip never stalls the event loop.

Profiles (``ABLETON_MCP_PROFILE``):
- ``core`` (default): one tool per job. The original tools that newer ones
  cover (set_tempo, fire_clip, get_clip_notes, …) are left out.
- ``full``: every tool, for clients and prompts written against the old set.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import ToolAnnotations

logger = logging.getLogger("AbletonMCPServer")

PROFILES = ("core", "full")

# The original tools report failure by returning text like this instead of
# raising.
_FAILURE_PREFIXES = ("Error", "Failed", "Ableton Remote Script missing capability")

# Failures that are about the request, not a bug: logged without a traceback
_EXPECTED = (ValueError, LookupError, RuntimeError, OSError)


def active_profile() -> str:
    value = os.environ.get("ABLETON_MCP_PROFILE", "core").strip().lower()
    return value if value in PROFILES else "core"


def in_profile(tier: str) -> bool:
    """tier "core" tools are always registered; "full" ones only in the full profile."""
    return tier == "core" or active_profile() == "full"


def _as_tool_error(name: str, error: Exception) -> ToolError:
    if isinstance(error, _EXPECTED) or type(error).__name__ == "AbletonCommandError":
        logger.error("%s failed: %s", name, error)
    else:
        logger.exception("%s failed", name)
    # The SDK prefixes it with "Error executing tool <name>: "
    return ToolError(str(error))


def errors_as_tool_errors(fn: Callable) -> Callable:
    """Re-raise any exception from `fn` as a ToolError with its message."""
    name = fn.__name__
    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def runner(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except (ToolError, MCPError):
                raise
            except Exception as e:
                raise _as_tool_error(name, e) from e

        return runner

    @functools.wraps(fn)
    def caller(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (ToolError, MCPError):
            raise
        except Exception as e:
            raise _as_tool_error(name, e) from e

    return caller


def failure_text_as_error(fn: Callable) -> Callable:
    """For the original tools, which return their errors as text."""
    def check(result: Any) -> Any:
        if isinstance(result, str) and result.startswith(_FAILURE_PREFIXES):
            raise ToolError(result)
        return result

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def runner(*args: Any, **kwargs: Any) -> Any:
            return check(await fn(*args, **kwargs))

        return runner

    @functools.wraps(fn)
    def caller(*args: Any, **kwargs: Any) -> Any:
        return check(fn(*args, **kwargs))

    return caller


def annotations(read_only: bool = False, destructive: bool = False,
                idempotent: bool = False, title: str | None = None) -> ToolAnnotations:
    # Every tool acts on the local Live set only, never on an open world.
    return ToolAnnotations(title=title, readOnlyHint=read_only,
                           destructiveHint=False if read_only else destructive,
                           idempotentHint=True if read_only else idempotent,
                           openWorldHint=False)


def register_tool(mcp, fn: Callable, *, read_only: bool = False, destructive: bool = False,
                  idempotent: bool = False, tier: str = "core", legacy: bool = False,
                  title: str | None = None, ui: str | None = None) -> Callable:
    """Register `fn` as an MCP tool (if its tier is in the active profile).

    ui: the `ui://` resource (MCP Apps) a client that supports it shows with
    the tool's result. The result itself stays complete text, for every other
    client.

    Returns `fn` unchanged so module-level names keep pointing at the plain
    function (tests call them directly).
    """
    if not in_profile(tier):
        return fn
    wrapped = errors_as_tool_errors(fn)
    if legacy:
        wrapped = failure_text_as_error(wrapped)
    mcp.tool(name=fn.__name__, title=title,
             annotations=annotations(read_only, destructive, idempotent, title),
             meta={"ui": {"resourceUri": ui}} if ui else None,
             structured_output=False)(wrapped)
    return fn
