"""Per-box review of gallery sets (owner request: curate Tinyseek's teacher boxes before training), and galleries
being login-only. Boxes may carry an id; an operator marks a box good/bad; verdicts live in the DB keyed by
(project, slug, file, box_id), outlive re-pushes, and every reader only sees those whose box is in the current index.
"""
import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from avalon_monitor import core
from conftest import WRITE
from test_gallery import ROOT, STATIC, item, make_zip, needs_node, proj, push


def _core_query():
    """The ?v= query research.js imports core.js with, so node loads the same module instance (as test_ui does)."""
    import re
    m = re.search(r'from "\./core\.js(\?[^"]*)?"', (STATIC / "research.js").read_text())
    return (m.group(1) or "") if m else ""

SLUG = "synthetic__cup__a"


def boxed(file, *ids, **over):
    """An item whose boxes carry these ids (nested boxes: each one inside the previous)."""
    boxes = [{"id": bid, "label": "cup", "score": 0.5 - k * 0.1, "bbox": [10 + 10 * k, 10 + 10 * k, 200 - 20 * k, 200 - 20 * k]}
             for k, bid in enumerate(ids)]
    return item(file, boxes=boxes, **over)


def setup_set(agent, items=None):
    p = proj()
    items = items if items is not None else [boxed("a.png", "b1", "b2"), boxed("sub/c.png", "b1")]
    r = push(agent, p, make_zip(p, items=items))
    assert r.status_code == 200, r.text
    return p, f"/api/v1/projects/{p}/galleries/{SLUG}/verdicts"


def put(c, url, file, box_id, verdict):
    return c.put(url, headers=WRITE, json={"file": file, "box_id": box_id, "verdict": verdict})


def stored(p):
    return {(v["file"], v["box_id"]): v["verdict"] for v in core.db.verdicts_list(p, SLUG)}


# ------------------------------------------------------------------ verdict routes
def test_set_flip_clear(agent, tunnel):
    """Gallery verdicts: an operator sets good, flips it to bad, clears it with null; GET shows each state, with the
    operator's email as `by`."""
    p, url = setup_set(agent)
    r = put(tunnel, url, "a.png", "b1", "good")
    assert r.status_code == 200, r.text
    assert r.json()["verdict"] == "good" and r.json()["by"] == "owner@example.test"
    v = tunnel.get(url).json()["verdicts"]
    assert [(x["file"], x["box_id"], x["verdict"], x["by"]) for x in v] == [("a.png", "b1", "good", "owner@example.test")]
    assert isinstance(v[0]["at"], float)
    assert put(tunnel, url, "a.png", "b1", "bad").status_code == 200
    assert [x["verdict"] for x in tunnel.get(url).json()["verdicts"]] == ["bad"]
    assert put(tunnel, url, "a.png", "b1", None).status_code == 200
    assert tunnel.get(url).json()["verdicts"] == [] and stored(p) == {}


def test_viewer_cannot_put(agent, client, tunnel):
    """Gallery verdicts: a trusted-network viewer cannot PUT a verdict (403), nothing is stored."""
    p, url = setup_set(agent)
    r = put(client, url, "a.png", "b1", "good")
    assert r.status_code == 403
    assert stored(p) == {}


def test_unknown_box_and_bad_verdict(agent, tunnel):
    """Gallery verdicts: an unknown file or box id is a 404; a verdict other than good/bad/null (or none at all) is 422."""
    p, url = setup_set(agent)
    assert put(tunnel, url, "nope.png", "b1", "good").status_code == 404
    assert put(tunnel, url, "a.png", "zz", "good").status_code == 404
    assert put(tunnel, url, "sub/c.png", "b2", "good").status_code == 404   # b2 exists, but on a.png only
    assert put(tunnel, url, "a.png", "b1", "meh").status_code == 422
    assert put(tunnel, url, "a.png", "b1", True).status_code == 422
    assert tunnel.put(url, headers=WRITE, json={"file": "a.png", "box_id": "b1"}).status_code == 422
    assert tunnel.put(f"/api/v1/projects/{p}/galleries/nope/verdicts", headers=WRITE,
                      json={"file": "a.png", "box_id": "b1", "verdict": "good"}).status_code == 404
    assert stored(p) == {}


