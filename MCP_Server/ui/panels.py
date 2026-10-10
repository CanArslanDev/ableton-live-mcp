"""The panels (MCP Apps views). Each one draws a single tool's result and may
run follow-up tools through the host; see `base.py` for the shared bridge."""

from __future__ import annotations

from . import PANELS
from .base import page

SESSION = "ui://ableton/session.html"
LEVELS = "ui://ableton/levels.html"
ANALYSIS = "ui://ableton/analysis.html"
MIX = "ui://ableton/mix.html"
REFERENCE = "ui://ableton/reference.html"
SNAPSHOTS = "ui://ableton/snapshots.html"
NOTES = "ui://ableton/notes.html"
DRUMS = "ui://ableton/drums.html"
MAKAM = "ui://ableton/makam.html"

# Runs the tool that produced this panel again, with the same arguments
_RERUN = r"""
function toolName() {
  const info = host.context.toolInfo;
  return info && info.tool ? info.tool.name : null;
}
function rerunButton(label) {
  const name = toolName();
  if (!name) return null;
  return actionButton(label || "Run again", async () => {
    host.data = await callTool(name, host.input);
    render(host.data, host.input);
  }, { busy: "Running" });
}
"""

_SESSION_JS = _RERUN + r"""
const KEYS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

function strip(track) {
  const states = [track.mute ? "M" : null, track.solo ? "S" : null, track.arm ? "R" : null]
    .filter(Boolean).join(" ");
  return el("tr", {},
    el("td", {}, el("span", { class: "swatch", style: "background:" + liveColor(track.color_index) }),
      track.name, track.type ? el("span", { class: "muted" }, " " + track.type) : null),
    el("td", { class: "num mono" }, track.volume || "–"),
    el("td", { class: "num mono" }, track.pan || "–"),
    el("td", { class: "mono" }, states),
    el("td", { class: "muted" }, (track.devices || []).join(", ")));
}

function render(data) {
  if (!data || !data.tracks) { show(notice(data && data.text ? data.text : "No session data.")); return; }
  const key = Number.isInteger(data.root_note) ? KEYS[data.root_note % 12] + " " + (data.scale_name || "") : "–";
  const tiles = el("div", { class: "tiles" },
    tile("Tempo", fmt(data.tempo, 1, "BPM")),
    tile("Meter", data.time_signature || "–"),
    tile("Key & Scale", key),
    tile("Transport", data.is_playing ? "Playing" : "Stopped", data.is_playing ? "good" : null));
  const mixer = el("table", {},
    el("tr", {}, el("th", {}, "Track"), el("th", { class: "num" }, "Volume"), el("th", { class: "num" }, "Pan"),
      el("th", {}, "M/S/R"), el("th", {}, "Devices")),
    data.tracks.map(strip), (data.return_tracks || []).map(strip),
    data.master_track ? strip(Object.assign({}, data.master_track, { name: "Master" })) : null);

  const used = new Set();
  data.tracks.forEach((t) => (t.session_clips || []).forEach((c) => used.add(c.slot)));
  const rows = (data.scenes || []).filter((s) => used.has(s.index));
  const grid = rows.length ? el("table", {},
    el("tr", {}, el("th", {}, "Scene"), data.tracks.map((t) => el("th", {}, t.name)), el("th", {})),
    rows.map((scene) => el("tr", {},
      el("td", {}, scene.name || String(scene.index + 1)),
      data.tracks.map((t) => {
        const clip = (t.session_clips || []).find((c) => c.slot === scene.index);
        if (!clip) return el("td", {});
        return el("td", { style: clip.playing ? "color:var(--good);font-weight:600" : "" },
          clip.name || "clip", el("span", { class: "muted" }, " " + fmt(clip.length, 0) + "b"));
      }),
      el("td", {}, actionButton("Fire", () => callTool("lom_call_method",
        { path: "live_set scenes " + scene.index, method: "fire" }), { busy: "Firing", done: "Fire" })))))
    : el("div", { class: "muted" }, "No Session clips yet.");

  show(el("h1", {}, "Live set"), tiles,
    el("h2", {}, "Mixer"), mixer,
    el("h2", {}, "Session clips"), grid,
    el("div", { class: "row", style: "margin-top:10px" },
      actionButton("Stop all clips", () => callTool("lom_call_method",
        { path: "live_set", method: "stop_all_clips" }), { busy: "Stopping", done: "Stop all clips" }),
      rerunButton("Refresh")));
}
"""

