"""How tools are registered with FastMCP.

Every tool, original or extended, goes through `register_tool`, which:

- runs synchronous tool bodies on a worker thread. FastMCP 1.x calls a sync
  tool inline on the event loop, so one slow Live round-trip (a 120 s batch,
  a browser walk) used to stall everything else the server does, including
  cancellation;
- reports failures as MCP tool errors (``isError``) instead of a successful
  result that happens to start with "Error";
- attaches MCP tool annotations, which clients use to decide when to ask the
  user before running a tool;
- leaves out tools that are not part of the active profile.

Profiles (``ABLETON_MCP_PROFILE``):
- ``core`` (default): one tool per job. The original tools that newer ones
  cover (set_tempo, fire_clip, get_clip_notes, …) are left out.
- ``full``: every tool, for clients and prompts written against the old set.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import os
from typing import Any, Callable

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

logger = logging.getLogger("AbletonMCPServer")

PROFILES = ("core", "full")

# The original tools report failure by returning text like this instead of
# raising.
_FAILURE_PREFIXES = ("Error", "Failed", "Ableton Remote Script missing capability")


def active_profile() -> str:
    value = os.environ.get("ABLETON_MCP_PROFILE", "core").strip().lower()
    return value if value in PROFILES else "core"


def in_profile(tier: str) -> bool:
    """tier "core" tools are always registered; "full" ones only in the full profile."""
    return tier == "core" or active_profile() == "full"


def off_event_loop(fn: Callable) -> Callable:
    """Async version of a sync tool that runs its body on a worker thread."""
    if inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def runner(*args: Any, **kwargs: Any) -> Any:
        return await asyncio.to_thread(fn, *args, **kwargs)

    return runner


def logged_errors(fn: Callable) -> Callable:
    """Log a failing tool, then let the exception through.

    FastMCP turns an exception into a tool result with isError=true and the
    text "Error executing tool <name>: <message>".
    """
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:
            logger.error("%s failed: %s", fn.__name__, e)
            raise

    return wrapper


def failure_text_as_error(fn: Callable) -> Callable:
    """For the original tools, which return their errors as text."""
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = await fn(*args, **kwargs)
        if isinstance(result, str) and result.startswith(_FAILURE_PREFIXES):
            raise ToolError(result)
        return result

    return wrapper


def annotations(read_only: bool = False, destructive: bool = False,
                idempotent: bool = False, title: str | None = None) -> ToolAnnotations:
    # Every tool acts on the local Live set only, never on an open world.
    return ToolAnnotations(title=title, readOnlyHint=read_only,
                           destructiveHint=False if read_only else destructive,
                           idempotentHint=True if read_only else idempotent,
                           openWorldHint=False)


def register_tool(mcp, fn: Callable, *, read_only: bool = False, destructive: bool = False,
                  idempotent: bool = False, tier: str = "core", legacy: bool = False,
                  title: str | None = None) -> Callable:
    """Register `fn` as an MCP tool (if its tier is in the active profile).

    Returns `fn` unchanged so module-level names keep pointing at the plain
    function (tests call them directly).
    """
    if not in_profile(tier):
        return fn
    wrapped = logged_errors(off_event_loop(fn))
    if legacy:
        wrapped = failure_text_as_error(wrapped)
    mcp.tool(name=fn.__name__, title=title,
             annotations=annotations(read_only, destructive, idempotent, title),
             structured_output=False)(wrapped)
    return fn
