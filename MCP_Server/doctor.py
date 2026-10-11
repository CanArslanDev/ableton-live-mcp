"""Diagnose the setup: versions, installed script copies, the connection,
blocking dialogs in Live, and AbletonMCP errors in Live's log.

Used by the `doctor` tool and the `ableton-mcp-doctor` command. Read-only:
it never changes the set or any file.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import stat
import sys
import time
from pathlib import Path

from .connection import AbletonConnection, read_session, session_file

from .remote_script_install import (
    EXPECTED_REMOTE_SCRIPT_VERSION,
    REMOTE_SCRIPT_FOLDER_NAME,
    _live_preference_dirs,
    discover_user_library_remote_script_dirs,
)

_VERSION_RE = re.compile(r'^SCRIPT_VERSION = "([^"]+)"', re.M)
_LOG_TAIL_BYTES = 2_000_000


def _check(name: str, status: str, detail: str, fix: str | None = None) -> dict:
    entry = {"check": name, "status": status, "detail": detail}
    if fix:
        entry["fix"] = fix
    return entry


def script_version_in(path: Path) -> str | None:
    try:
        match = _VERSION_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return match.group(1) if match else None


def _package_version() -> str:
    try:
        from importlib.metadata import version
        return version("ableton-live-mcp")
    except Exception:
        return "unknown"


def check_server() -> dict:
    from .tooling import active_profile
    try:
        from importlib.metadata import version
        mcp_version = version("mcp")
    except Exception:
        mcp_version = "unknown"
    return _check("server", "info",
                  f"ableton-live-mcp {_package_version()}, mcp {mcp_version}, "
                  f"Python {platform.python_version()}, tool profile '{active_profile()}', "
                  f"expects Remote Script {EXPECTED_REMOTE_SCRIPT_VERSION}")


def check_installed_scripts() -> dict:
    found = []
    for folder in discover_user_library_remote_script_dirs():
        init = folder / REMOTE_SCRIPT_FOLDER_NAME / "__init__.py"
        if init.is_file():
            found.append((init, script_version_in(init)))
    if not found:
        return _check("installed_script", "fail",
                      "No AbletonMCP Remote Script in any User Library",
                      "Call the install_remote_script tool (or run "
                      "`ableton-mcp-install-script`), restart Live, then select "
                      "AbletonMCP under Settings → Link, Tempo & MIDI → Control Surface.")
    stale = [(p, v) for p, v in found if v != EXPECTED_REMOTE_SCRIPT_VERSION]
    listing = "; ".join(f"{p.parent} (v{v})" for p, v in found)
    if stale:
        return _check("installed_script", "warn", f"Outdated copy: {listing}",
                      "Call install_remote_script (or run `ableton-mcp-install-script`), "
                      "then restart Live.")
    return _check("installed_script", "ok", listing)


def live_app_bundles() -> list[Path]:
    if sys.platform == "darwin":
        return sorted(Path("/Applications").glob("Ableton Live*.app"))
    if sys.platform == "win32":
        return sorted(Path("C:/ProgramData/Ableton").glob("Live*"))
    return []


def _bundle_scripts_dir(app: Path) -> Path:
    if sys.platform == "darwin":
        return app / "Contents" / "App-Resources" / "MIDI Remote Scripts"
    return app / "Resources" / "MIDI Remote Scripts"


def check_bundle_copies() -> dict:
    """A folder named AbletonMCP inside Live's own scripts shadows the User Library copy."""
    shadows = [_bundle_scripts_dir(app) / REMOTE_SCRIPT_FOLDER_NAME for app in live_app_bundles()]
    shadows = [p for p in shadows if p.exists()]
    if shadows:
        return _check("bundle_copy", "fail",
                      "Old AbletonMCP copy inside Live itself (it wins over the User "
                      "Library copy): " + ", ".join(map(str, shadows)),
                      "Delete that AbletonMCP folder (Live updates have restored it), "
                      "then restart Live.")
    return _check("bundle_copy", "ok", "No AbletonMCP folder inside the Live application")


