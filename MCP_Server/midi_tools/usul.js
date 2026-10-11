// Usul (MIDI Generator): a Turkish rhythmic cycle repeated over the time
// selection; düm, tek and the light strokes (te, ke, ka) on their pads.
// Ornament fills long strokes with light hits, as players decorate the plain
// form ("Düüm" -> "Düm Te Ke").

params.usul = "aksak";
params.dum = 36;
params.tek = 38;
params.light = 37;
params.ornament = 0;

function usul(index) { setParam("usul", USUL_NAMES[index] || "aksak"); }
function dum(pitch) { setParam("dum", pitch); }
function tek(pitch) { setParam("tek", pitch); }
function light(pitch) { setParam("light", pitch); }
function ornament(percent) { setParam("ornament", percent / 100); }

function pad(stroke) {
  return stroke === "düm" ? params.dum : stroke === "tek" ? params.tek : params.light;
}

function transform(notes, c) {
  var u = USULS[params.usul];
  var unit = 4 / u.denominator;
  var random = rng(c.seed);
  var out = [];
  var position = c.start;
  while (position < c.end - 1e-9) {
    for (var i = 0; i < u.strokes.length && position < c.end - 1e-9; i++) {
      var stroke = u.strokes[i][0];
      var units = u.strokes[i][1];
      out.push(note(pad(stroke), position, unit * 0.9, 110 * STROKES[stroke]));
      for (var extra = 1; extra < units; extra++) {
        var at = position + extra * unit;
        if (params.ornament > 0 && random() < params.ornament && at < c.end - 1e-9) {
          out.push(note(params.light, at, unit * 0.9, 110 * 0.45));
        }
      }
      position += units * unit;
    }
  }
  return out;
}
