# Ensures `import MCP_Server.server` resolves no matter how pytest is invoked.
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Tools that change parameters save automatic snapshots: keep the test run's
# out of the user's ~/.ableton-mcp/snapshots
os.environ["ABLETON_MCP_SNAPSHOT_DIR"] = tempfile.mkdtemp(prefix="abletonmcp-snapshots-")
os.environ["ABLETON_MCP_MEMORY_FILE"] = os.path.join(
    tempfile.mkdtemp(prefix="abletonmcp-memory-"), "memory.json")
# Reference jobs' records and data (reference_jobs.py) stay out of the user's
# ~/.ableton-mcp/jobs and ~/Music/Ableton MCP too
os.environ["ABLETON_MCP_JOBS_DIR"] = tempfile.mkdtemp(prefix="abletonmcp-jobs-")
os.environ["ABLETON_MCP_REFERENCE_DIR"] = tempfile.mkdtemp(prefix="abletonmcp-reference-")
