"""Permanent map labels stay inside the frame and off each other."""
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "static" / "map-labels.js"


def _choose(items, width, height, pad=4):
    node = shutil.which("node") or "/exec-daemon/node"
    script = r"""
const labels = require(process.argv[1]);
const payload = JSON.parse(process.argv[2]);
const chosen = labels.choose(payload.items, payload.width, payload.height, payload.pad);
process.stdout.write(JSON.stringify(chosen.map(function (p) {
  return {dir: p.dir, box: p.box, labelBox: p.labelBox, shift: p.shift};
})));
"""
    proc = subprocess.run(
        [node, "-e", script, str(JS), json.dumps({"items": items, "width": width, "height": height, "pad": pad})],
        check=True, capture_output=True, text=True,
    )
    return json.loads(proc.stdout)


def _inside(box, width, height, pad):
    p = pad if isinstance(pad, dict) else {"l": pad, "t": pad, "r": pad, "b": pad}
    return box["l"] >= p["l"] - 0.01 and box["t"] >= p["t"] - 0.01 and box["r"] <= width - p["r"] + 0.01 and box["b"] <= height - p["b"] + 0.01


def test_edge_labels_point_into_the_map():
    width, height, pad = 390, 240, {"l": 6, "t": 6, "r": 6, "b": 20}
    items = [
        {"x": 24, "y": 120, "w": 92, "h": 18},   # west coast, little room on the left
        {"x": 360, "y": 150, "w": 110, "h": 18},  # east coast, little room on the right
        {"x": 200, "y": 18, "w": 78, "h": 18},    # north, little room above
        {"x": 180, "y": 200, "w": 96, "h": 18},   # south, little room below
    ]
    chosen = _choose(items, width, height, pad)
    assert chosen[0]["dir"] == "right"
    assert chosen[1]["dir"] == "left"
    assert chosen[2]["dir"] != "above"
    assert chosen[3]["dir"] != "below"
    for spot in chosen:
        assert _inside(spot["box"], width, height, pad)


def test_clustered_labels_do_not_cover_each_other():
    width, height = 390, 240
    # Four sites a few pixels apart, as Sydney is at country zoom on a phone.
    items = [
        {"x": 300, "y": 140, "w": 100, "h": 18},
        {"x": 306, "y": 146, "w": 88, "h": 18},
        {"x": 298, "y": 136, "w": 92, "h": 18},
        {"x": 310, "y": 150, "w": 80, "h": 18},
    ]
    chosen = _choose(items, width, height, 4)
    for spot in chosen:
        assert _inside(spot["box"], width, height, 4)
    labels = [c["labelBox"] for c in chosen]
    for i, a in enumerate(labels):
        for b in labels[i + 1:]:
            assert labels_separated(a, b)


def labels_separated(a, b):
    return a["r"] <= b["l"] or b["r"] <= a["l"] or a["b"] <= b["t"] or b["b"] <= a["t"]


def test_named_sites_fit_a_phone_map():
    from triage.geo import asset_markers
    # 390px viewport: the map sits inside page and panel padding, about 340px wide.
    width, height = 340, 520
    fit = {"l": 48, "t": 28, "r": 64, "b": 80}
    edge = {"l": 6, "t": 6, "r": 6, "b": 20}
    markers = asset_markers()
    lons = [m["lon"] for m in markers]
    lats = [m["lat"] for m in markers]
    west, east = min(lons), max(lons)
    south, north = min(lats), max(lats)
    inner_w = width - fit["l"] - fit["r"]
    inner_h = height - fit["t"] - fit["b"]
    items = []
    for marker in markers:
        text = marker["short"]
        items.append({
            "x": fit["l"] + (marker["lon"] - west) / (east - west) * inner_w,
            "y": fit["t"] + (north - marker["lat"]) / (north - south) * inner_h,
            "w": 8 * len(text) + 16,
            "h": 20,
            "name": text,
        })
    chosen = _choose(items, width, height, edge)
    by_name = {items[i]["name"]: chosen[i] for i in range(len(items))}
    for name in ("Harold Holt", "Stirling", "Osborne", "US Sydney", "Town Hall", "Kuttabul", "NSW Parl"):
        assert name in by_name, name
        assert _inside(by_name[name]["box"], width, height, edge), (name, by_name[name])
    assert by_name["Harold Holt"]["dir"] == "right"
    assert by_name["Stirling"]["dir"] == "right"
    assert by_name["Osborne"]["dir"] != "below"
    assert by_name["US Sydney"]["dir"] != "right"
    assert len(chosen) == len(items)
    boxes = [c["labelBox"] for c in chosen]
    for spot in chosen:
        assert _inside(spot["box"], width, height, edge)
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert labels_separated(a, b)