def live_versions() -> list[str]:
    versions = []
    for app in live_app_bundles():
        plist = app / "Contents" / "Info.plist"
        try:
            with plist.open("rb") as handle:
                info = plistlib.load(handle)
            versions.append(f"{app.stem} {info.get('CFBundleShortVersionString', '?')}")
        except Exception:
            versions.append(app.name)
    return versions


def check_session() -> dict:
    """The file where Live publishes the session token (Remote Script 2.0+)."""
    path = session_file()
    info = read_session()
    if info is None:
        return _check("session", "info", f"No session file at {path}: Remote Script 2.0 "
                      "is not running, or Live runs with ABLETON_MCP_AUTH=0")
    detail = f"{path} (Live pid {info.get('pid')}, script v{info.get('script_version')})"
    if os.name == "posix":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            return _check("session", "warn", f"{detail} is readable by other users "
                          f"(mode {mode:o})", f"chmod 600 {path}")
    pid = info.get("pid")
    if isinstance(pid, int) and os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return _check("session", "warn", f"{detail} is left over: that Live is not running",
                          "Start Live; it writes a fresh file.")
        except PermissionError:
            pass
    return _check("session", "ok", detail)


def check_connection(host: str, port: int) -> tuple[list[dict], dict | None]:
    """Connection and script version checks, round-trip time, and Live's
    dialog/CPU state."""
    connection = AbletonConnection(host=host, port=port)
    if not connection.connect():
        reason = connection.last_error or "unknown error"
        if reason.startswith("Nothing answers"):
            return [_check("connection", "fail", reason,
                           "Start Live and select AbletonMCP as a Control Surface "
                           "(Settings → Link, Tempo & MIDI), Input/Output None.")], None
        return [_check("connection", "fail", f"Live refused the connection: {reason}",
                       "Restart Live so it writes a fresh token, and run the server as "
                       "the same user as Live (or set ABLETON_MCP_TOKEN).")], None
    checks: list[dict] = []
    state = None
    try:
        info = connection.send_command("get_script_info")
        version = info.get("script_version")
        if version == EXPECTED_REMOTE_SCRIPT_VERSION:
            checks.append(_check("connection", "ok",
                                 f"Remote Script v{version} on {host}:{port}, protocol "
                                 f"{connection.protocol}, token "
                                 f"{'checked' if info.get('auth_required') else 'not required'}"))
        else:
            checks.append(_check("connection", "warn",
                                 f"Live runs Remote Script v{version}, this server expects "
                                 f"v{EXPECTED_REMOTE_SCRIPT_VERSION}",
                                 "Call install_remote_script (or run "
                                 "`ableton-mcp-install-script`) and restart Live. If the "
                                 "version does not change, see the bundle_copy check."))
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            state = connection.send_command("lom_get", {"path": "live_app", "properties": [
                "open_dialog_count", "current_dialog_message", "average_process_usage",
                "peak_process_usage"]})
            timings.append((time.perf_counter() - started) * 1000)
        timings.sort()
        transport = info.get("transport") or "socket thread (before 2.0)"
        checks.append(_check("latency", "warn" if timings[2] > 250 else "ok",
                             f"{timings[2]:.0f} ms per command (median of 5; {transport})",
                             None if timings[2] <= 250 else
                             "Update the Remote Script to 2.0 (polled on Live's main "
                             "thread) and restart Live."))
    except Exception as e:
        checks.append(_check("connection", "fail", f"Connected, but the script did not "
                             f"answer properly: {e}", "Restart Live; then check live_log."))
    finally:
        connection.disconnect()
    if state:
        dialogs = state.get("open_dialog_count")
        if isinstance(dialogs, int) and dialogs > 0:
            checks.append(_check("dialogs", "warn",
                                 f"{dialogs} dialog(s) open in Live: "
                                 f"{state.get('current_dialog_message')!r}",
                                 "Close the dialog in Live; it can hold up changes to the set."))
        else:
            checks.append(_check("dialogs", "ok", "No dialog open in Live"))
        cpu = state.get("average_process_usage")
        if isinstance(cpu, (int, float)):
            status = "warn" if cpu > 70 else "info"
            checks.append(_check("cpu", status,
                                 f"Live CPU load {cpu:.0f}% average, "
                                 f"{state.get('peak_process_usage', 0):.0f}% peak"))
    return checks, state