_LEVELS_JS = _RERUN + r"""
const FLOOR = -70, CEIL = 6;
function meter(peak, average) {
  const width = 240, height = 14;
  const x = (db) => Math.max(0, Math.min(1, (db - FLOOR) / (CEIL - FLOOR))) * width;
  const color = peak === null ? "var(--bg3)" : peak > -1 ? "var(--bad)" : peak > -6 ? "var(--warn)" : "var(--good)";
  return svg("svg", { width, height, viewBox: "0 0 " + width + " " + height },
    svg("rect", { x: 0, y: 0, width, height, rx: 3, fill: "var(--bg3)" }),
    average !== null ? svg("rect", { x: 0, y: 3, width: x(average), height: height - 6, rx: 2, fill: color, opacity: 0.55 }) : null,
    peak !== null ? svg("rect", { x: Math.max(0, x(peak) - 2), y: 0, width: 3, height, fill: color }) : null,
    svg("line", { x1: x(0), x2: x(0), y1: 0, y2: height, stroke: "var(--fg2)", "stroke-dasharray": "2 2" }));
}
function render(data) {
  if (!data || !data.levels) { show(notice(data && data.text ? data.text : "No levels.")); return; }
  const table = el("table", {},
    el("tr", {}, el("th", {}, "Track"), el("th", {}, "Level (−70 … +6 dBFS)"),
      el("th", { class: "num" }, "Peak"), el("th", { class: "num" }, "Average")),
    data.levels.map((l) => el("tr", {},
      el("td", {}, l.track), el("td", {}, meter(l.peak_dbfs, l.average_dbfs)),
      el("td", { class: "num mono" }, fmt(l.peak_dbfs, 1, "dB")),
      el("td", { class: "num mono" }, fmt(l.average_dbfs, 1, "dB")))));
  show(el("h1", {}, "Levels over " + fmt(data.seconds, 0, "s")), table,
    (data.observations || []).length ? el("ul", {}, data.observations.map((o) => el("li", {}, o))) : null,
    el("div", { class: "row", style: "margin-top:10px" }, rerunButton("Measure again")));
}
"""

_ANALYSIS_JS = _RERUN + r"""
function status(value, warnAbove, badAbove) {
  if (value === null || value === undefined) return null;
  return value > badAbove ? "bad" : value > warnAbove ? "warn" : "good";
}
function bands(energy) {
  const entries = Object.entries(energy || {});
  const top = Math.max(1, ...entries.map(([, v]) => v));
  return el("table", {}, entries.map(([name, value]) => el("tr", {},
    el("td", { style: "width:90px" }, name.replace("_", " ")),
    el("td", {}, bar(value / top)),
    el("td", { class: "num mono", style: "width:60px" }, fmt(value, 1, "%")))));
}
function render(data) {
  if (!data || data.integrated_lufs === undefined) { show(notice(data && data.text ? data.text : "No analysis.")); return; }
  const stereo = data.stereo || {};
  const tiles = el("div", { class: "tiles" },
    tile("Loudness", fmt(data.integrated_lufs, 1, "LUFS")),
    tile("True peak", fmt(data.true_peak_dbtp, 1, "dBTP"), status(data.true_peak_dbtp, -1, 0)),
    tile("Loudness range", fmt(data.loudness_range_lu, 1, "LU")),
    tile("Crest factor", fmt(data.crest_factor_db, 1, "dB"), data.crest_factor_db < 6 ? "warn" : null),
    tile("Stereo correlation", fmt(stereo.correlation, 2), stereo.correlation < 0 ? "bad" : null),
    tile("Tempo", data.tempo_estimate_bpm ? fmt(data.tempo_estimate_bpm, 0, "BPM") : "–"),
    tile("Key", data.key_estimate ? data.key_estimate.key : "–"));
  show(el("h1", {}, "What it sounds like"), tiles,
    el("h2", {}, "Energy by band"), bands(data.band_energy_percent),
    (data.observations || []).length ? [el("h2", {}, "Notes"),
      el("ul", {}, data.observations.map((o) => el("li", {}, o)))] : null,
    el("div", { class: "row", style: "margin-top:10px" }, rerunButton("Listen again")));
}
"""

