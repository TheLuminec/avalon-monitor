"""Drawn boxes in gallery sets (owner request: the teacher model misses objects; add them by hand in the full-size
view, for training and for the real test set). An operator POSTs a box {file, label, bbox}; the hub clamps it to the
image, gives it its own id ("u" + 8 hex) and stores it in gallery_added_boxes keyed by (project, slug, file, box_id).
Drawn boxes outlive re-pushes like verdicts, readers only see those whose file is in the current index, and the agent
export carries them next to the verdicts ("added").
"""
import json
import math
import re
import subprocess
import sys

from avalon_monitor import core
from conftest import WRITE
from test_gallery import ROOT, STATIC, item, make_zip, needs_node, proj, push
from test_gallery_verdicts import SLUG, boxed, live_hub, setup_set  # noqa: F401  (live_hub: a module fixture)

ID_RE = re.compile(r"^u[0-9a-f]{8}$")


def added_url(p, box_id=None):
    return f"/api/v1/projects/{p}/galleries/{SLUG}/added" + (f"/{box_id}" if box_id else "")


def add(c, p, file="a.png", label="cup", bbox=(20, 30, 100, 50)):
    return c.post(added_url(p), headers=WRITE, json={"file": file, "label": label, "bbox": list(bbox) if isinstance(bbox, tuple) else bbox})


def stored(p):
    return {a["box_id"]: (a["file"], a["label"], a["bbox"]) for a in core.db.added_list(p, SLUG)}


# ------------------------------------------------------------------ routes
def test_add_relabel_move_delete(agent, tunnel):
    """Gallery drawn boxes: an operator adds a box (hub-made id "u" + 8 hex, by = the operator), GET lists it;
    PATCH relabels it (a new class is fine), PATCH moves it, DELETE removes it, each shown by GET."""
    p, _ = setup_set(agent)
    r = add(tunnel, p, label="saucer", bbox=[20, 30, 100, 50])
    assert r.status_code == 200, r.text
    d = r.json()
    bid = d["box_id"]
    assert ID_RE.match(bid) and (d["file"], d["label"], d["bbox"], d["by"]) == ("a.png", "saucer", [20, 30, 100, 50], "owner@example.test")
    assert isinstance(d["at"], float)
    g = tunnel.get(added_url(p)).json()["added"]
    assert [(a["file"], a["box_id"], a["label"], a["bbox"], a["by"]) for a in g] == [("a.png", bid, "saucer", [20, 30, 100, 50], "owner@example.test")]
    assert set(g[0]) == {"file", "box_id", "label", "bbox", "at", "by"}
    assert add(tunnel, p, file="sub/c.png").json()["box_id"] != bid   # a second box: its own id

    r = tunnel.patch(added_url(p, bid), headers=WRITE, json={"label": "teapot (new class)"})
    assert r.status_code == 200, r.text
    assert (r.json()["label"], r.json()["bbox"]) == ("teapot (new class)", [20, 30, 100, 50])
    r = tunnel.patch(added_url(p, bid), headers=WRITE, json={"bbox": [5, 6, 70, 80]})
    assert r.status_code == 200 and (r.json()["label"], r.json()["bbox"]) == ("teapot (new class)", [5, 6, 70, 80])
    assert stored(p)[bid] == ("a.png", "teapot (new class)", [5, 6, 70, 80])

    assert tunnel.delete(added_url(p, bid), headers=WRITE).status_code == 200
    assert bid not in stored(p) and [a["file"] for a in tunnel.get(added_url(p)).json()["added"]] == ["sub/c.png"]
    assert tunnel.delete(added_url(p, bid), headers=WRITE).status_code == 404   # already gone


def test_viewer_cannot_write(agent, client, tunnel):
    """Gallery drawn boxes: a trusted-network viewer cannot POST, PATCH or DELETE a drawn box (403); nothing changes."""
    p, _ = setup_set(agent)
    bid = add(tunnel, p).json()["box_id"]
    before = stored(p)
    assert add(client, p, label="other").status_code == 403
    assert client.patch(added_url(p, bid), headers=WRITE, json={"label": "x"}).status_code == 403
    assert client.delete(added_url(p, bid), headers=WRITE).status_code == 403
    assert stored(p) == before


