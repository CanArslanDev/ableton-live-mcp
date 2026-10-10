"""Push Live's changes to clients that watch a resource.

Two resources change as Live does:

- ``ableton://events``: what ``watch_live`` observers saw;
- ``ableton://session/overview``: tracks, returns and scenes (added, removed,
  reordered).

Clients on the 2025-11-25 protocol subscribe with ``resources/subscribe`` and
get ``notifications/resources/updated`` on their session. Clients on
2026-07-28 open a ``subscriptions/listen`` stream, and the server publishes
``ResourceUpdated`` events on its subscription bus, which delivers each one
only to the streams that asked for that URI.

A background task long-polls Live's event buffer (``poll_events`` with
``wait``) only while somebody is listening, and stops by itself when the last
subscription or stream goes away.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.subscriptions import InMemorySubscriptionBus, ResourceUpdated
from mcp.types import EmptyResult, SubscribeRequestParams, UnsubscribeRequestParams

logger = logging.getLogger("AbletonMCPServer")

EVENTS_URI = "ableton://events"
OVERVIEW_URI = "ableton://session/overview"
# What makes the overview stale
STRUCTURE = [("live_set", "tracks"), ("live_set", "return_tracks"), ("live_set", "scenes")]


class LiveUpdates:
    def __init__(self, bus: InMemorySubscriptionBus, asend) -> None:
        self.bus = bus
        self.asend = asend
        self.sessions: dict[str, set[Any]] = {}  # uri → sessions (2025-era subscriptions)
        self.task: asyncio.Task | None = None
        self.structure_observers: set[int] = set()  # observers whose events touch the overview
        self.owned: set[int] = set()  # the ones this watcher created (and removes)

    # ── Who is listening ────────────────────────────────────────────────────

    def listening(self) -> bool:
        legacy = any(self.sessions.values())
        # The in-memory bus keeps one listener per open listen stream
        streams = bool(getattr(self.bus, "_listeners", None))
        return legacy or streams

    def ensure_running(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self._run())

    async def on_subscribe(self, ctx: Any, params: SubscribeRequestParams) -> EmptyResult:
        self.sessions.setdefault(str(params.uri), set()).add(ctx.session)
        self.ensure_running()
        return EmptyResult()

    async def on_unsubscribe(self, ctx: Any, params: UnsubscribeRequestParams) -> EmptyResult:
        self.sessions.get(str(params.uri), set()).discard(ctx.session)
        return EmptyResult()

    async def middleware(self, ctx: Any, call_next: Any) -> Any:
        # A 2026-era client's listen stream is the response to this request
        if getattr(ctx, "method", None) == "subscriptions/listen":
            self.ensure_running()
        return await call_next(ctx)

    # ── The watcher ─────────────────────────────────────────────────────────

    async def _run(self) -> None:
        await asyncio.sleep(0.2)  # let the subscription or stream settle first
        try:
            await self._observe_structure()
            since = (await self.asend("poll_events", {"since": 10 ** 12}))["latest_seq"]
            while self.listening():
                try:
                    result = await self.asend("poll_events", {"since": since, "limit": 500,
                                                              "wait": 2.0}, timeout=20.0)
                except Exception as e:  # Live closed or restarted: try again shortly
                    logger.debug("Live updates: %s", e)
                    await asyncio.sleep(2.0)
                    continue
                since = result["latest_seq"]
                events = result.get("events") or []
                if not events:
                    # Remote Scripts before 2.0 answer at once instead of waiting
                    await asyncio.sleep(0.25)
                    continue
                await self.publish(EVENTS_URI)
                if any(e.get("observer_id") in self.structure_observers for e in events):
                    await self.publish(OVERVIEW_URI)
        finally:
            await self._unobserve_structure()

    async def publish(self, uri: str) -> None:
        await self.bus.publish(ResourceUpdated(uri=uri))
        for session in list(self.sessions.get(uri, ())):
            try:
                await session.send_resource_updated(uri)
            except Exception as e:  # that client is gone
                logger.debug("Dropping a subscription to %s: %s", uri, e)
                self.sessions[uri].discard(session)

    async def _observe_structure(self) -> None:
        result = await self.asend("batch", {"commands": [
            {"type": "observe", "params": {"path": path, "property": prop}}
            for path, prop in STRUCTURE], "stop_on_error": False})
        for step in result.get("results", []):
            if step.get("status") != "success":
                continue
            observer = step["result"]["observer_id"]
            self.structure_observers.add(observer)
            if not step["result"].get("already_observed"):
                self.owned.add(observer)

    async def _unobserve_structure(self) -> None:
        for observer in list(self.owned):
            try:
                await self.asend("unobserve", {"observer_id": observer})
            except Exception as e:
                logger.debug("Could not remove observer %s: %s", observer, e)
        self.structure_observers.clear()
        self.owned.clear()


def install(mcp: Any, bus: InMemorySubscriptionBus, asend) -> LiveUpdates:
    """Serve resources/subscribe and start the watcher for listen streams."""
    updates = LiveUpdates(bus, asend)
    server = mcp._lowlevel_server  # no public API for subscribe handlers in mcp 2.x
    server.add_request_handler("resources/subscribe", SubscribeRequestParams,
                               updates.on_subscribe)
    server.add_request_handler("resources/unsubscribe", UnsubscribeRequestParams,
                               updates.on_unsubscribe)
    mcp.middleware.append(updates.middleware)
    return updates