_MIX_JS = _RERUN + r"""
const BANDS = ["sub", "low bass", "upper bass", "low mids", "mids", "upper mids", "presence", "brilliance", "air"];
const HZ = ["20–60", "60–120", "120–250", "250–500", "500–1k", "1–2k", "2–4k", "4–8k", "8–16k"];
function heat(value) {
  if (!value) return "transparent";
  return "color-mix(in srgb, var(--bad) " + Math.round(25 + 75 * value) + "%, transparent)";
}
function masking(pairs) {
  if (!pairs || !pairs.length) return el("div", { class: "muted" }, "No two parts fight for the same frequencies.");
  return el("table", {},
    el("tr", {}, el("th", {}, "Parts (Hz →)"), BANDS.map((b, i) => el("th", { class: "num", title: b }, HZ[i]))),
    pairs.map((p) => el("tr", { title: p.advice },
      el("td", {}, p.pair.join(" × ")),
      BANDS.map((b) => {
        const v = (p.bands || {})[b];
        return el("td", { class: "num mono", style: "background:" + heat(v) }, v ? Math.round(v * 100) + "%" : "");
      }))));
}
function stemsTable(stems) {
  const rows = Object.entries(stems || {});
  if (!rows.length) return null;
  return el("table", {},
    el("tr", {}, el("th", {}, "Stem"), el("th", { class: "num" }, "Loudness"), el("th", { class: "num" }, "Peak"),
      el("th", {}, "Main band"), el("th", {}, "")),
    rows.map(([name, s]) => el("tr", {},
      el("td", {}, name), el("td", { class: "num mono" }, fmt(s.lufs, 1, "LUFS")),
      el("td", { class: "num mono" }, fmt(s.peak_dbfs, 1, "dB")),
      el("td", {}, s.main_band || "–"),
      el("td", { class: "muted mono", title: s.file || "" }, s.file ? s.file.split("/").pop() : ""))));
}
function render(data) {
  if (!data || (!data.findings && !data.stems)) { show(notice(data && data.text ? data.text : "No mix data.")); return; }
  const m = data.master;
  const section = data.section || {};
  const where = section.mode === "session" ? "Session " + (section.scene === "playing clips" ? "clips" : "scene " + section.scene)
    : "Arrangement from beat " + fmt(section.start_beat, 0);
  const parts = [el("h1", {}, (data.findings ? "Mix doctor" : "Stems") + " · " + where + " · " + fmt(section.seconds, 1, "s"))];
  if (m) {
    parts.push(el("div", { class: "tiles" },
      tile("Master loudness", fmt(m.integrated_lufs, 1, "LUFS")),
      tile("True peak", fmt(m.true_peak_dbtp, 1, "dBTP"), m.true_peak_dbtp > -1 ? "bad" : "good"),
      tile("Crest factor", fmt(m.crest_factor_db, 1, "dB"), m.crest_factor_db < 6 ? "warn" : null),
      tile("Loudness range", fmt(m.loudness_range_lu, 1, "LU"))));
  }
  if (data.findings) parts.push(el("h2", {}, "Findings"), el("ol", {}, data.findings.map((f) => el("li", {}, f))));
  if (data.masking !== undefined) parts.push(el("h2", {}, "Masking (share of the time two parts collide)"), masking(data.masking));
  parts.push(el("h2", {}, "Stems"), stemsTable(data.stems));
  parts.push(el("div", { class: "row", style: "margin-top:10px" },
    actionButton("Save a snapshot first", () => callTool("snapshot",
      { action: "save", name: "before mix fixes " + new Date().toISOString().slice(0, 16).replace("T", " ") }),
      { busy: "Saving", done: "Snapshot saved" }),
    rerunButton("Listen again")));
  show(parts);
}
"""

