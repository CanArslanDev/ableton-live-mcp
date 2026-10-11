# Ensures `import MCP_Server.server` resolves no matter how pytest is invoked.
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Tools that change parameters save automatic snapshots: keep the test run's
# out of the user's ~/.ableton-mcp/snapshots
os.environ["ABLETON_MCP_SNAPSHOT_DIR"] = tempfile.mkdtemp(prefix="abletonmcp-snapshots-")
