"""The TCP connection to the AbletonMCP Remote Script running inside Live.

Protocol 2 (Remote Script 2.0+): one JSON object per line in both directions,
every request carries an "id" that its reply echoes, and the first request is
"hello" with the token Live writes to the session file at every launch.
Remote Scripts before 2.0 answer hello with "Unknown command"; the connection
then falls back to protocol 1, where replies are bare objects without an id.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import journal

logger = logging.getLogger("AbletonMCPServer")

DEFAULT_TIMEOUT = 15.0
# Commands that can keep Live's main thread busy for a while (decoding a large
# audio file, walking the browser, a long batch).
LONG_RUNNING_COMMANDS = {
    "create_audio_clip": 65.0,
    "batch": 125.0,
    "get_browser_tree": 35.0,
    "get_browser_items_at_path": 35.0,
    "get_browser_item": 35.0,
    "get_session_snapshot": 35.0,
    "load_browser_item": 35.0,
    "load_instrument_or_effect": 35.0,
}


class AbletonCommandError(Exception):
    """Live refused a command (bad path, wrong value, …). The connection is fine."""


class AbletonConnectionError(Exception):
    """The connection failed mid-request; it was dropped and the next call reconnects."""


def session_file() -> Path:
    """Where the Remote Script publishes its port and session token."""
    override = os.environ.get("ABLETON_MCP_SESSION_FILE")
    return Path(override) if override else Path.home() / ".ableton-mcp" / "live-session.json"


def read_session() -> dict | None:
    try:
        data = json.loads(session_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def live_port() -> int:
    """ABLETON_PORT, else the port the Remote Script published, else 9877."""
    if os.environ.get("ABLETON_PORT"):
        return int(os.environ["ABLETON_PORT"])
    port = (read_session() or {}).get("port")
    return port if isinstance(port, int) and 0 < port < 65536 else 9877


def session_token() -> str | None:
    """ABLETON_MCP_TOKEN wins (for setups where the file is out of reach)."""
    return os.environ.get("ABLETON_MCP_TOKEN") or (read_session() or {}).get("token")


@dataclass
class AbletonConnection:
    host: str
    port: int
    sock: socket.socket | None = None
    # 2 = lines with ids (Remote Script 2.0+), 1 = an older script, 0 = unknown
    protocol: int = 0
    # What hello returned (protocol 2), like get_script_info
    script_info: dict | None = None
    last_error: str | None = None

    # One socket, several threads (tools run on worker threads): a request
    # and its reply are one exchange under this lock.
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _inbox: bytearray = field(default_factory=bytearray, repr=False)
    _next_id: int = field(default=0, repr=False)

    def connect(self) -> bool:
        """Open the socket and say hello. False (see last_error) on failure."""
        if self.sock:
            return True
        try:
            sock = socket.create_connection((self.host, self.port), timeout=5.0)
        except OSError as e:
            self.last_error = f"Nothing answers on {self.host}:{self.port} ({e})"
            logger.error("Failed to connect to Ableton: %s", self.last_error)
            return False
        self.sock, self.protocol, self._inbox = sock, 0, bytearray()
        try:
            self._greet()
        except Exception as e:  # noqa: BLE001 — any failure means no usable session
            self.last_error = str(e)
            logger.error("Ableton refused the connection: %s", e)
            self._drop_socket()
            return False
        self.last_error = None
        logger.info("Connected to Ableton at %s:%s (protocol %d)",
                    self.host, self.port, self.protocol)
        return True

    def _greet(self) -> None:
        try:
            self.script_info = self._exchange(
                "hello", {"token": session_token(), "protocol_version": 2}, 5.0)
        except AbletonCommandError as e:
            if "Unknown command" not in str(e):
                raise
            # A Remote Script before 2.0: no hello, no token, bare replies
            self.protocol, self.script_info = 1, None
            return
        self.protocol = 2
        if isinstance(self.script_info, dict) and self.script_info.get("script_version"):
            from .script_handshake import remember

            remember(self.script_info)

    def disconnect(self) -> None:
        with self._lock:
            self._drop_socket()

    def send_command(self, command_type: str, params: dict[str, Any] | None = None,
                     timeout: float | None = None) -> Any:
        """Send a command and return its result.

        Raises AbletonCommandError when Live refuses the command (the
        connection stays usable); any other failure drops the socket so the
        next call reconnects.
        """
        with self._lock:
            if not self.sock and not self.connect():
                raise ConnectionError(f"Not connected to Ableton: {self.last_error}")
            if timeout is None:
                timeout = LONG_RUNNING_COMMANDS.get(command_type, DEFAULT_TIMEOUT)
            try:
                result = self._exchange(command_type, params or {}, timeout)
                journal.note_command(command_type, params, result)
                return result
            except AbletonCommandError:
                raise
            except TimeoutError:
                logger.error("Timeout waiting for Ableton to answer %s", command_type)
                self._drop_socket()
                raise AbletonConnectionError("Timeout waiting for Ableton response") from None
            except OSError as e:
                logger.error("Connection to Ableton lost: %s", e)
                self._drop_socket()
                raise AbletonConnectionError(f"Connection to Ableton lost: {e}") from e
            except ValueError as e:
                logger.error("Invalid response from Ableton: %s", e)
                self._drop_socket()
                raise AbletonConnectionError(f"Invalid response from Ableton: {e}") from e
            except Exception as e:  # the stream is in an unknown state
                logger.error("Error communicating with Ableton: %s", e)
                self._drop_socket()
                raise AbletonConnectionError(f"Communication error with Ableton: {e}") from e

    def _exchange(self, command_type: str, params: dict, timeout: float) -> Any:
        self._next_id += 1
        request_id = self._next_id
        request = {"id": request_id, "type": command_type, "params": params}
        logger.debug("Sending %s (id %d)", command_type, request_id)
        self.sock.settimeout(timeout)
        self.sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
        deadline = time.monotonic() + timeout
        while True:
            reply = self._read_message(deadline)
            reply_id = reply.get("id")
            if reply_id is None or reply_id == request_id:
                break
            logger.debug("Skipped a reply to an earlier request (id %s)", reply_id)
        if reply.get("status") == "error":
            logger.info("Ableton refused %s: %s", command_type, reply.get("message"))
            raise AbletonCommandError(reply.get("message") or "Unknown error from Ableton")
        return reply.get("result", {})

    def _read_message(self, deadline: float) -> dict:
        scanned = 0
        while True:
            newline = self._inbox.find(b"\n", scanned)
            if newline >= 0:
                line = bytes(self._inbox[:newline])
                del self._inbox[:newline + 1]
                scanned = 0
                if line.strip():
                    return json.loads(line)
                continue
            scanned = len(self._inbox)
            if self.protocol != 2 and self._inbox.rstrip().endswith(b"}"):
                # Scripts before 2.0 reply with a bare object and no newline
                try:
                    message = json.loads(bytes(self._inbox))
                except ValueError:
                    pass
                else:
                    self._inbox.clear()
                    return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("Live closed the connection")
            self._inbox += chunk

    def _drop_socket(self) -> None:
        """Close a socket whose request/reply stream is in an unknown state."""
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.protocol = 0
        self._inbox = bytearray()