_REFERENCE_JS = _RERUN + r"""
function curve(diff) {
  const entries = Object.entries(diff || {});
  if (!entries.length) return null;
  const width = Math.max(360, Math.min(640, window.innerWidth - 30)), height = 150, pad = 24;
  const top = Math.max(3, ...entries.map(([, v]) => Math.abs(v)));
  const x = (i) => pad + i * (width - 2 * pad) / (entries.length - 1);
  const y = (v) => height / 2 - v / top * (height / 2 - 14);
  const step = (width - 2 * pad) / entries.length;
  return svg("svg", { width, height, viewBox: "0 0 " + width + " " + height, role: "img",
      "aria-label": "Tonal difference: mix minus reference, dB per third octave" },
    svg("line", { x1: pad, x2: width - pad, y1: y(0), y2: y(0), stroke: "var(--line)" }),
    entries.map(([label, v], i) => svg("rect", {
      x: x(i) - step * 0.35, width: step * 0.7, y: Math.min(y(0), y(v)), height: Math.abs(y(v) - y(0)),
      fill: Math.abs(v) < 1.5 ? "var(--bg3)" : v > 0 ? "var(--warn)" : "var(--info)", rx: 1 },
      svg("title", {}, label + ": " + (v > 0 ? "+" : "") + v.toFixed(1) + " dB"))),
    entries.map(([label], i) => (i % 4 === 0) ? svg("text", { x: x(i), y: height - 4, "text-anchor": "middle" }, label.replace(" Hz", "")) : null),
    svg("text", { x: 2, y: 10 }, "+" + top.toFixed(0) + " dB"), svg("text", { x: 2, y: height - 16 }, "−" + top.toFixed(0)));
}
const LABELS = {
  integrated_lufs: "Integrated loudness (LUFS)", max_short_term_lufs: "Loudest 3 s (LUFS)",
  loudness_range_lu: "Loudness range (LU)", true_peak_dbtp: "True peak (dBTP)",
  crest_factor_db: "Crest factor (dB)", side_to_mid_db: "Width: side vs mid (dB)",
};
function render(data) {
  if (!data || !data.mix_vs_reference) { show(notice(data && data.text ? data.text : "No comparison.")); return; }
  const rows = Object.entries(data.mix_vs_reference).map(([name, [mix, ref]]) => el("tr", {},
    el("td", {}, LABELS[name] || name.replaceAll("_", " ")), el("td", { class: "num mono" }, fmt(mix, 1)),
    el("td", { class: "num mono" }, fmt(ref, 1)),
    el("td", { class: "num mono" }, mix !== null && ref !== null ? ((mix - ref > 0 ? "+" : "") + (mix - ref).toFixed(1)) : "–")));
  const moves = (data.eq_moves || []).map((m) => el("li", {},
    m.type + " " + (m.gain_db > 0 ? "+" : "") + m.gain_db.toFixed(1) + " dB at " + m.frequency_hz + " Hz",
    el("span", { class: "muted" }, " (" + m.from_hz + "–" + m.to_hz + " Hz is " + Math.abs(m.off_by_db).toFixed(1) +
      " dB " + (m.off_by_db < 0 ? "below" : "above") + " the reference)")));
  const settings = data.eq_eight_settings || {};
  show(el("h1", {}, "Mix vs reference"), el("div", { class: "muted" }, "Compared with " + (data.compared_with || "the reference")),
    el("table", {}, el("tr", {}, el("th", {}, ""), el("th", { class: "num" }, "Mix"), el("th", { class: "num" }, "Reference"),
      el("th", { class: "num" }, "Δ")), rows),
    el("h2", {}, "Tonal balance (mix − reference, levels matched)"), curve(data.tonal_difference_db),
    moves.length ? [el("h2", {}, "Suggested EQ (about 60% of the way)"), el("ul", {}, moves)] : null,
    (data.observations || []).length ? [el("h2", {}, "Notes"), el("ul", {}, data.observations.map((o) => el("li", {}, o)))] : null,
    el("div", { class: "row", style: "margin-top:10px" },
      Object.keys(settings).length ? actionButton("Add this EQ to the master", () => callTool("add_device",
        { track: "master", device: "EQ Eight", settings }), { confirm: "Click again to add it", busy: "Adding", done: "EQ added", primary: true }) : null,
      rerunButton("Compare again")));
}
"""

