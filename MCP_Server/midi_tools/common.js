// Shared by the AbletonMCP MIDI Tools (Max for Live, v8 in Max 9). The data
// above this (USULS, STROKES, MAKAMS, NOTE_NAMES, MELODY_RHYTHMS) is written
// from MCP_Server/turkish.py and music.py when the devices are built.
//
// live.miditool.in sends the context (inlet 1) and then the clip's notes
// (inlet 0); the tool's transform(notes, context) answers with the notes to
// write, out to live.miditool.out. A control that changes after the first
// Apply bangs live.miditool.in again, so the result follows it, as Live's own
// tools do.

inlets = 2;
outlets = 2;

var context = {};
var params = {};
var applied = false;

function msg_dictionary(d) {
  if (inlet === 1) {
    context = d || {};
    return;
  }
  applied = true;
  var notes = (d && d.notes) || [];
  outlet_dictionary(0, { notes: transform(notes, readContext(context)) });
}

function setParam(name, value) {
  params[name] = value;
  if (applied) {
    outlet(1, "bang");
  }
}

function num(value, fallback) {
  return typeof value === "number" && isFinite(value) ? value : fallback;
}

// The time selection (else the clip's first bar), the grid, the clip's scale
// and the seed Live's dice button changes
function readContext(ctx) {
  var clip = ctx.clip || {};
  var start = num(clip.time_selection_start, num(ctx.time_selection_start, 0));
  var end = num(clip.time_selection_end, num(ctx.time_selection_end, start + 4));
  var scale = ctx.scale || {};
  var grid = ctx.grid || {};
  return {
    start: start,
    end: Math.max(end, start + 0.25),
    grid: num(grid.interval, 0.25),
    root: num(scale.root_note, 0),
    intervals: Array.isArray(scale.scale_intervals) ? scale.scale_intervals : [0, 2, 4, 5, 7, 9, 11],
    seed: num(ctx.seed, 1)
  };
}

// mulberry32: the same seed gives the same notes
function rng(seed) {
  var a = Math.floor(Math.abs(seed)) >>> 0;
  return function () {
    a = (a + 0x6d2b79f5) >>> 0;
    var t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function choice(random, items) {
  return items[Math.floor(random() * items.length) % items.length];
}

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function round6(value) {
  return Math.round(value * 1e6) / 1e6;
}

function note(pitch, start, duration, velocity) {
  return {
    pitch: clamp(Math.round(pitch), 0, 127),
    start_time: round6(start),
    duration: round6(Math.max(duration, 0.01)),
    velocity: clamp(Math.round(velocity), 1, 127),
    mute: 0,
    probability: 1,
    velocity_deviation: 0,
    release_velocity: 64
  };
}

// The makam's pitches between low and high (MIDI), and its karar's pitch class
function makamPitches(name, karar, low, high) {
  var intervals = MAKAMS[name].intervals;
  var pitches = [];
  for (var p = low; p <= high; p++) {
    if (intervals.indexOf((((p - karar) % 12) + 12) % 12) >= 0) {
      pitches.push(p);
    }
  }
  return pitches;
}