def test_validation_and_clamping(agent, tunnel):
    """Gallery drawn boxes: an unknown file (or set, or box id) is 404; a label that isn't a non-empty string of at
    most 40 characters is 422, so is a bbox that isn't 4 finite numbers; a bbox reaching outside the image is clamped
    to it (512 x 384); one under 2 px either way once clamped is 422. PATCH checks the same."""
    p, _ = setup_set(agent)
    assert add(tunnel, p, file="nope.png").status_code == 404
    assert tunnel.post(f"/api/v1/projects/{p}/galleries/nope/added", headers=WRITE,
                       json={"file": "a.png", "label": "cup", "bbox": [1, 1, 10, 10]}).status_code == 404
    for bad in ("", "   ", "x" * 41, 7, None, ["cup"], "cup\nmug"):
        assert add(tunnel, p, label=bad).status_code == 422, bad
    assert tunnel.post(added_url(p), headers=WRITE, json={"file": "a.png", "bbox": [1, 1, 10, 10]}).status_code == 422
    assert add(tunnel, p, label="x" * 40).status_code == 200   # exactly 40 is fine
    for bad in ([1, 2, 3], [1, 2, 3, "4"], [1, 2, 3, True], "1,2,3,4", None):
        assert add(tunnel, p, bbox=bad).status_code == 422, bad
    for bad in ([1, 2, math.nan, 4], [1, 2, math.inf, 4]):   # sent raw: httpx's json= refuses NaN, a client need not
        r = tunnel.post(added_url(p), headers={**WRITE, "Content-Type": "application/json"},
                        content=json.dumps({"file": "a.png", "label": "cup", "bbox": bad}))
        assert r.status_code == 422, bad
    r = add(tunnel, p, bbox=[-10, 300, 600, 200])
    assert r.status_code == 200 and r.json()["bbox"] == [0, 300, 512, 84]
    assert add(tunnel, p, bbox=[510, 10, 50, 50]).json()["bbox"] == [510, 10, 2, 50]   # 2 px left: still a box
    for small in ([511, 10, 50, 50], [10, 383.5, 50, 50], [600, 600, 10, 10], [10, 10, -40, 40], [10, 10, 1.5, 40]):
        r = add(tunnel, p, bbox=small)
        assert r.status_code == 422 and "2 x 2" in r.json()["detail"], small
    bid = add(tunnel, p).json()["box_id"]
    assert tunnel.patch(added_url(p, bid), headers=WRITE, json={"label": ""}).status_code == 422
    assert tunnel.patch(added_url(p, bid), headers=WRITE, json={"bbox": [511, 0, 9, 9]}).status_code == 422
    assert tunnel.patch(added_url(p, bid), headers=WRITE, json={}).status_code == 422
    assert tunnel.patch(added_url(p, bid), headers=WRITE, json={"bbox": [400, -5, 500, 50]}).json()["bbox"] == [400, 0, 112, 45]
    assert tunnel.patch(added_url(p, "u00000000"), headers=WRITE, json={"label": "x"}).status_code == 404
    assert tunnel.patch(added_url(p, "bad id!"), headers=WRITE, json={"label": "x"}).status_code == 404


def test_repush_keeps_and_export_drops_orphans(agent, tunnel):
    """Gallery drawn boxes: a re-push keeps the drawn boxes of files that survive; one on a file the push dropped is
    left out of GET and the agent export, stays in the DB, can't be changed (404), and comes back with its file;
    deleting the set removes them all (a new push of it starts with none)."""
    p, _ = setup_set(agent)
    keep = add(tunnel, p, file="a.png").json()["box_id"]
    orphan = add(tunnel, p, file="sub/c.png", label="mug").json()["box_id"]
    assert push(agent, p, make_zip(p, items=[boxed("a.png", "b1")])).status_code == 200   # sub/c.png gone
    assert [a["box_id"] for a in tunnel.get(added_url(p)).json()["added"]] == [keep]
    exp = agent.get(f"/api/v1/agent/projects/{p}/galleries/{SLUG}/verdicts").json()
    assert [a["box_id"] for a in exp["added"]] == [keep]
    assert set(stored(p)) == {keep, orphan}   # orphan kept
    assert tunnel.patch(added_url(p, orphan), headers=WRITE, json={"label": "x"}).status_code == 404
    assert tunnel.delete(added_url(p, orphan), headers=WRITE).status_code == 404
    assert push(agent, p, make_zip(p, items=[boxed("a.png", "b1"), boxed("sub/c.png", "b1")])).status_code == 200
    assert {a["box_id"] for a in tunnel.get(added_url(p)).json()["added"]} == {keep, orphan}

    assert tunnel.delete(f"/api/v1/projects/{p}/galleries/{SLUG}", headers=WRITE).status_code == 200
    assert stored(p) == {}
    assert push(agent, p, make_zip(p, items=[boxed("a.png", "b1")])).status_code == 200
    assert tunnel.get(added_url(p)).json()["added"] == []