_SNAPSHOTS_JS = r"""
async function refresh() {
  host.data = await callTool("snapshot", { action: "list" });
  render(host.data);
}
function changes(data) {
  const rows = data.changes || [];
  if (!rows.length) return el("div", { class: "muted" }, "Nothing changed.");
  return el("table", {},
    el("tr", {}, el("th", {}, "Track"), el("th", {}, "Where"), el("th", {}, "Parameter"),
      el("th", { class: "num" }, "Saved"), el("th", { class: "num" }, "Now")),
    rows.map((c) => el("tr", {}, el("td", {}, c.track), el("td", {}, c.where), el("td", {}, c.parameter),
      el("td", { class: "num mono" }, String(c.saved)), el("td", { class: "num mono" }, String(c.now)))));
}
function list(snapshots) {
  if (!snapshots.length) return el("div", { class: "muted" }, "No snapshots yet.");
  let a = snapshots[1] ? snapshots[1].name : null, b = snapshots[0].name;
  const abRow = snapshots.length > 1 ? el("div", { class: "row", style: "margin:8px 0" },
    el("strong", {}, "A/B"),
    actionButton("Hear A (" + a + ")", () => callTool("snapshot", { action: "restore", name: a }), { busy: "Restoring", done: "A is on" }),
    actionButton("Hear B (" + b + ")", () => callTool("snapshot", { action: "restore", name: b }), { busy: "Restoring", done: "B is on" })) : null;
  return [abRow, el("table", {},
    el("tr", {}, el("th", {}, "Snapshot"), el("th", {}, "Saved"), el("th", { class: "num" }, "Parameters"), el("th", {}, "")),
    snapshots.map((s) => el("tr", {},
      el("td", {}, s.name), el("td", { class: "muted" }, s.created || ""), el("td", { class: "num" }, String(s.parameters || "")),
      el("td", {}, el("div", { class: "row" },
        actionButton("Diff", async () => { const d = await callTool("snapshot", { action: "diff", name: s.name }); render(d); }, { busy: "Comparing" }),
        actionButton("Restore", () => callTool("snapshot", { action: "restore", name: s.name }), { confirm: "Click again to restore", busy: "Restoring", done: "Restored" }),
        actionButton("Delete", async () => { await callTool("snapshot", { action: "delete", name: s.name }); await refresh(); }, { confirm: "Click again to delete", busy: "Deleting" }))))))];
}
function render(data) {
  if (!data) { show(notice("No snapshot data.")); return; }
  const back = actionButton("All snapshots", refresh, { busy: "Loading" });
  if (data.snapshots) { show(el("h1", {}, "Snapshots"), list(data.snapshots)); return; }
  if (data.changes !== undefined) {
    show(el("h1", {}, "Changes since “" + data.snapshot + "”"), el("div", { class: "muted" }, data.changed + " changed, compared to " + data.compared_to),
      changes(data),
      (data.automated || []).length ? [el("h2", {}, "Automated (left alone)"), el("ul", {}, data.automated.map((a) => el("li", {}, a)))] : null,
      el("div", { class: "row", style: "margin-top:10px" },
        actionButton("Restore “" + data.snapshot + "”", () => callTool("snapshot", { action: "restore", name: data.snapshot }), { confirm: "Click again to restore", busy: "Restoring", done: "Restored" }),
        back));
    return;
  }
  if (data.saved) {
    show(el("h1", {}, "Saved “" + data.saved + "”"), el("div", { class: "muted" }, data.parameters + " parameters on " + data.tracks.length + " tracks"),
      el("div", { class: "row", style: "margin-top:10px" },
        actionButton("What changed since?", async () => render(await callTool("snapshot", { action: "diff", name: data.saved })), { busy: "Comparing" }), back));
    return;
  }
  if (data.restored !== undefined) {
    show(el("h1", {}, "Restored “" + data.snapshot + "”"), el("div", {}, data.restored + " of " + data.changed + " values put back"),
      (data.automated_not_restored || []).length ? notice("Left alone (automated): " + data.automated_not_restored.join(", ")) : null,
      (data.errors || []).length ? notice(data.errors.map((e) => e.error || JSON.stringify(e)).join("; "), "bad") : null,
      el("div", { class: "row", style: "margin-top:10px" }, back));
    return;
  }
  show(el("pre", { class: "mono" }, data.text || JSON.stringify(data, null, 2)), back);
}
"""