def newest_log() -> Path | None:
    """The Log.txt Live wrote to last (several Live versions may be installed)."""
    logs = [candidate for pref in _live_preference_dirs()
            for candidate in (pref / "Log.txt", pref / "Preferences" / "Log.txt")
            if candidate.is_file()]
    return max(logs, key=lambda p: p.stat().st_mtime, default=None)


def check_live_log(log: Path | None = None) -> dict:
    log = log or newest_log()
    if log is None:
        return _check("live_log", "info", "Live's Log.txt not found")
    try:
        with log.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - _LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError as e:
        return _check("live_log", "info", f"Could not read {log}: {e}")
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if "AbletonMCP Remote Script initializing" in line]
    if not starts:
        return _check("live_log", "warn",
                      f"AbletonMCP did not start in the latest part of {log}",
                      "Select AbletonMCP as a Control Surface in Live's settings.")
    session = lines[starts[-1]:]
    errors = [line.strip() for line in session if "(AbletonMCP)" in line
              and re.search(r"error|failed|traceback|exception", line, re.I)]
    if errors:
        return _check("live_log", "warn",
                      f"{len(errors)} AbletonMCP error line(s) since the script last "
                      f"started; latest: " + " | ".join(e[-200:] for e in errors[-3:]))
    return _check("live_log", "ok", f"No AbletonMCP errors since the script last started ({log})")


def check_companion() -> dict:
    """The optional Extensions SDK add-on (Live 12.4.5+ Suite)."""
    from . import companion

    try:
        info = companion.request("song", timeout=3.0)
        session = companion.find_session()
        return _check("companion", "ok",
                      f"AbletonMCP Companion {session.get('companion') or ''} answers on port "
                      f"{session['port']} ({len(info.get('tracks', []))} tracks)".replace("  ", " "))
    except companion.CompanionUnavailable as e:
        if not companion.session_files() and "ABLETON_MCP_COMPANION_TOKEN" not in os.environ:
            return _check("companion", "info", "AbletonMCP Companion not installed (optional: "
                          "an Extensions SDK add-on for Live 12.4.5+ Suite)")
        return _check("companion", "warn", str(e),
                      "Open Live with the extension enabled (Settings › Extensions), or delete "
                      "the stale session file.")
    except Exception as e:
        return _check("companion", "warn", f"AbletonMCP Companion error: {e}")


def run(host: str = "127.0.0.1", port: int = 9877) -> dict:
    checks = [check_server(), check_installed_scripts(), check_bundle_copies()]
    versions = live_versions()
    if versions:
        checks.append(_check("live", "info", ", ".join(versions)))
    checks.append(check_session())
    connection, _state = check_connection(host, port)
    checks += connection
    checks.append(check_live_log())
    checks.append(check_companion())
    problems = [c for c in checks if c["status"] in ("fail", "warn")]
    return {"ok": not any(c["status"] == "fail" for c in checks),
            "problems": len(problems), "checks": checks}


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Diagnose the Ableton MCP setup.")
    parser.add_argument("--host", default=os.environ.get("ABLETON_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=None,
                        help="Live's port (default: ABLETON_PORT, else the one Live published)")
    parser.add_argument("--json", action="store_true", help="print the raw JSON report")
    args = parser.parse_args(argv)
    from .connection import live_port

    report = run(args.host, args.port or live_port())
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        marks = {"ok": "✓", "info": "·", "warn": "!", "fail": "✗"}
        for c in report["checks"]:
            print(f"{marks[c['status']]} {c['check']}: {c['detail']}")
            if c.get("fix"):
                print(f"    → {c['fix']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
