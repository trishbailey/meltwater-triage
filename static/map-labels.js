/* Permanent site labels: keep each label inside the map and off its neighbours. */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.MapLabels = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  var ICON = 22;
  var GAP = 4;

  function rect(x, y, w, h) {
    return {l: x, t: y, r: x + w, b: y + h};
  }

  function union(a, b) {
    return {
      l: Math.min(a.l, b.l), t: Math.min(a.t, b.t),
      r: Math.max(a.r, b.r), b: Math.max(a.b, b.b)
    };
  }

  function padBox(pad) {
    if (typeof pad === "number") return {l: pad, t: pad, r: pad, b: pad};
    return {l: pad.l || 0, t: pad.t || 0, r: pad.r || 0, b: pad.b || 0};
  }

  function overflow(box, width, height, pad) {
    var p = padBox(pad);
    return Math.max(0, p.l - box.l) + Math.max(0, p.t - box.t)
      + Math.max(0, box.r - (width - p.r)) + Math.max(0, box.b - (height - p.b));
  }

  function overlapArea(a, b) {
    var w = Math.min(a.r, b.r) - Math.max(a.l, b.l);
    var h = Math.min(a.b, b.b) - Math.max(a.t, b.t);
    if (w <= 0 || h <= 0) return 0;
    return w * h;
  }

  function placement(px, py, labelW, labelH, dir, shift) {
    var iconRect = rect(px - ICON / 2, py - ICON / 2, ICON, ICON);
    var labelRect;
    if (dir === "right") labelRect = rect(px + ICON / 2 + GAP, py - labelH / 2 + shift, labelW, labelH);
    else if (dir === "left") labelRect = rect(px - ICON / 2 - GAP - labelW, py - labelH / 2 + shift, labelW, labelH);
    else if (dir === "below") labelRect = rect(px - labelW / 2 + shift, py + ICON / 2 + GAP, labelW, labelH);
    else labelRect = rect(px - labelW / 2 + shift, py - ICON / 2 - GAP - labelH, labelW, labelH);
    var box = union(iconRect, labelRect);
    return {
      dir: dir,
      shift: shift,
      box: box,
      labelBox: labelRect,
      anchorX: px - box.l,
      anchorY: py - box.t,
      iconX: iconRect.l - box.l,
      iconY: iconRect.t - box.t,
      labelX: labelRect.l - box.l,
      labelY: labelRect.t - box.t,
      width: box.r - box.l,
      height: box.b - box.t
    };
  }

  // Nearest map edge first, so a label steps inland: west sites to the
  // right, east sites to the left, south sites upward, north sites downward.
  function preferredDirs(px, py, width, height) {
    var edges = [
      {dir: "right", gap: px},
      {dir: "left", gap: width - px},
      {dir: "above", gap: height - py},
      {dir: "below", gap: py}
    ];
    edges.sort(function (a, b) { return a.gap - b.gap; });
    return edges.map(function (e) { return e.dir; });
  }

  function candidates(px, py, labelW, labelH, width, height) {
    var options = preferredDirs(px, py, width, height);
    var shifts = [0, -26, 26, -52, 52, -78, 78, -104, 104, -130, 130, -156, 156, -182, 182];
    var out = [];
    for (var d = 0; d < options.length; d++) {
      for (var s = 0; s < shifts.length; s++) {
        out.push(placement(px, py, labelW, labelH, options[d], shifts[s]));
      }
    }
    return out;
  }

  // Going off the continent costs more than any inland nudge, so a west
  // label stays to the right of its icon and a south label stays above it.
  function oceanPenalty(dir, px, py, width, height) {
    var pen = 0;
    if (px < width * 0.5 && dir === "left") pen += 400;
    if (px >= width * 0.5 && dir === "right") pen += 400;
    if (py > height * 0.55 && dir === "below") pen += 400;
    if (py < height * 0.40 && dir === "above") pen += 400;
    return pen;
  }

  function choose(items, width, height, pad) {
    var placed = [];
    var ranked = items.map(function (item, index) {
      return {item: item, index: index, room: Math.min(item.x, width - item.x, item.y, height - item.y)};
    }).sort(function (a, b) { return a.room - b.room; });
    var chosen = new Array(items.length);
    ranked.forEach(function (entry) {
      var item = entry.item;
      var opts = candidates(item.x, item.y, item.w, item.h, width, height);
      var fitting = [];
      for (var f = 0; f < opts.length; f++) {
        if (overflow(opts[f].box, width, height, pad) === 0) fitting.push(opts[f]);
      }
      var options = fitting.length ? fitting : opts;
      var best = options[0];
      var bestScore = Infinity;
      for (var i = 0; i < options.length; i++) {
        var opt = options[i];
        var over = overflow(opt.box, width, height, pad);
        var hit = 0;
        var collisions = 0;
        for (var p = 0; p < placed.length; p++) {
          var area = overlapArea(opt.labelBox, placed[p]);
          if (area > 0) {
            collisions += 1;
            hit += area;
          }
        }
        var score = over * 100000 + collisions * 10000 + hit
          + oceanPenalty(opt.dir, item.x, item.y, width, height)
          + Math.abs(opt.shift);
        if (score < bestScore) {
          bestScore = score;
          best = opt;
        }
      }
      placed.push(best.labelBox);
      chosen[entry.index] = best;
    });
    return chosen;
  }

  return {choose: choose, placement: placement, overflow: overflow, overlapArea: overlapArea,
          preferredDirs: preferredDirs};
});
