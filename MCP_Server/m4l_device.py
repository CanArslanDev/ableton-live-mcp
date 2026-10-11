"""The AbletonMCP Analyzer: a Max for Live audio effect, built here as bytes.

It passes audio through untouched and, ten times a second, sends what the
Live Object Model cannot give the server: the level of ten octave bands,
stereo correlation and side/mid balance, plus peaks and RMS, as one OSC
message over UDP to 127.0.0.1:9880 (analyzer.py listens there):

    /ableton-mcp/analyzer/2  track  id  peakL peakR  rmsL rmsR  meanLR  rmsMid rmsSide
                             band1 … band10              (RMS of the mid signal)

The bands come from a chain of cross~ crossovers (3rd-order Butterworth) at
the octave edges, so they split the spectrum without overlap: on a test mix
they matched octave filters within ~1 dB and summed to the full-band power.
(fffb~'s resonant bands let the bass leak into every band above: 5–15 dB
too much at the top.) The version in the address tells an older copy of the
device, which Live keeps until it is replaced, from this one.

`track` is the name of the track it sits on, read on every message so a
rename shows at once; `id` is Max's id for the device, which tells two
analyzers on same-named tracks apart (it is not a LOM id the Remote Script
knows). An .amxd file is chunks — "ampf" + device type, "meta", "ptch" —
with the Max patcher as NUL-terminated JSON in "ptch", as in Live's own
template (Misc/Max Devices/Max Audio Effect.amxd).
"""

from __future__ import annotations

import json
import struct

NAME = "AbletonMCP Analyzer"
VERSION = 2
ADDRESS_PREFIX = "/ableton-mcp/analyzer"
ADDRESS = f"{ADDRESS_PREFIX}/{VERSION}"
PORT = 9880
BAND_START_HZ = 31.25  # ten octaves: 31.25 Hz … 16 kHz
BANDS = 10
WINDOW = 2205  # samples averaged per value (50 ms at 44.1 kHz)
INTERVAL_MS = 100
AUDIO_EFFECT = b"aaaa"


class Patch:
    """Boxes and patch cords, in Max's patcher JSON."""

    def __init__(self) -> None:
        self.boxes: list[dict] = []
        self.lines: list[dict] = []

    def add(self, text: str, x: float, y: float, inlets: int = 1, outlets: int = 1,
            outlettype: list[str] | None = None, maxclass: str = "newobj", **extra) -> str:
        box_id = f"obj-{len(self.boxes) + 1}"
        box = {"id": box_id, "maxclass": maxclass, "numinlets": inlets, "numoutlets": outlets,
               "patching_rect": [float(x), float(y), max(40.0, 7.0 * len(text) + 12), 22.0],
               "fontname": "Arial Bold", "fontsize": 10.0}
        if outlets:
            box["outlettype"] = outlettype or [""] * outlets
        if text:
            box["text"] = text
        box.update(extra)
        self.boxes.append({"box": box})
        return box_id

    def connect(self, source: str, outlet: int, destination: str, inlet: int = 0) -> None:
        self.lines.append({"patchline": {"source": [source, outlet],
                                         "destination": [destination, inlet]}})