_NOTES_JS = r"""
const NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function noteName(p) { return NAMES[p % 12] + (Math.floor(p / 12) - 2); }
function beatsPerBar(sig) {
  const m = /^(\d+)\/(\d+)$/.exec(sig || "");
  return m ? Number(m[1]) * 4 / Number(m[2]) : 4;
}
function roll(notes, length, sig, chords) {
  if (!notes.length) return el("div", { class: "muted" }, "The clip is empty.");
  const lo = Math.min(...notes.map((n) => n.pitch)) - 1, hi = Math.max(...notes.map((n) => n.pitch)) + 1;
  const end = Math.max(length || 0, ...notes.map((n) => n.start_time + n.duration));
  const width = Math.max(360, Math.min(720, window.innerWidth - 30)), left = 34, rowH = Math.max(5, Math.min(12, 240 / (hi - lo + 1)));
  const height = (hi - lo + 1) * rowH + 18;
  const x = (beat) => left + beat / end * (width - left - 4);
  const y = (p) => (hi - p) * rowH;
  const bar = beatsPerBar(sig);
  const lines = [];
  for (let b = 0; b <= end + 1e-6; b += bar) lines.push(svg("line", { x1: x(b), x2: x(b), y1: 0, y2: height - 18, stroke: "var(--line)" }));
  const labels = [];
  for (let p = lo; p <= hi; p++) {
    if (p % 12 === 0) labels.push(svg("text", { x: 2, y: y(p) + rowH - 1 }, noteName(p)));
    if ([1, 3, 6, 8, 10].includes(p % 12)) labels.push(svg("rect", { x: left, y: y(p), width: width - left, height: rowH, fill: "var(--bg2)" }));
  }
  const chordLabels = (chords || []).map((c, i, all) => svg("text", { x: x(i * end / all.length) + 3, y: height - 5 }, c.chord || c));
  return svg("svg", { width, height, viewBox: "0 0 " + width + " " + height, role: "img", "aria-label": "Piano roll" },
    labels, lines,
    notes.map((n) => svg("rect", { x: x(n.start_time), y: y(n.pitch) + 1, width: Math.max(2, x(n.start_time + n.duration) - x(n.start_time) - 1),
      height: rowH - 2, rx: 2, fill: n.mute ? "var(--bg3)" : "var(--accent)", opacity: (0.35 + 0.65 * (n.velocity || 100) / 127).toFixed(2) },
      svg("title", {}, noteName(n.pitch) + " · beat " + (n.start_time + 1).toFixed(2) + " · vel " + Math.round(n.velocity || 0)))),
    chordLabels);
}
function slotPath(clip) { return clip && clip.endsWith(" clip") ? clip.slice(0, -5) : null; }
async function loadNotes(input) {
  if (input.track === undefined || !canCallTools()) return null;
  const args = { track: input.track };
  if (input.arrangement_clip_index !== undefined && input.arrangement_clip_index !== null) args.arrangement_clip_index = input.arrangement_clip_index;
  else args.clip_index = input.clip_index === undefined || input.clip_index === null ? 0 : input.clip_index;
  return callTool("read_clip_notes", args);
}
let drawn = null;
function render(data, input) {
  if (!data) { show(notice("No clip data.")); return; }
  const summary = [];
  if (data.key) summary.push(data.key + " " + (data.scale || ""));
  if (data.time_signature) summary.push(data.time_signature);
  if (data.length_beats) summary.push(fmt(data.length_beats, 1, "beats"));
  if (data.notes_written !== undefined) summary.push(data.notes_written + " notes");
  if (data.usul) summary.push("usul " + data.usul + " " + (data.meter || ""));
  const head = [el("h1", {}, data.clip ? "Clip" : "Notes"), summary.length ? el("div", { class: "muted" }, summary.join(" · ")) : null];
  const buttons = el("div", { class: "row", style: "margin-top:10px" },
    slotPath(data.clip) ? actionButton("Play it", () => callTool("lom_call_method", { path: slotPath(data.clip), method: "fire" }), { busy: "Firing", done: "Play it" }) : null,
    data.clip || input.track !== undefined ? actionButton("Stop", () => callTool("lom_call_method", { path: "live_set", method: "stop_all_clips" }), { busy: "Stopping", done: "Stop" }) : null);
  if (data.notes) {
    show(head, roll(data.notes, data.length, data.time_signature, data.chords), buttons);
    return;
  }
  show(head, el("div", { class: "muted" }, "Reading the notes…"), buttons);
  if (drawn === data) return;
  drawn = data;
  loadNotes(input || {}).then((notes) => {
    if (!notes) { show(head, el("div", { class: "muted" }, data.text || "Open the clip in Live to see it."), buttons); return; }
    show(head, roll(notes.notes || [], notes.length || data.length_beats, data.time_signature, data.chords), buttons);
  }).catch((e) => show(head, notice(String(e.message || e), "bad"), buttons));
}
"""

