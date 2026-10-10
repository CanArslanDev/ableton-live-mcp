"""Ableton Live integration through the Model Context Protocol."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("ableton-live-mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0+unknown"

# Expose key classes and functions for easier imports
from .server import AbletonConnection, get_ableton_connection  # noqa: E402

__all__ = ["AbletonConnection", "get_ableton_connection", "__version__"]
