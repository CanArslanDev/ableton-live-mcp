// Makam Snap (MIDI Transformation): every note moves to the nearest pitch of
// a Turkish makam (its degrees rounded to semitones), the lower one on a tie.
// Notes keep their ids, timing and everything else.

params.makam = "hicaz";
params.karar = -1;  // -1: the makam's usual karar

function makam(index) { setParam("makam", MAKAM_NAMES[index] || "hicaz"); }
function karar(index) { setParam("karar", index - 1); }

function transform(notes, c) {
  var m = MAKAMS[params.makam];
  var karar = params.karar >= 0 ? params.karar : m.karar;
  var allowed = makamPitches(params.makam, karar, 0, 127);
  return notes.map(function (n) {
    var best = allowed.reduce(function (x, y) {
      return Math.abs(y - n.pitch) < Math.abs(x - n.pitch) ? y : x;
    });
    var moved = Object.assign({}, n);
    moved.pitch = best;
    return moved;
  });
}