_DRUMS_JS = r"""
const NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
function noteName(p) { return NAMES[p % 12] + (Math.floor(p / 12) - 2); }
function stem(path) { return (path || "").split("/").pop().replace(/\.[^.]+$/, ""); }
async function refresh(track) { return callTool("get_drum_pads", { track }); }
function render(data, input) {
  if (!data) { show(notice("No pads.")); return; }
  const track = data.track !== undefined ? data.track : input.track;
  const byNote = {};
  (data.pads || []).forEach((p) => {
    byNote[p.note] = { note: p.note, name: p.sample ? stem(p.sample) : p.name, role: p.role, sample: p.sample, kept: p.kept };
  });
  Object.entries(data.auto_mapping || {}).forEach(([role, note]) => { if (byNote[note]) byNote[note].role = byNote[note].role || role; });
  const pad = (note) => {
    const p = byNote[note];
    return el("div", { class: "tile", style: "min-height:74px;" + (p ? "" : "opacity:.45") },
      el("div", { class: "label" }, noteName(note) + (p && p.role ? " · " + p.role : "")),
      el("div", { style: "font-weight:600;word-break:break-word" }, p ? (p.name || (p.kept ? "(kept)" : "")) : "—"),
      p && p.role ? el("div", { class: "row", style: "margin-top:4px" },
        actionButton("Swap", async () => {
          await callTool("build_drum_kit", { track, roles: [p.role], replace: true, seed: Math.floor(Math.random() * 1e6) });
          host.data = await refresh(track);
          render(host.data, input);
        }, { busy: "Swapping" }),
        actionButton("▶", () => callTool("browser_preview", { item: p.name, category: "drums" }), { title: "Preview in Live's browser", done: "▶" })) : null);
  };
  const grid = el("div", { style: "display:grid;grid-template-columns:repeat(4,1fr);gap:6px" },
    [48, 49, 50, 51, 44, 45, 46, 47, 40, 41, 42, 43, 36, 37, 38, 39].map(pad));
  const others = Object.values(byNote).filter((p) => p.note < 36 || p.note > 51);
  show(el("h1", {}, "Drum Rack" + (track !== undefined ? " · " + track : "")), grid,
    others.length ? [el("h2", {}, "Other pads"), el("div", { class: "tiles" }, others.map((p) => pad(p.note)))] : null,
    (data.no_match || []).length ? notice("No sample found for: " + data.no_match.join(", ")) : null,
    el("div", { class: "row", style: "margin-top:10px" },
      actionButton("Stop preview", () => callTool("browser_preview", { stop: true }), { done: "Stop preview" })));
}
"""