def test_agent_export_has_added(agent, tunnel):
    """Gallery drawn boxes: the agent export lists them under "added" next to "verdicts": file, box_id, label, bbox,
    ISO time and who drew it."""
    p, vurl = setup_set(agent)
    assert tunnel.put(vurl, headers=WRITE, json={"file": "a.png", "box_id": "b1", "verdict": "good"}).status_code == 200
    bid = add(tunnel, p, file="sub/c.png", label="saucer", bbox=[1, 2, 30, 40]).json()["box_id"]
    d = agent.get(f"/api/v1/agent/projects/{p}/galleries/{SLUG}/verdicts").json()
    assert [(v["file"], v["box_id"]) for v in d["verdicts"]] == [("a.png", "b1")]
    assert [(a["file"], a["box_id"], a["label"], a["bbox"], a["by"]) for a in d["added"]] == \
        [("sub/c.png", bid, "saucer", [1, 2, 30, 40], "owner@example.test")]
    assert set(d["added"][0]) == {"file", "box_id", "label", "bbox", "at", "by"}
    assert d["added"][0]["at"].endswith("Z") and "T" in d["added"][0]["at"]


def test_tool_writes_added(tmp_path, agent, tunnel, host, live_hub):  # noqa: F811
    """Gallery drawn boxes: tools/gallery_verdicts.py writes the export's "added" list through unchanged, and says
    how many drawn boxes it wrote."""
    p, _ = setup_set(agent)
    bid = add(tunnel, p, label="mug", bbox=[10, 10, 50, 60]).json()["box_id"]
    conf = tmp_path / "agent.conf"
    conf.write_text(f"AVM_SERVER_URL={live_hub}\nAVM_TOKEN={host[1]}\n")
    out = tmp_path / "v.json"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "gallery_verdicts.py"), p, SLUG, "--out", str(out), "--config", str(conf)],
                       capture_output=True, text=True, timeout=60, env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    assert "1 drawn boxes" in r.stderr
    d = json.loads(out.read_text())
    assert [(a["file"], a["box_id"], a["label"], a["bbox"]) for a in d["added"]] == [("a.png", bid, "mug", [10, 10, 50, 60])]
    assert d["verdicts"] == []


def test_trusted_network_get_refused(agent, client, tunnel):
    """Gallery drawn boxes: GET .../added is login-only: a trusted-network viewer gets 403 saying a login is needed,
    an Access login gets 200."""
    p, _ = setup_set(agent)
    r = client.get(added_url(p))
    assert r.status_code == 403 and "login" in r.json()["detail"]
    assert tunnel.get(added_url(p)).status_code == 200


def test_set_without_box_ids_can_have_drawn_boxes(agent, tunnel):
    """Gallery drawn boxes: an item whose teacher boxes carry no ids (or that has no boxes) still takes drawn boxes:
    drawing needs only the file to be in the index."""
    p = proj()
    assert push(agent, p, make_zip(p, items=[item("a.png", boxes=[])])).status_code == 200
    assert add(tunnel, p).status_code == 200