def analyzer_patch(port: int = PORT) -> dict:
    p = Patch()
    signal2 = ["signal", "signal"]
    plugin = p.add("plugin~", 40, 20, 2, 2, signal2)
    plugout = p.add("plugout~", 40, 560, 2, 2, signal2)
    p.connect(plugin, 0, plugout, 0)
    p.connect(plugin, 1, plugout, 1)

    # mid = (L + R) / 2, side = (L - R) / 2, L × R for the correlation
    add_lr = p.add("+~", 260, 60, 2, 1, ["signal"])
    sub_lr = p.add("-~", 330, 60, 2, 1, ["signal"])
    mul_lr = p.add("*~", 400, 60, 2, 1, ["signal"])
    for box in (add_lr, sub_lr, mul_lr):
        p.connect(plugin, 0, box, 0)
        p.connect(plugin, 1, box, 1)
    mid = p.add("*~ 0.5", 260, 95, 2, 1, ["signal"])
    side = p.add("*~ 0.5", 330, 95, 2, 1, ["signal"])
    p.connect(add_lr, 0, mid, 0)
    p.connect(sub_lr, 0, side, 0)

    # On load: the device's id, and a live.object pointing at its track
    this_device = p.add("live.thisdevice", 40, 120, 1, 3, ["bang", "int", "int"])
    on_load = p.add("t b b", 40, 150, 1, 2, ["bang", "bang"])
    p.connect(this_device, 0, on_load, 0)
    path_message = p.add("path this_device", 100, 180, 2, 1, maxclass="message")
    live_path = p.add("live.path", 100, 210, 1, 3, ["", "", ""])
    strip_id = p.add("route id", 100, 240, 2, 2, ["", ""])
    device_id = p.add("i", 100, 270, 2, 1, ["int"])
    p.connect(on_load, 1, path_message, 0)
    p.connect(path_message, 0, live_path, 0)
    p.connect(live_path, 0, strip_id, 0)
    p.connect(strip_id, 0, device_id, 1)
    track_message = p.add("path this_device canonical_parent", 230, 180, 2, 1,
                          maxclass="message")
    track_path = p.add("live.path", 230, 210, 1, 3, ["", "", ""])
    track_object = p.add("live.object", 230, 420, 2, 1, [""])
    p.connect(on_load, 0, track_message, 0)
    p.connect(track_message, 0, track_path, 0)
    p.connect(track_path, 0, track_object, 1)

    send_toggle = p.add("", 40, 210, 1, 2, ["", ""], maxclass="live.toggle",
                        parameter_enable=1, presentation=1,
                        presentation_rect=[8.0, 52.0, 15.0, 15.0], varname="Send",
                        saved_attribute_attributes={"valueof": {
                            "parameter_longname": "Send", "parameter_shortname": "Send",
                            "parameter_type": 2, "parameter_mmax": 1.0,
                            "parameter_enum": ["off", "on"], "parameter_initial_enable": 1,
                            "parameter_initial": [1]}})
    metro = p.add(f"metro {INTERVAL_MS}", 40, 270, 2, 1, ["bang"])
    p.connect(send_toggle, 0, metro, 0)
    tick = p.add("t b b", 40, 300, 1, 2, ["bang", "bang"])
    p.connect(metro, 0, tick, 0)

    values = 7 + BANDS
    pack = p.add("pack s i " + " ".join(["f"] * values), 40, 470, values + 2, 1)
    # The right outlet fires first: the id and every reading go to cold
    # inlets; then the track's name reaches the hot inlet and sends it all
    p.connect(tick, 1, device_id, 0)
    p.connect(device_id, 0, pack, 1)
    get_name = p.add("get name", 230, 390, 2, 1, maxclass="message")
    strip_name = p.add("route name", 230, 445, 2, 2, ["", ""])
    as_symbol = p.add("tosymbol", 230, 470, 1, 1, [""])
    p.connect(tick, 0, get_name, 0)
    p.connect(get_name, 0, track_object, 0)
    p.connect(track_object, 0, strip_name, 0)
    p.connect(strip_name, 0, as_symbol, 0)
    p.connect(as_symbol, 0, pack, 0)

    slot = 2

    def measure(source: str, analysis: str, x: float) -> None:
        nonlocal slot
        reader = p.add(analysis, x, 330, 1 if analysis.startswith("average") else 2, 1,
                       ["signal"] if analysis.startswith("average") else [""])
        p.connect(source, 0, reader, 0)
        if analysis.startswith("average"):
            snapshot = p.add("snapshot~", x, 365, 2, 1, [""])
            p.connect(reader, 0, snapshot, 0)
            p.connect(tick, 1, snapshot, 0)
            p.connect(snapshot, 0, pack, slot)
        else:  # peakamp~: the peak since the last bang
            p.connect(tick, 1, reader, 0)
            p.connect(reader, 0, pack, slot)
        slot += 1

    left = p.add("*~ 1.", 120, 60, 2, 1, ["signal"])
    right = p.add("*~ 1.", 190, 60, 2, 1, ["signal"])
    p.connect(plugin, 0, left, 0)
    p.connect(plugin, 1, right, 0)
    measure(left, "peakamp~", 120)
    measure(right, "peakamp~", 190)
    measure(left, f"average~ {WINDOW} rms", 260)
    measure(right, f"average~ {WINDOW} rms", 330)
    measure(mul_lr, f"average~ {WINDOW} bipolar", 400)
    measure(mid, f"average~ {WINDOW} rms", 470)
    measure(side, f"average~ {WINDOW} rms", 540)
    # Octave bands: each crossover keeps one band below its edge and passes
    # the rest up the chain; what is left above the last edge is the top band
    rest, outlet = mid, 0
    for band in range(BANDS):
        if band < BANDS - 1:
            edge = BAND_START_HZ * 2 ** band * 2 ** 0.5
            crossover = p.add(f"cross~ {edge:.1f}", 610 + 70 * band, 160, 2, 2,
                              ["signal", "signal"])
            p.connect(rest, outlet, crossover, 0)
            source, source_outlet, rest, outlet = crossover, 0, crossover, 1
        else:
            source, source_outlet = rest, outlet
        reader = p.add(f"average~ {WINDOW} rms", 610 + 70 * band, 330, 1, 1, ["signal"])
        p.connect(source, source_outlet, reader, 0)
        snapshot = p.add("snapshot~", 610 + 70 * band, 365, 2, 1, [""])
        p.connect(reader, 0, snapshot, 0)
        p.connect(tick, 1, snapshot, 0)
        p.connect(snapshot, 0, pack, slot)
        slot += 1

    prepend = p.add(f"prepend {ADDRESS}", 40, 500, 1, 1)
    sender = p.add(f"udpsend 127.0.0.1 {port}", 40, 530, 1, 0)
    p.connect(pack, 0, prepend, 0)
    p.connect(prepend, 0, sender, 0)

    p.add("AbletonMCP\nAnalyzer", 0, 0, 1, 0, maxclass="live.comment", presentation=1,
          presentation_rect=[6.0, 6.0, 110.0, 30.0], presentation_linecount=2,
          patching_rect=[480.0, 20.0, 110.0, 30.0])
    p.add(f"send to :{port}", 0, 0, 1, 0, maxclass="live.comment", presentation=1,
          presentation_rect=[24.0, 52.0, 90.0, 18.0], patching_rect=[480.0, 52.0, 90.0, 18.0])

    return {"patcher": {
        "fileversion": 1,
        "appversion": {"major": 8, "minor": 1, "revision": 2, "architecture": "x64",
                       "modernui": 1},
        "classnamespace": "box",
        "rect": [100.0, 100.0, 1400.0, 640.0],
        "openrect": [0.0, 0.0, 0.0, 169.0],
        "openinpresentation": 1,
        "default_fontsize": 10.0, "default_fontface": 0, "default_fontname": "Arial Bold",
        "gridsize": [8.0, 8.0], "devicewidth": 120.0,
        "description": "Sends octave bands, stereo correlation and levels to the "
                       "AbletonMCP server (UDP 127.0.0.1:%d). Audio passes through." % port,
        "digest": "", "tags": "", "style": "", "subpatcher_template": "",
        "boxes": p.boxes, "lines": p.lines, "dependency_cache": [], "latency": 0,
        "project": {"version": 1, "creationdate": 3590052493, "modificationdate": 3590052493,
                    "viewrect": [0.0, 0.0, 300.0, 500.0], "autoorganize": 1,
                    "hideprojectwindow": 1, "showdependencies": 1, "autolocalize": 0,
                    "contents": {"patchers": {}}, "layout": {}, "searchpath": {},
                    "detailsvisible": 0, "amxdtype": int.from_bytes(AUDIO_EFFECT, "big"),
                    "readonly": 0, "devpathtype": 0, "devpath": ".", "sortmode": 0,
                    "viewmode": 0},
        "autosave": 0,
    }}


def amxd(patch: dict, device_type: bytes = AUDIO_EFFECT) -> bytes:
    body = json.dumps(patch, indent="\t").encode("utf-8") + b"\x00"
    return (b"ampf" + struct.pack("<I", 4) + device_type
            + b"meta" + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
            + b"ptch" + struct.pack("<I", len(body)) + body)


def read_amxd(data: bytes) -> tuple[bytes, dict]:
    """(device type, patcher) of an unfrozen .amxd."""
    if data[:4] != b"ampf":
        raise ValueError("Not a Max for Live device")
    device_type = data[8:12]
    pos = 12
    while pos < len(data):
        tag, size = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + size]
        if tag == b"ptch":
            return device_type, json.loads(body.rstrip(b"\x00").decode("utf-8"))
        pos += 8 + size
    raise ValueError("No patcher in this device")


def analyzer_device(port: int = PORT) -> bytes:
    return amxd(analyzer_patch(port))