def test_box_ids_validated_at_push(agent):
    """Gallery verdicts: a duplicate box id within one item is refused at push (422 naming it), so is a malformed id;
    the same id on two different items is fine, and boxes without ids still push."""
    p = proj()
    r = push(agent, p, make_zip(p, items=[boxed("a.png", "dup", "dup")]))
    assert r.status_code == 422 and "dup" in r.json()["detail"] and "unique" in r.json()["detail"]
    for bad in ("", "x" * 33, "has space", "a/b", 7):
        assert push(agent, p, make_zip(p, items=[boxed("a.png", bad)])).status_code == 422, bad
    assert push(agent, p, make_zip(p, items=[boxed("a.png", "b1"), boxed("c.png", "b1"), item("d.png")])).status_code == 200


def test_repush_keeps_verdicts_and_export_drops_orphans(agent, tunnel):
    """Gallery verdicts: re-pushing the set keeps the verdicts of boxes that survive; one whose box id is gone (and
    one whose file is gone) drop out of GET and the agent export but stay in the DB, and come back with the box."""
    p, url = setup_set(agent)
    for f, b, v in (("a.png", "b1", "good"), ("a.png", "b2", "bad"), ("sub/c.png", "b1", "good")):
        assert put(tunnel, url, f, b, v).status_code == 200
    assert push(agent, p, make_zip(p, items=[boxed("a.png", "b1", "b9")])).status_code == 200   # b2 and sub/c.png gone
    assert [(x["file"], x["box_id"]) for x in tunnel.get(url).json()["verdicts"]] == [("a.png", "b1")]
    exp = agent.get(f"/api/v1/agent/projects/{p}/galleries/{SLUG}/verdicts").json()
    assert [(x["file"], x["box_id"], x["verdict"]) for x in exp["verdicts"]] == [("a.png", "b1", "good")]
    assert len(stored(p)) == 3   # orphans kept
    assert put(tunnel, url, "a.png", "b2", "good").status_code == 404   # not in the current index
    assert push(agent, p, make_zip(p, items=[boxed("a.png", "b1", "b2")])).status_code == 200
    assert {(x["box_id"], x["verdict"]) for x in tunnel.get(url).json()["verdicts"]} == {("b1", "good"), ("b2", "bad")}


def test_delete_set_removes_verdicts(agent, tunnel):
    """Gallery verdicts: deleting the set deletes its verdicts (a new push of the same set starts unreviewed)."""
    p, url = setup_set(agent)
    assert put(tunnel, url, "a.png", "b1", "good").status_code == 200
    assert tunnel.delete(f"/api/v1/projects/{p}/galleries/{SLUG}", headers=WRITE).status_code == 200
    assert stored(p) == {}
    assert push(agent, p, make_zip(p, items=[boxed("a.png", "b1")])).status_code == 200
    assert tunnel.get(url).json()["verdicts"] == []


def test_agent_export(agent, client, tunnel):
    """Gallery verdicts: the agent export works with an agent token (project, set name, slug, ISO times) and refuses
    without one (401) or with a bad one; it is read-only (no PUT there)."""
    p, url = setup_set(agent)
    assert put(tunnel, url, "sub/c.png", "b1", "bad").status_code == 200
    eurl = f"/api/v1/agent/projects/{p}/galleries/{SLUG}/verdicts"
    r = agent.get(eurl)
    assert r.status_code == 200, r.text
    d = r.json()
    assert (d["project"], d["set"], d["slug"]) == (p, "synthetic/cup/a", SLUG)
    assert d["exported_at"].endswith("Z") and "T" in d["exported_at"]
    assert [(v["file"], v["box_id"], v["verdict"], v["by"]) for v in d["verdicts"]] == [("sub/c.png", "b1", "bad", "owner@example.test")]
    assert d["verdicts"][0]["at"].endswith("Z")
    assert client.get(eurl).status_code == 401
    assert client.get(eurl, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert agent.put(eurl, json={}).status_code == 405


@pytest.fixture(scope="module")
def live_hub(app_obj):
    import uvicorn
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app_obj, host="127.0.0.1", port=port, lifespan="off", log_level="error", access_log=False))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield "http://localhost:%d" % port
    server.should_exit = True
    th.join(5)