# ------------------------------------------------------------------ UI
@needs_node
def test_ui_draw_static():
    """Gallery drawn boxes: research.js passes node --check; the full-size view has a Draw box toggle (D key) that
    drags in image pixels through toImagePx (clamped when the drag leaves the image) and dragToBox; review clicks are
    suspended in draw mode; an inline editor (no prompt()) has the set's labels plus "other…", Save and Cancel; while
    it is open (or a drag is under way) Esc cancels the pending box only and never reaches close(); drawn boxes are
    drawn from their own list (never via drawnBoxes, so never hit-tested and never slider-filtered), POSTed
    optimistically with rollback, listed as "drawn" rows with re-label and delete, counted, filterable and applied
    live from gallery_added events (which app.js now dispatches, with gallery_verdict)."""
    subprocess.run(["node", "--check", str(STATIC / "research.js")], check=True, timeout=30)
    src = (STATIC / "research.js").read_text()
    modal = src[src.index("function galleryModal(ctx, list, start)"):src.index("export function galleryNote")]
    # draw mode, D key, toggle button
    assert '(e.key === "d" || e.key === "D")' in modal and "toggleDraw()" in modal and "Draw box" in modal
    assert "canChange() ? h(\"button\", { class: \"btn small gal-draw-btn\"" in modal   # operators only
    # Esc while editing: the pending-box branch comes first and returns before the close() branch
    key = modal[modal.index("const onKey = (e) => {"):modal.index("const row = (k, v) =>")]
    pend = key[key.index("if (edit || drag) {"):key.index("return;")]
    assert 'e.key === "Escape"' in pend and "cancelPending()" in pend and "close()" not in pend
    assert key.index("if (edit || drag) {") < key.index('if (e.key === "Escape") { e.preventDefault(); close(); }')
    assert "edit.save()" in pend
    # image pixels, clamped drags, tiny drags ignored
    assert "toImagePx(svg.getBoundingClientRect(), it.width, it.height, e.clientX, e.clientY, clamp)" in modal
    assert "dragToBox(drag.p0, imgPx(e, true), it.width, it.height)" in modal and "setPointerCapture" in modal
    # review suspended while drawing
    assert 'if (ctx.draw.on || edit) { e.preventDefault(); return; } ctx.review(e, it, fig, "good", true)' in modal
    assert 'if (ctx.draw.on || edit) { e.preventDefault(); return; } ctx.review(e, it, fig, "bad", true)' in modal
    # inline editor, not a browser prompt
    assert "prompt(" not in src and '"other…"' in modal and '"Save"' in modal and '"Cancel"' in modal
    assert "ctx.labels()" in modal and "drawLabels(index," in src
    # drawn boxes: own list, not hit-tested, not slider-filtered
    assert "hitBox(drawnBoxes(it, f.thr, vOf), p[0], p[1])" in src   # the hit-test still only sees teacher boxes
    ovl = src[src.index("export function boxOverlay("):src.index("// Full-size view of list[start]")]
    loop = ovl[ovl.index("for (const a of added) {"):]
    assert "thr" not in loop and "gal-box gal-drawn" in loop and "${DRAWN_MARK} ${a.label}" in loop and '"data-bid": a.box_id' in loop
    assert "boxOverlay(it, f.thr, colorOf, false, vOf, addedOf(it.file))" in src            # grid tiles
    assert "boxOverlay(it, thr, ctx.colorOf, true, ctx.vOf, added)" in modal                 # full-size view
    # rows, optimistic writes, counts, filter, live events
    assert 'h("td", { title: a.by ? `drawn by ${a.by}` : "drawn by hand" }, "drawn")' in modal and '"delete")' in modal
    assert "onmouseenter: () => highlight(a.box_id, true)" in modal
    assert "if (slug === f.slug) applyAdded(it, tmp, null);   // roll back" in src and "Drawn box not saved" in src
    assert '"PATCH" : "DELETE"' in src and "applyAdded(it, id, prev); }   // roll back" in src
    assert ", drawn ${nDrawn}`" in src and 'opt("drawn", "has drawn boxes")' in src
    assert 'on("gallery_added"' in src and 'm.op === "delete" ? null' in src
    app = (STATIC / "app.js").read_text()
    assert 'msg.type === "gallery_verdict" || msg.type === "gallery_added") emit(msg.type, msg)' in app
    css = (STATIC / "app.css").read_text()
    assert "svg .gal-box.gal-drawn" in css and "svg .gal-drawn-halo" in css and ".gal-full.gal-drawing" in css and ".gal-ed {" in css