_MAKAM_JS = r"""
const NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const BLACK = [1, 3, 6, 8, 10];
function keyboard(tonic, intervals, offsets) {
  const start = NAMES.indexOf(tonic), keys = 25, white = [];
  const marks = {};
  intervals.forEach((iv, i) => { marks[(start + iv) % 12] = offsets[i]; });
  for (let k = 0; k < keys; k++) if (!BLACK.includes((start + k) % 12)) white.push(k);
  const w = 26, h = 92, width = white.length * w;
  const x = {};
  white.forEach((k, i) => { x[k] = i * w; });
  const nodes = [];
  white.forEach((k) => {
    const pc = (start + k) % 12, on = pc in marks;
    nodes.push(svg("rect", { x: x[k], y: 0, width: w - 1, height: h, rx: 3, fill: on ? "var(--accent)" : "#fafafa", stroke: "var(--line)" },
      svg("title", {}, NAMES[pc] + (on ? " " + (marks[pc] >= 0 ? "+" : "") + marks[pc].toFixed(1) + " ¢" : ""))));
    nodes.push(svg("text", { x: x[k] + w / 2, y: h - 6, "text-anchor": "middle", style: "fill:" + (on ? "#fff" : "#555") }, NAMES[pc]));
  });
  for (let k = 0; k < keys; k++) {
    const pc = (start + k) % 12;
    if (!BLACK.includes(pc) || x[k - 1] === undefined) continue;
    const on = pc in marks;
    nodes.push(svg("rect", { x: x[k - 1] + w * 0.65, y: 0, width: w * 0.7, height: h * 0.6, rx: 2,
        fill: on ? "#b8441b" : "#333", stroke: "#111", "stroke-width": 1 },
      svg("title", {}, NAMES[pc] + (on ? " " + (marks[pc] >= 0 ? "+" : "") + marks[pc].toFixed(1) + " ¢" : ""))));
  }
  return svg("svg", { width, height: h, viewBox: "0 0 " + width + " " + h }, nodes);
}
function overview(data) {
  const cards = Object.entries(data.makams).map(([name, m]) => el("div", { class: "tile" },
    el("div", { style: "font-weight:600" }, name.replaceAll("_", " ")),
    el("div", { class: "label" }, "karar " + m.karar + " · güçlü " + m.guclu),
    el("div", { class: "mono label" }, m.steps_commas.join(" ")),
    actionButton("Open", async () => { host.data = await callTool("turkish_makam", { name }); render(host.data); }, { busy: "Loading" })));
  const usuls = Object.entries(data.usuls || {}).map(([name, text]) => el("tr", {}, el("td", {}, name.replaceAll("_", " ")), el("td", { class: "mono" }, text)));
  show(el("h1", {}, "Makams"), el("div", { class: "muted" }, "Steps in commas of " + data.comma_cents + " cents (Arel-Ezgi-Uzdilek)"),
    el("div", { class: "tiles" }, cards), el("h2", {}, "Usuls"), el("table", {}, usuls));
}
function render(data) {
  if (!data) { show(notice("No makam data.")); return; }
  if (data.makams) { overview(data); return; }
  if (!data.makam) { show(notice(data.text || "No makam data.")); return; }
  const degrees = data.notes_12tet.map((note, i) => el("tr", {},
    el("td", {}, String(i + 1)), el("td", {}, note),
    el("td", { class: "num mono" }, data.degrees_commas[i] + " k"),
    el("td", { class: "num mono" }, data.degrees_cents[i].toFixed(1) + " ¢"),
    el("td", { class: "num mono" }, (data.offsets_from_12tet_cents[i] >= 0 ? "+" : "") + data.offsets_from_12tet_cents[i].toFixed(1) + " ¢"),
    el("td", { class: "mono muted" }, i < data.steps_commas.length ? "→ " + data.steps_commas[i] : "")));
  show(el("h1", {}, "Makam " + data.makam.replaceAll("_", " ") + " on " + data.tonic),
    el("div", { class: "muted" }, data.built_from + " · karar " + data.karar + " · güçlü " + data.guclu),
    el("div", { style: "margin:10px 0;overflow-x:auto" }, keyboard(data.tonic, data.intervals_12tet, data.offsets_from_12tet_cents)),
    el("table", {}, el("tr", {}, el("th", {}, "#"), el("th", {}, "Key"), el("th", { class: "num" }, "Commas"),
      el("th", { class: "num" }, "Cents"), el("th", { class: "num" }, "vs 12-TET"), el("th", {}, "Step")), degrees),
    data.tuning_file ? notice("Tuning written: " + data.tuning_file + ". " + (data.load_it || "")) : null,
    el("div", { class: "row", style: "margin-top:10px" },
      data.tuning_file ? null : actionButton("Write the tuning for Live", async () => {
        host.data = await callTool("turkish_makam", { name: data.makam, tonic: data.tonic, tuning_file: true });
        render(host.data);
      }, { busy: "Writing", primary: true }),
      actionButton("All makams", async () => { host.data = await callTool("turkish_makam", {}); render(host.data); }, { busy: "Loading" })));
}
"""

_SPECS = [
    (SESSION, "Live set", "Mixer, Session clips and transport of the current set.", _SESSION_JS),
    (LEVELS, "Levels", "Peak and average level of each track, from Live's meters.", _LEVELS_JS),
    (ANALYSIS, "Audio analysis", "Loudness, peaks, dynamics, stereo and tonal balance.", _ANALYSIS_JS),
    (MIX, "Mix doctor", "Findings, masking between parts, and the stems.", _MIX_JS),
    (REFERENCE, "Reference comparison", "Tonal balance and loudness against a reference.", _REFERENCE_JS),
    (SNAPSHOTS, "Snapshots", "Saved mixer and device states, with A/B and restore.", _SNAPSHOTS_JS),
    (NOTES, "Piano roll", "The notes of a clip that was written or changed.", _NOTES_JS),
    (DRUMS, "Drum pads", "The Drum Rack's pads, with sample swapping and preview.", _DRUMS_JS),
    (MAKAM, "Makam", "A makam's degrees, comma pitches and Live tuning.", _MAKAM_JS),
]


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("ableton-live-mcp")
    except Exception:
        return ""


for _uri, _title, _description, _script in _SPECS:
    PANELS[_uri] = (_title, _description, page(_title, _script, version=_version()))
