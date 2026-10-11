"""The Max for Live analyzer: the device file (m4l_device.py) and what the
server does with its messages (analyzer.py)."""

import math
import socket
import struct
import time
from pathlib import Path

import pytest

from MCP_Server import analyzer, m4l_device

TEMPLATE = Path("/Applications/Ableton Live 12 Trial.app/Contents/App-Resources/Misc/"
                "Max Devices/Max Audio Effect.amxd")


def _boxes():
    kind, patch = m4l_device.read_amxd(m4l_device.analyzer_device())
    assert kind == b"aaaa"
    patcher = patch["patcher"]
    return {b["box"]["id"]: b["box"] for b in patcher["boxes"]}, patcher["lines"], patcher


def test_device_file_layout():
    data = m4l_device.analyzer_device()
    assert data[:12] == b"ampf\x04\x00\x00\x00aaaa"
    assert data[12:24] == b"meta\x04\x00\x00\x00\x00\x00\x00\x00"
    assert data[24:28] == b"ptch" and data.endswith(b"\x00")
    assert struct.unpack("<I", data[28:32])[0] == len(data) - 32
    _boxes_, _lines, patcher = _boxes()
    assert patcher["project"]["amxdtype"] == int.from_bytes(b"aaaa", "big")
    assert patcher["openinpresentation"] == 1 and patcher["devicewidth"] > 0


@pytest.mark.skipif(not TEMPLATE.is_file(), reason="Live 12 Trial is not installed")
def test_header_matches_lives_own_template():
    assert m4l_device.analyzer_device()[:24] == TEMPLATE.read_bytes()[:24]


def test_every_cord_joins_existing_inlets_and_outlets():
    boxes, lines, _ = _boxes()
    for line in lines:
        (source, outlet), (destination, inlet) = (line["patchline"]["source"],
                                                  line["patchline"]["destination"])
        assert outlet < boxes[source]["numoutlets"], line
        assert inlet < boxes[destination]["numinlets"], line


def test_the_patch_measures_and_sends_what_the_server_reads():
    boxes, lines, _ = _boxes()
    texts = [b.get("text", "") for b in boxes.values()]
    assert {"plugin~", "plugout~", "live.thisdevice", "metro 100",
            "udpsend 127.0.0.1 9880", f"prepend {m4l_device.ADDRESS}"} <= set(texts)
    edges = sorted(float(t.split()[1]) for t in texts if t.startswith("cross~"))
    assert edges == pytest.approx([31.25 * 2 ** k * 2 ** 0.5 for k in range(9)], abs=0.06)
    pack = next(i for i, b in boxes.items() if b.get("text", "").startswith("pack "))
    arguments = boxes[pack]["text"].split()[1:]
    assert arguments == ["s", "i"] + ["f"] * (len(analyzer.FIELDS) + m4l_device.BANDS)
    feeding = sorted(line["patchline"]["destination"][1] for line in lines
                     if line["patchline"]["destination"][0] == pack)
    assert feeding == list(range(len(arguments)))  # one source per inlet
    # Audio passes straight through
    plugin = next(i for i, b in boxes.items() if b.get("text") == "plugin~")
    plugout = next(i for i, b in boxes.items() if b.get("text") == "plugout~")
    assert {(0, 0), (1, 1)} <= {(line["patchline"]["source"][1], line["patchline"]["destination"][1])
                                for line in lines if line["patchline"]["source"][0] == plugin
                                and line["patchline"]["destination"][0] == plugout}


def osc(address, *args):
    def pad(raw):
        raw += b"\x00"
        return raw + b"\x00" * (-len(raw) % 4)

    tags, payload = ",", b""
    for arg in args:
        if isinstance(arg, str):
            tags += "s"
            payload += pad(arg.encode())
        elif isinstance(arg, int):
            tags += "i"
            payload += struct.pack(">i", arg)
        else:
            tags += "f"
            payload += struct.pack(">f", arg)
    return pad(address.encode()) + pad(tags.encode()) + payload


def frame(rms=0.1, mean_lr=None, side=0.0, bands=None):
    mean_lr = rms * rms if mean_lr is None else mean_lr
    bands = bands or [0.05] * m4l_device.BANDS
    return [rms * 1.4, rms * 1.4, rms, rms, mean_lr, rms, side, *bands]


def test_parse_osc():
    assert analyzer.parse_osc(osc("/a/b", "Main Bus", 7, 0.5)) == ("/a/b", ["Main Bus", 7, 0.5])
    assert analyzer.parse_osc(b"not osc") is None
    assert analyzer.parse_osc(osc("/a", 1)[:-2]) is None


def test_listener_keeps_frames_by_track_and_spots_old_devices():
    listener = analyzer.Listener(port=0)
    listener.feed(osc(m4l_device.ADDRESS, "Bass", 1, *frame()), now=10.0)
    listener.feed(osc(m4l_device.ADDRESS, "Drums", 2, *frame()), now=10.1)
    listener.feed(osc(m4l_device.ADDRESS, "Drums", 3, *frame()), now=10.2)
    assert listener.outdated_at < 0
    listener.feed(osc("/ableton-mcp/analyzer", 5, *frame()), now=11.0)  # version 1
    assert listener.outdated_at == 11.0
    fresh = listener.since(10.05)
    assert set(fresh) == {"Drums (device 2)", "Drums (device 3)"}
    assert set(listener.last_seen()) == {"Bass", "Drums"}


def test_listener_receives_udp():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    listener = analyzer.Listener(port=port)
    listener.start()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(osc(m4l_device.ADDRESS, "Main", 1, *frame()), ("127.0.0.1", port))
    for _ in range(50):
        if listener.packets:
            break
        time.sleep(0.02)
    assert list(listener.since(0)) == ["Main"]
    with pytest.raises(RuntimeError, match="Cannot listen on UDP port"):
        analyzer.Listener(port=port).start()


def test_summary_levels_loudness_and_stereo():
    result = analyzer.summary([frame(rms=0.1)] * 20)
    weights = analyzer.k_weights()
    expected = -0.691 + 10 * math.log10(sum(weights) / len(weights) * 2 * 0.01)
    assert result["loudness_lufs"] == round(expected, 1)
    assert result["rms_dbfs"] == -20.0 and result["peak_dbfs"] == pytest.approx(-17.1, abs=0.1)
    assert result["correlation"] == 1.0 and result["side_to_mid_db"] <= -90
    assert result["bands_db"]["1 kHz"] == pytest.approx(-26.0, abs=0.1)
    assert result["observations"] == []


def test_summary_notes_what_stands_out():
    flipped = analyzer.summary([frame(rms=0.1, mean_lr=-0.008, side=0.09)] * 10)
    assert flipped["correlation"] == -0.8
    assert "cancel in mono" in flipped["observations"][0]
    muddy = [0.001] * m4l_device.BANDS
    muddy[3] = muddy[4] = 0.2
    assert any("muddy" in n for n in analyzer.summary([frame(bands=muddy)] * 5)["observations"])
    silent = analyzer.summary([[0.0] * (len(analyzer.FIELDS) + m4l_device.BANDS)] * 5)
    assert silent["loudness_lufs"] is None and silent["observations"][0].startswith("Silent")


def test_k_weighting_follows_bs1770():
    gains_db = [10 * math.log10(g) for g in analyzer.k_weights()]
    assert gains_db[0] < -7 and abs(gains_db[4]) < 0.5 and 3.5 < gains_db[-1] < 4.5
