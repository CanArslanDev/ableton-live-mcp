"""A stand-in for Ableton Live: the real Remote Script over a fake Live.

`ableton-mcp-simulator` loads the bundled Remote Script against fake_live's
in-memory Live Object Model and serves it the way Live does (a port and a
session token file), so the server and its tools run without Live: to try
them, and for the eval suite (evals/). Nothing makes sound, the browser holds
a few items, and only the LOM surface in fake_live.py exists.

    ableton-mcp-simulator --state-file state.json
    ABLETON_MCP_SESSION_FILE=~/.ableton-mcp/simulator-session.json ableton-mcp

The server takes the port from the session file (or ABLETON_PORT).
`--state-file` keeps a JSON description of the set (tracks, devices, clips and
notes, scenes, locators, undo history) up to date after every change, for
checking what an agent did. `--setup` runs protocol commands first, like the
server sends them ({"type": "lom_set", "params": {...}}). `--detach` returns
once the simulator serves, leaving it running in its own session until its
last client has been gone for `--idle-exit` seconds (the eval scaffold).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable

from . import fake_live

BUNDLED_SCRIPT = Path(__file__).resolve().parent / "bundled_ableton_remote_script" / "AbletonMCP_init.py"
DEFAULT_PORT = 9880
DEFAULT_SESSION_FILE = Path.home() / ".ableton-mcp" / "simulator-session.json"


def load_remote_script(module_name: str = "ableton_mcp_simulated_script") -> types.ModuleType:
    """Import the bundled Remote Script with Live's own modules stubbed."""
    fake_live.install_fake_live_module()
    if "_Framework.ControlSurface" not in sys.modules:
        framework = types.ModuleType("_Framework")
        surface_module = types.ModuleType("_Framework.ControlSurface")

        class ControlSurface(object):
            def __init__(self, *args, **kwargs):
                pass

            def log_message(self, *args, **kwargs):
                pass

            def show_message(self, *args, **kwargs):
                pass

            def disconnect(self):
                pass

            def update_display(self):
                pass

        surface_module.ControlSurface = ControlSurface
        framework.ControlSurface = surface_module
        sys.modules["_Framework"] = framework
        sys.modules["_Framework.ControlSurface"] = surface_module
    spec = importlib.util.spec_from_file_location(module_name, BUNDLED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_surface(module: types.ModuleType, song: Any | None = None) -> Any:
    """The script's control surface over a fake song, without Live's __init__."""
    song = song if song is not None else fake_live.FakeSong()
    surface = module.AbletonMCP.__new__(module.AbletonMCP)
    surface._song = song
    surface._passive_events = []
    surface._passive_lock = threading.Lock()
    surface._lom_init()
    surface.schedule_message = lambda _delay, callback: callback()
    browser = fake_live.FakeBrowser(song)
    application = fake_live.FakeObj(view=fake_live.FakeObj(), browser=browser)
    surface.application = lambda: application
    surface._transport_init()
    return surface


# ── What the set looks like, for checks ──────────────────────────────────────

def _number(value: Any) -> Any:
    if isinstance(value, float):
        value = round(value, 4)
        return int(value) if value.is_integer() else value
    return value


def _parameter(param: Any) -> dict:
    return {"value": _number(float(param.value)), "display": param.str_for_value(param.value)}


def _clip(clip: Any) -> dict:
    item: dict[str, Any] = {"name": clip.name, "length": _number(float(clip.length)),
                            "looping": bool(clip.looping), "muted": bool(clip.muted)}
    if hasattr(clip, "start_time"):  # in the Arrangement: where it plays its own time
        for key in ("start_time", "end_time", "start_marker", "loop_start", "loop_end"):
            item[key] = _number(float(getattr(clip, key)))
    if clip.is_midi_clip:
        notes = sorted(clip._notes, key=lambda n: (n.start_time, n.pitch))
        item["notes"] = [[n.pitch, _number(float(n.start_time)), _number(float(n.duration)),
                          _number(float(n.velocity))] + (["muted"] if getattr(n, "mute", False) else [])
                         for n in notes]
    else:
        item["file_path"] = clip.file_path
    envelopes = {}
    for envelope in clip.envelopes.values():
        name = getattr(envelope.parameter, "name", "?") if envelope.parameter is not None else "?"
        envelopes[name] = [[_number(float(t)), _number(float(d)), _number(float(v))]
                           for t, d, v in envelope.steps]
    if envelopes:
        item["envelopes"] = envelopes
    return item


def _device(device: Any) -> dict:
    item: dict[str, Any] = {"name": device.name, "class_name": device.class_name,
                            "parameters": {p.name: p.str_for_value(p.value)
                                           for p in device.parameters}}
    sample = getattr(device, "sample", None)
    if sample is not None:
        item["sample"] = os.path.basename(sample.file_path)
        item["reversed"] = bool(getattr(device, "reversed", False))
    pads = getattr(device, "drum_pads", None)
    if pads:
        item["pads"] = {str(p.note): p.name for p in pads if p.name or p.chains}
    return item


def _automation(track: Any, lanes: dict) -> dict:
    """Recorded Arrangement automation of the track's parameters, by
    "mixer/<name>" or "<device>/<name>": [[beat, value], ...]."""
    mixer = track.mixer_device
    owned = [("mixer", p) for p in [mixer.volume, mixer.panning] + list(mixer.sends)]
    owned += [(d.name, p) for d in track.devices for p in d.parameters]
    found = {}
    for owner, param in owned:
        lane = lanes.get(id(param))
        if lane and lane["parameter"] is param:
            found["%s/%s" % (owner, param.name)] = [[_number(t), _number(v)]
                                                    for t, v in lane["points"]]
    return found


def _track(track: Any, lanes: dict | None = None) -> dict:
    mixer = track.mixer_device
    item: dict[str, Any] = {
        "name": track.name, "type": "midi" if track.has_midi_input else "audio",
        "mute": bool(track.mute), "solo": bool(track.solo), "arm": bool(track.arm),
        "color_index": track.color_index, "color": "#%06X" % (track.color or 0),
        "volume": _parameter(mixer.volume), "pan": _parameter(mixer.panning),
        "sends": [_parameter(s) for s in mixer.sends],
        "devices": [_device(d) for d in track.devices],
        "clip_slots": {str(i): _clip(slot.clip) for i, slot in enumerate(track.clip_slots)
                       if slot.has_clip and slot.clip is not None},
    }
    if track.arrangement_clips:
        item["arrangement_clips"] = [_clip(c) for c in track.arrangement_clips]
    automation = _automation(track, lanes or {})
    if automation:
        item["automation"] = automation
    routing = getattr(track, "input_routing_type", None)
    if not track.has_midi_input and routing is not None:
        item["input"] = routing.display_name
    return item


def describe_song(song: Any, browser: Any | None = None) -> dict:
    """The set as plain JSON: what an eval or a test checks."""
    state: dict[str, Any] = {
        "tempo": _number(float(song.tempo)),
        "signature": "%d/%d" % (song.signature_numerator, song.signature_denominator),
        "key": {"root_note": song.root_note, "scale_name": song.scale_name},
        "swing_amount": _number(float(song.swing_amount)),
        "is_playing": bool(song.is_playing),
        "loop": {"on": bool(song.loop), "start": _number(float(song.loop_start)),
                 "length": _number(float(song.loop_length))},
        "record_mode": bool(song.record_mode),
        "tracks": [_track(t, song.automation) for t in song.tracks],
        "return_tracks": [_track(t, song.automation) for t in song.return_tracks],
        "master": _track(song.master_track, song.automation),
        "scenes": [{"name": s.name, "tempo": _number(float(s.tempo))} for s in song.scenes],
        "locators": sorted(({"name": c.name, "time": _number(float(c.time))}
                            for c in song.cue_points), key=lambda c: c["time"]),
        "selected_track": getattr(song.view.selected_track, "name", None),
        "data": dict(vars(song).get("_data", {})),
        # steps: what Live's undo history holds now (commands that changed
        # nothing, or only what Live does not undo, leave none)
        "undo": {"steps": len(song._undo), "commands": song.undo_log.count("begin"),
                 "undone": song.undo_log.count("undo"), "redone": song.undo_log.count("redo")},
    }
    if browser is not None:
        state["browser"] = {"loaded": [list(item) for item in browser.loaded],
                            "previewing": browser.previewing}
    return state


# ── Serving it ───────────────────────────────────────────────────────────────

def _write_json(path: Path, data: Any) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def load_check(path: Path | str) -> Callable[[dict, dict], dict]:
    """`check(state, initial) -> {name: bool}` from a Python file; its folder's
    parent goes on sys.path first, for shared helpers (evals/checklib.py)."""
    path = Path(path).resolve()
    for folder in (str(path.parent.parent), str(path.parent)):
        if folder not in sys.path:
            sys.path.insert(0, folder)
    spec = importlib.util.spec_from_file_location("simulator_check_%d" % abs(hash(path)), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


class Simulator:
    """The Remote Script serving a fake set on `port`, polled by `serve()`
    or by a background thread (`start()`)."""

    def __init__(self, port: int = DEFAULT_PORT, session_file: Path | str = DEFAULT_SESSION_FILE,
                 state_file: Path | str | None = None,
                 on_change: Callable[[dict], None] | None = None,
                 check: Callable[[dict, dict], dict] | None = None,
                 checks_file: Path | str | None = None) -> None:
        self.module = load_remote_script()
        self.module.DEFAULT_PORT = port
        self.module.SESSION_FILE = str(Path(session_file).expanduser())
        self.port = port
        self.session_file = Path(self.module.SESSION_FILE)
        self.state_file = Path(state_file).expanduser() if state_file else None
        self.on_change = on_change
        self.check = check
        self.checks_file = Path(checks_file).expanduser() if checks_file else None
        self.initial: dict | None = None
        self.song = fake_live.FakeSong(running_clock=True)
        self.surface = build_surface(self.module, self.song)
        self._running = threading.Event()
        self._thread: threading.Thread | None = None
        run_command = self.surface._process_command

        def process(command: dict) -> dict:
            response = run_command(command)
            if not self.surface._is_read_only(command.get("type", ""), command.get("params") or {}):
                self._changed()
            return response

        self.surface._process_command = process

    @property
    def browser(self) -> Any:
        return self.surface.application().browser

    def state(self) -> dict:
        return describe_song(self.song, self.browser)

    def checks(self, state: dict | None = None) -> dict:
        """The check's verdicts on the set ({"check:<name>": bool}), against
        the set as it was before anyone connected."""
        state = state if state is not None else self.state()
        try:
            verdicts = self.check(state, self.initial or state) if self.check else {}
            return {"check:%s" % name: bool(ok) for name, ok in verdicts.items()}
        except Exception as e:  # a set the check did not expect: nothing passes
            return {"error": "%s: %s" % (type(e).__name__, e)}

    def _changed(self) -> None:
        if self.state_file is None and self.on_change is None and self.checks_file is None:
            return
        state = self.state()
        try:
            if self.state_file is not None:
                _write_json(self.state_file, state)
            if self.checks_file is not None:
                _write_json(self.checks_file, self.checks(state))
        except OSError as e:  # the folder went away (an eval run's workspace)
            print("Could not write the state: %s" % e, file=sys.stderr)
        if self.on_change is not None:
            self.on_change(state)

    def run_setup(self, commands: list[dict]) -> None:
        """Commands to shape the set before anyone connects; they leave no undo
        history behind, like a set that was just opened."""
        for command in commands:
            response = self.surface._process_command(command)
            if response.get("status") != "success":
                raise RuntimeError("Setup command %s failed: %s"
                                   % (command.get("type"), response.get("message")))
        for history in (self.song.undo_log, self.song._undo, self.song._redo):
            del history[:]
        self._changed()

    def listen(self) -> None:
        self.initial = self.state()
        self.surface.start_server()
        if not self.surface.running:
            raise OSError("Could not listen on 127.0.0.1:%d (in use?)" % self.port)
        self._running.set()
        self._changed()

    def serve(self, idle_exit: float | None = None, lifetime: float | None = None) -> None:
        """Poll the sockets until stop(), the script's own loop in Live; or
        until no client has been connected for `idle_exit` seconds (counted
        from the last one leaving), or `lifetime` seconds have passed."""
        if not self._running.is_set():
            self.listen()
        started = last_client = time.monotonic()
        seen_client = False
        while self._running.is_set():
            self.surface._poll_sockets()
            now = time.monotonic()
            if self.surface._clients:
                seen_client, last_client = True, now
            elif idle_exit is not None and seen_client and now - last_client > idle_exit:
                break
            if lifetime is not None and now - started > lifetime:
                break
            time.sleep(0.002)

    def start(self) -> Simulator:
        self.listen()
        self._thread = threading.Thread(target=self.serve, name="ableton-simulator", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running.clear()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        surface = self.surface
        surface.running = False
        for client in list(surface._clients):
            surface._drop_client(client)
        if surface.server is not None:
            surface.server.close()
            surface.server = None
        surface._withdraw_session()

    def __enter__(self) -> Simulator:
        return self.start()

    def __exit__(self, *_exc: Any) -> None:
        self.stop()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _detach(argv: list[str], session_file: Path, log_file: str | None) -> int:
    """Start this command again in its own session and return once it serves."""
    child_argv = [a for a in argv if a != "--detach"]
    log = open(log_file, "ab") if log_file else subprocess.DEVNULL
    options: dict[str, Any] = {"start_new_session": True} if os.name == "posix" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    try:
        child = subprocess.Popen([sys.executable, "-m", "MCP_Server.simulator", *child_argv],
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
    finally:
        if log is not subprocess.DEVNULL:
            log.close()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if child.poll() is not None:
            print("The simulator stopped (exit %s)" % child.returncode, file=sys.stderr)
            return 1
        try:
            session = json.loads(session_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            session = {}
        if session.get("pid") == child.pid:
            print("Simulated Live set on 127.0.0.1:%s (pid %d)" % (session.get("port"), child.pid))
            return 0
        time.sleep(0.05)
    child.kill()
    print("The simulator did not start in time", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ableton-mcp-simulator",
        description="Serve an in-memory Ableton Live set through the real Remote Script, "
                    "so the MCP server runs without Live.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port to listen on (default %(default)s; Live uses 9877; 0 picks a "
                             "free one, published in the session file)")
    parser.add_argument("--session-file", default=str(DEFAULT_SESSION_FILE),
                        help="where to write the port and token (default %(default)s)")
    parser.add_argument("--state-file", help="keep a JSON description of the set here")
    parser.add_argument("--setup", help="JSON file: a list of commands to run first")
    parser.add_argument("--check", help="Python file whose check(state, initial) returns "
                                        "{name: bool}, kept in --checks-file")
    parser.add_argument("--checks-file", help="where the check's verdicts go")
    parser.add_argument("--detach", action="store_true",
                        help="run in the background and return once it serves")
    parser.add_argument("--idle-exit", type=float, metavar="SECONDS",
                        help="stop once the last client has been gone this long")
    parser.add_argument("--lifetime", type=float, metavar="SECONDS",
                        help="stop after this long in any case")
    parser.add_argument("--log-file", help="with --detach: where its output goes")
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)
    if args.port == 0:
        args.port = free_port()
        argv += ["--port", str(args.port)]
    if args.detach:
        return _detach(argv, Path(args.session_file).expanduser(), args.log_file)

    simulator = Simulator(args.port, args.session_file, args.state_file,
                          check=load_check(args.check) if args.check else None,
                          checks_file=args.checks_file)
    if args.setup:
        simulator.run_setup(json.loads(Path(args.setup).read_text(encoding="utf-8")))
    try:
        simulator.listen()
    except OSError as e:
        print(e, file=sys.stderr)
        return 1

    def finish(*_args: Any) -> None:
        simulator._running.clear()

    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    print("Simulated Live set on 127.0.0.1:%d. Run the server against it with:\n"
          "  ABLETON_MCP_SESSION_FILE=%s ableton-mcp"
          % (args.port, simulator.session_file), flush=True)
    try:
        simulator.serve(args.idle_exit, args.lifetime)
    finally:
        simulator.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