@needs_node
def test_ui_draw_helpers():
    """Gallery drawn boxes: the pure helpers in node: dragToBox clamps either-way-round drags to the image and drops
    tiny ones; toImagePx clamps a point outside the image when asked; drawn boxes (box_id, no id) are never hit;
    drawLabels lists the set's labels then new drawn ones; the "drawn" review filter; boxOverlay draws every drawn
    box with ✎ whatever the threshold, beside the teacher boxes that pass it."""
    js = """
import { dragToBox, toImagePx, hitBox, drawLabels, filterItems, boxOverlay } from %s;
globalThis.document = {   // just enough DOM for s(): SVG elements with attributes and text
  createElementNS: (ns, t) => ({ tag: t, attrs: {}, kids: [], setAttribute(k, v) { this.attrs[k] = String(v); },
    append(...c) { this.kids.push(...c); } }),
  createTextNode: (d) => ({ nodeType: 3, text: d }) };
const drawn = {file: "a", box_id: "u1234abcd", label: "mug", bbox: [40, 40, 20, 20]};
const items = [{file: "a", boxes: [{id: "t", label: "cup", score: 0.3, bbox: [0, 0, 100, 100]}]}, {file: "b", boxes: []}];
const addedOf = (f) => (f === "a" ? [drawn] : []);
const ovl = boxOverlay({file: "a", width: 100, height: 100, boxes: items[0].boxes}, 0.9, (l) => "red", true, undefined, [drawn]);
const rects = ovl.kids.filter((k) => k.tag === "rect").map((k) => k.attrs.class + "|" + (k.attrs["data-bid"] ?? ""));
const texts = ovl.kids.filter((k) => k.tag === "text").map((k) => k.kids.map((c) => c.text).join(""));
console.log(JSON.stringify({
  fwd: dragToBox([10, 20], [50, 70], 100, 100), back: dragToBox([50, 70], [10, 20], 100, 100),
  out: dragToBox([90, 90], [130, -20], 100, 100), tinyX: dragToBox([10, 10], [13, 50], 100, 100),
  tinyY: dragToBox([10, 10], [50, 13.9], 100, 100), edge: dragToBox([10, 10], [14, 14], 100, 100),
  none: dragToBox(null, [1, 1], 100, 100), round: dragToBox([0.04, 0], [10.16, 10], 100, 100),
  inImg: toImagePx({left: 10, top: 20, width: 200, height: 100}, 100, 100, 30, 70),
  clamped: toImagePx({left: 10, top: 20, width: 200, height: 100}, 100, 100, 30, 70, true),
  clamped2: toImagePx({left: 10, top: 20, width: 200, height: 100}, 100, 100, 500, 0, true),
  hitDrawn: hitBox([drawn], 50, 50), hitBoth: hitBox([drawn, ...items[0].boxes], 50, 50)?.id,
  labels: drawLabels({labels: ["cup", "plate"]}, [drawn, {label: "cup"}, {label: ""}]), labelsNone: drawLabels(null, []),
  fDrawn: filterItems(items, {label: "", session: "", kept: "", thr: 0, review: "drawn", addedOf}).map((i) => i.file),
  fAll: filterItems(items, {label: "", session: "", kept: "", thr: 0, review: "", addedOf}).length,
  rects, texts,
}));
""" % json.dumps((STATIC / "research.js").as_uri())
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out == {
        "fwd": [10, 20, 40, 50], "back": [10, 20, 40, 50], "out": [90, 0, 10, 90], "tinyX": None, "tinyY": None,
        "edge": [10, 10, 4, 4], "none": None, "round": [0, 0, 10.1, 10],
        "inImg": None, "clamped": [0, 50], "clamped2": [100, 0],
        "hitDrawn": None, "hitBoth": "t",
        "labels": ["cup", "plate", "mug"], "labelsNone": [],
        "fDrawn": ["a"], "fAll": 2,
        "rects": ["gal-drawn-halo|", "gal-box gal-drawn|u1234abcd"],   # the teacher box (0.3 < 0.9) is filtered, the drawn one is not
        "texts": ["✎ mug"],
    }
