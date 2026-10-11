// Makam Melody (MIDI Generator): a phrase (A A' B A'') in a Turkish makam,
// its degrees rounded to semitones, arriving on the karar through the
// makam's first tetrachord (G F# Eb D in Hicaz on D). Port of
// MCP_Server/music.py melody().

params.makam = "hicaz";
params.karar = -1;  // -1: the makam's usual karar
params.octave = 3;  // of the karar: C3 = MIDI 60
params.density = 0.6;

function makam(index) { setParam("makam", MAKAM_NAMES[index] || "hicaz"); }
function karar(index) { setParam("karar", index - 1); }
function octave(value) { setParam("octave", value); }
function density(percent) { setParam("density", percent / 100); }

function makeBar(random, pool, startIndex, density, beatsPerBar) {
  var rhythm = choice(random, MELODY_RHYTHMS);
  if (density < 0.4) {
    rhythm = rhythm.map(function (r) { return r * 2; }).slice(0, Math.max(1, Math.floor(rhythm.length / 2)));
  }
  var index = startIndex;
  var events = [];
  var t = 0;
  for (var i = 0; i < rhythm.length; i++) {
    if (t >= beatsPerBar - 1e-9) {
      break;
    }
    var step = random() < 0.85 ? choice(random, [-2, -1, -1, 0, 1, 1, 2, 3, -3]) : choice(random, [-4, 4]);
    index = clamp(index + step, 0, pool.length - 1);
    events.push([t, Math.min(rhythm[i], beatsPerBar - t), index]);
    t += rhythm[i];
  }
  return { events: events, last: index };
}

function transform(notes, c) {
  var m = MAKAMS[params.makam];
  var karar = params.karar >= 0 ? params.karar : m.karar;
  var tonic = 12 * (params.octave + 2) + karar;
  var pool = makamPitches(params.makam, karar, tonic - 5, tonic + 19);
  var random = rng(c.seed);
  var beatsPerBar = 4;
  var bars = Math.max(1, Math.round((c.end - c.start) / beatsPerBar));
  var a = makeBar(random, pool, Math.floor(pool.length / 2), params.density, beatsPerBar);
  var b = makeBar(random, pool, a.last, params.density, beatsPerBar);
  var out = [];
  for (var bar = 0; bar < bars; bar++) {
    var section = bar % 4;
    var events;
    if (section === 0 || section === 3) {
      var shift = section === 0 ? 0 : choice(random, [0, 0, 1, -1]);
      events = a.events.map(function (e) { return [e[0], e[1], e[2] + shift]; });
    } else if (section === 1) {
      events = a.events.map(function (e) { return [e[0], e[1], e[2] + choice(random, [1, 2, -1])]; });
    } else {
      events = b.events;
    }
    for (var i = 0; i < events.length; i++) {
      var t = events[i][0];
      if (random() > 0.35 + params.density * 0.65 && t > 0) {
        continue;
      }
      var pitch = pool[clamp(events[i][2], 0, pool.length - 1)];
      out.push(note(pitch, c.start + bar * beatsPerBar + t, events[i][1] * 0.95, 95 + (t === 0 ? 8 : 0)));
    }
  }
  // The cadence: down the first tetrachord to the karar
  var steps = [3, 2, 1, 0];
  var tail = out.slice(-steps.length);
  steps = steps.slice(steps.length - tail.length);
  var intervals = m.intervals;
  var top = pool[pool.length - 1];
  var lastPitch = tail.length ? tail[tail.length - 1].pitch : tonic;
  var tonics = pool.filter(function (p) { return (p - karar) % 12 === 0 && p + intervals[steps[0]] <= top; });
  if (!tonics.length) {
    tonics = pool.filter(function (p) { return (p - karar) % 12 === 0; });
  }
  if (tonics.length) {
    var best = tonics.reduce(function (x, y) { return Math.abs(y - lastPitch) < Math.abs(x - lastPitch) ? y : x; });
    for (var k = 0; k < tail.length; k++) {
      tail[k].pitch = best + intervals[steps[k]];
    }
  }
  return out;
}