def test_tool_writes_json(tmp_path, agent, tunnel, host, live_hub):
    """Gallery verdicts: tools/gallery_verdicts.py, given the set name (slug derived like the hub), reads a temp
    agent.conf and writes the documented JSON to --out; given the slug it prints the same to stdout; no config -> 2."""
    p, url = setup_set(agent)
    assert put(tunnel, url, "a.png", "b2", "good").status_code == 200
    conf = tmp_path / "agent.conf"
    conf.write_text(f"AVM_SERVER_URL={live_hub}\nAVM_TOKEN={host[1]}\n")
    out = tmp_path / "v.json"
    tool = str(ROOT / "tools" / "gallery_verdicts.py")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    r = subprocess.run([sys.executable, tool, p, "synthetic/cup/a", "--out", str(out), "--config", str(conf)],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0, r.stderr
    d = json.loads(out.read_text())
    assert set(d) == {"project", "set", "slug", "exported_at", "verdicts"}
    assert (d["project"], d["set"], d["slug"]) == (p, "synthetic/cup/a", SLUG)
    assert [(v["file"], v["box_id"], v["verdict"]) for v in d["verdicts"]] == [("a.png", "b2", "good")]
    assert set(d["verdicts"][0]) == {"file", "box_id", "verdict", "at", "by"}
    r = subprocess.run([sys.executable, tool, p, SLUG, "--config", str(conf)], capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0 and json.loads(r.stdout)["verdicts"] == d["verdicts"]
    r = subprocess.run([sys.executable, tool, p, SLUG, "--config", str(tmp_path / "none.conf")], capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.returncode == 2 and "no agent config" in r.stderr


def test_tool_slug_matches_hub():
    """Gallery verdicts: the tool derives a set's slug exactly as the hub does, and a slug maps to itself."""
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import gallery_verdicts as gv
    finally:
        sys.path.remove(str(ROOT / "tools"))
    from avalon_monitor.routes_gallery import set_slug
    for name in ("synthetic/cup/a", "Real Cup #2", "..x/y", "_a__b", "ÄÖ/üx", "a" * 300):
        assert gv.set_slug(name) == set_slug(name)
        assert gv.set_slug(set_slug(name)) == set_slug(name)


# ------------------------------------------------------------------ UI
@needs_node
def test_ui_review_static():
    """Gallery verdicts: research.js passes node --check; it handles contextmenu (preventDefault only for operators,
    over the image), hit-tests in image pixel space from the overlay's rect, toggles via nextVerdict, rolls back a
    failed PUT, applies gallery_verdict events, and draws reviewed boxes regardless of the slider (drawnBoxes)."""
    subprocess.run(["node", "--check", str(STATIC / "research.js")], check=True, timeout=30)
    src = (STATIC / "research.js").read_text()
    assert src.count('addEventListener("contextmenu"') == 2   # grid tile and full-size view
    assert 'if (which === "bad") e.preventDefault();   // over the image only' in src
    assert src.index("if (!canChange()) {") < src.index('if (which === "bad") e.preventDefault()')   # read-only: no suppression
    assert "toImagePx(svg.getBoundingClientRect(), it.width, it.height, e.clientX, e.clientY)" in src
    assert "hitBox(drawnBoxes(it, f.thr, vOf), p[0], p[1])" in src
    assert "for (const b of drawnBoxes(item, thr, vOf))" in src   # the overlay draws reviewed boxes whatever thr is
    assert "nextVerdict(vOf(it.file, b.id), which)" in src and "apply(it, id, prev); }   // roll back" in src
    assert 'on("gallery_verdict"' in src and '"PUT")' in src and "sign in to review" in src.lower()
    assert "reviewed ${c.reviewed} / ${c.n} boxes (good ${c.good}, bad ${c.bad})" in src
    assert 'opt("unreviewed", "has unreviewed boxes"), opt("bad", "has bad boxes"), opt("done", "fully reviewed")' in src
    css = (STATIC / "app.css").read_text()
    assert ".gal-box.gal-good" in css and ".gal-v-bad" in css


@needs_node
def test_ui_review_helpers():
    """Gallery verdicts: the pure helpers behave in node: the smallest id-carrying box under a point wins, boxes
    without an id are never hit, client -> image pixels honours the letterbox, the toggle clears and flips, reviewed
    boxes are drawn below the threshold, and the review counts and filter agree."""
    js = """
import { hitBox, toImagePx, nextVerdict, drawnBoxes, reviewCounts, filterItems, vKey } from %s;
const big = {id: "big", label: "cup", score: 0.9, bbox: [0, 0, 100, 100]};
const small = {id: "small", label: "cup", score: 0.2, bbox: [40, 40, 20, 20]};
const noid = {label: "cup", score: 0.9, bbox: [45, 45, 5, 5]};
const items = [
  {file: "a", boxes: [big, small, noid]},
  {file: "b", boxes: [{id: "x", label: "cup", score: 0.1, bbox: [0, 0, 5, 5]}]},
  {file: "c", boxes: [noid]},
];
const V = new Map([[vKey("a", "small"), "bad"], [vKey("b", "x"), "good"]]);
const vOf = (f, id) => V.get(vKey(f, id)) ?? null;
const F = (review) => filterItems(items, {label: "", session: "", kept: "", thr: 0, review, vOf}).map((i) => i.file).join("");
console.log(JSON.stringify({
  inner: hitBox(items[0].boxes, 50, 50)?.id, outer: hitBox(items[0].boxes, 10, 10)?.id, none: hitBox(items[0].boxes, 150, 5),
  noid: hitBox([noid], 46, 46), onlyDrawn: hitBox(drawnBoxes(items[0], 0.5), 50, 50)?.id,
  drawnHi: drawnBoxes(items[0], 0.95, vOf).map((b) => b.id ?? "-"), drawnNoV: drawnBoxes(items[0], 0.95).length,
  px: toImagePx({left: 10, top: 20, width: 200, height: 100}, 100, 100, 110, 70),
  pxOut: toImagePx({left: 10, top: 20, width: 200, height: 100}, 100, 100, 30, 70),
  t: [nextVerdict(null, "good"), nextVerdict("good", "good"), nextVerdict("good", "bad")],
  counts: reviewCounts(items, vOf),
  all: F(""), unrev: F("unreviewed"), bad: F("bad"), done: F("done"),
}));
""" % json.dumps((STATIC / "research.js").as_uri())
    out = json.loads(subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30, check=True).stdout)
    assert out == {
        "inner": "small", "outer": "big", "none": None, "noid": None, "onlyDrawn": "big",
        "drawnHi": ["small"], "drawnNoV": 0,
        "px": [50, 50], "pxOut": None,
        "t": ["good", None, "bad"],
        "counts": {"n": 3, "reviewed": 2, "good": 1, "bad": 1},
        "all": "abc", "unrev": "a", "bad": "a", "done": "b",
    }


# ------------------------------------------------------------------ login-only
GALLERY_READS = ["/galleries", f"/galleries/{SLUG}", f"/galleries/{SLUG}/image/a.png", f"/galleries/{SLUG}/thumb/a.png",
                 f"/galleries/{SLUG}/verdicts"]


def test_trusted_network_refused(agent, client):
    """Gallery login: a trusted-network (no Access login) viewer gets 403 on the list, index, image, thumb and verdicts
    GET, saying a login is needed; /projects shows it neither the gallery-only project nor any gallery count."""
    p, _ = setup_set(agent)
    for path in GALLERY_READS:
        r = client.get(f"/api/v1/projects/{p}" + path)
        assert r.status_code == 403 and "login" in r.json()["detail"], (path, r.status_code)
    projects = client.get("/api/v1/projects").json()["projects"]
    assert p not in {x["project"] for x in projects}
    assert all(not x.get("galleries") for x in projects)


def test_access_login_allowed(agent, tunnel):
    """Gallery login: an Access identity gets 200 on the same reads and sees the project with its set count."""
    p, _ = setup_set(agent)
    for path in GALLERY_READS:
        assert tunnel.get(f"/api/v1/projects/{p}" + path).status_code == 200, path
    projects = {x["project"]: x for x in tunnel.get("/api/v1/projects").json()["projects"]}
    assert projects[p]["galleries"] == 1


def test_broadcast_login_only():
    """Gallery login: a login-only broadcast (gallery / gallery_verdict events name sets, files and reviewers) reaches
    only sockets opened with an Access login; other events reach every socket."""
    class WS:
        def __init__(self, login):
            self.state = SimpleNamespace(avm_login=login) if login is not None else SimpleNamespace()
            self.got = []

        async def send_text(self, t):
            self.got.append(json.loads(t)["type"])

    hub = core.LiveHub()
    a, t, bare = WS(True), WS(False), WS(None)
    hub.clients.update({a, t, bare})
    asyncio.run(hub.broadcast({"type": "gallery_verdict"}, login_only=True))
    asyncio.run(hub.broadcast({"type": "host"}))
    assert a.got == ["gallery_verdict", "host"] and t.got == ["host"] and bare.got == ["host"]
    src = (ROOT / "server" / "avalon_monitor" / "routes_gallery.py").read_text()
    assert src.count("hub.broadcast(") == src.count("login_only=True)") == 3


@needs_node
def test_ui_login_and_empty_notes():
    """Gallery login: the research page always has a Gallery section: signed out it says galleries need a login (with
    the public address) and fetches nothing; signed in with no sets it says none were pushed."""
    src = (STATIC / "research.js").read_text()
    assert "Image galleries need a login: open this page through " in src and "state.config?.public_url" in src
    assert "No image sets pushed yet" in src
    assert "const galleries = signedIn() && pinfo.galleries ? api(" in src
    assert src.count("galleryCard(project, sets)") == 2
    js = """
import { galleryNote } from %s;
import { state } from %s;
const txt = (el) => el ? (el.textContent ?? "") : null;
globalThis.document = {   // just enough DOM for h(): elements and text nodes
  createElement: (t) => ({ nodeType: 1, tag: t, kids: [], attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
    append(...c) { this.kids.push(...c); }, get textContent() { return this.kids.map((k) => k.textContent).join(""); } }),
  createTextNode: (d) => ({ nodeType: 3, textContent: d }) };
state.config = { public_url: "https://monitor.example.test", user: { method: "trusted-network" } };
const out = txt(galleryNote("p", []));
state.config.user.method = "access";
console.log(JSON.stringify({ out, empty: txt(galleryNote("p", [])), some: galleryNote("p", [{slug: "s"}]) }));
""" % (json.dumps((STATIC / "research.js").as_uri()), json.dumps((STATIC / "core.js").as_uri() + _core_query()))
    r = subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert "need a login" in out["out"] and "monitor.example.test" in out["out"]
    assert "No image sets pushed yet" in out["empty"] and out["some"] is None


def test_ui_box_list_review_buttons():
    """Gallery verdicts (static, source-text; owner report 11-09): a box covered by a smaller one had no clickable
    spot, because an image click picks the smallest box under the pointer. The full-size view's box list now gives
    operators per-row ✓ good / ✗ bad buttons that set that box's verdict by id (toggling via nextVerdict, through the
    same optimistic setVerdict), read-only viewers still see plain text, and hovering a row highlights its box via
    data-bid on the overlay rect."""
    src = (STATIC / "research.js").read_text()
    assert "function reviewCell(it, b, v)" in src and "reviewCell(it, b, v)" in src
    assert "ctx.setVerdict(it, id, nextVerdict(v, which))" in src
    assert "review, setVerdict, listeners: new Set()" in src           # the modal reaches the same PUT/rollback path
    cell = src[src.index("function reviewCell(it, b, v)"):src.index("function highlight(id, on)")]
    assert cell.index("if (!canChange())") < cell.index('h("button"')      # read-only viewers get no buttons
    assert '"data-bid": typeof b.id === "string" ? b.id : null' in src
    assert 'el.classList.toggle("gal-hl", on && el.getAttribute("data-bid") === id)' in src
    assert "onmouseenter: id ? () => highlight(id, true) : null" in src
    css = (STATIC / "app.css").read_text()
    assert "svg .gal-box.gal-hl" in css and ".gal-vbtn-good.active" in css and ".gal-vbtn-bad.active" in css
