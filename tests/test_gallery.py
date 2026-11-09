"""Image galleries (for Tinyseek): a project pushes an image set as a zip (tools/gallery_push.py) and the
research page browses it with boxes overlaid. The hub cannot read project directories, so it stores and serves only
what was pushed; the upload is untrusted, so entry names, sizes and the index are all checked before anything lands.
"""
import io
import json
import shutil
import stat
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

from avalon_monitor import routes_gallery as rg
from avalon_monitor.config import settings
from conftest import WRITE, report_policy

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "server" / "avalon_monitor" / "static"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 4
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")


def proj():
    return "gt-" + uuid.uuid4().hex[:10]


def item(file, **over):
    return {"file": file, "width": 512, "height": 384, "text": "a cup on a table", "seed": 7, "session": "s1",
            "tags": {"kept": True, "reason": None, "luma": 64.0},
            "boxes": [{"label": "cup", "score": 0.41, "bbox": [10, 20, 100, 80]}], **over}


def make_zip(project, set_name="synthetic/cup/a", items=None, files=None, index=None, entries=(), thumbs=False):
    """A gallery zip: index.json + images/<file> for every item (or `files`), plus raw `entries` (name, bytes|ZipInfo)."""
    items = [item("000001.png"), item("sub/000002.png", session="s2", tags={"kept": False})] if items is None else items
    doc = index if index is not None else {"project": project, "set": set_name, "kind": "synthetic", "labels": ["cup"], "items": items}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("index.json", json.dumps(doc))
        for f in (files if files is not None else [it["file"] for it in items]):
            z.writestr("images/" + f, PNG + f.encode())
            if thumbs:
                z.writestr("thumbs/" + f + ".jpg", b"thumb:" + f.encode())
        for name, data in entries:   # name: a str or a ZipInfo (e.g. a symlink entry)
            z.writestr(name, data)
    return buf.getvalue()


def push(agent, project, body):
    return agent.post(f"/api/v1/projects/{project}/gallery", content=body, headers={"Content-Type": "application/zip"})


def files_under(root: Path) -> set:
    return {str(p.relative_to(root)) for p in root.rglob("*")} if root.exists() else set()


def test_push_list_and_serve(agent, tunnel):
    """Gallery: a pushed set is listed (slug, name, kind, items, labels, host, bytes), its index serves without the
    pusher's image_root, its images serve with an image type, private caching and nosniff, and a thumbnail falls back
    to the full image when the set has none."""
    p = proj()
    idx = {"project": p, "set": "synthetic/cup/a", "kind": "synthetic", "image_root": "/home/someone/data", "labels": ["cup"],
           "items": [item("000001.png"), item("sub/000002.png")]}
    r = push(agent, p, make_zip(p, index=idx, files=["000001.png", "sub/000002.png"]))
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "synthetic__cup__a" and r.json()["items"] == 2

    sets = tunnel.get(f"/api/v1/projects/{p}/galleries").json()["galleries"]
    assert [(s["slug"], s["set"], s["kind"], s["items"], s["labels"], s["host"]) for s in sets] == \
        [("synthetic__cup__a", "synthetic/cup/a", "synthetic", 2, ["cup"], agent.host_row.name)]
    assert sets[0]["bytes"] > 0 and sets[0]["pushed_at"] > 0 and sets[0]["thumbs"] is False

    doc = tunnel.get(f"/api/v1/projects/{p}/galleries/synthetic__cup__a").json()
    assert "image_root" not in doc and doc["set"] == "synthetic/cup/a"
    assert doc["items"][0]["boxes"] == [{"label": "cup", "score": 0.41, "bbox": [10, 20, 100, 80]}]

    img = tunnel.get(f"/api/v1/projects/{p}/galleries/synthetic__cup__a/image/sub/000002.png")
    assert img.status_code == 200 and img.content == PNG + b"sub/000002.png"
    assert img.headers["content-type"] == "image/png"
    assert img.headers["cache-control"] == "private, max-age=3600" and img.headers["x-content-type-options"] == "nosniff"
    th = tunnel.get(f"/api/v1/projects/{p}/galleries/synthetic__cup__a/thumb/000001.png")
    assert th.status_code == 200 and th.content == PNG + b"000001.png"

    projects = {x["project"]: x for x in tunnel.get("/api/v1/projects").json()["projects"]}
    assert projects[p]["galleries"] == 1 and projects[p]["pushes"] == 0


def test_thumbnails_served(agent, tunnel):
    """Gallery: a set pushed with thumbnails serves thumbs/<file>.jpg as image/jpeg on the thumb route."""
    p = proj()
    assert push(agent, p, make_zip(p, thumbs=True)).status_code == 200
    th = tunnel.get(f"/api/v1/projects/{p}/galleries/synthetic__cup__a/thumb/000001.png")
    assert th.status_code == 200 and th.content == b"thumb:000001.png" and th.headers["content-type"] == "image/jpeg"


def test_repush_replaces(agent, tunnel):
    """Gallery: re-pushing the same set replaces it whole: the new items only, the old images gone, one set listed,
    and no temp directories left behind."""
    p = proj()
    assert push(agent, p, make_zip(p)).status_code == 200
    r = push(agent, p, make_zip(p, items=[item("new.jpg", width=64, height=64)]))
    assert r.status_code == 200, r.text
    sets = tunnel.get(f"/api/v1/projects/{p}/galleries").json()["galleries"]
    assert len(sets) == 1 and sets[0]["items"] == 1
    assert [i["file"] for i in tunnel.get(f"/api/v1/projects/{p}/galleries/{sets[0]['slug']}").json()["items"]] == ["new.jpg"]
    assert tunnel.get(f"/api/v1/projects/{p}/galleries/{sets[0]['slug']}/image/000001.png").status_code == 404
    assert not [n for n in (rg.gallery_root() / p).iterdir() if n.name.startswith(".")]


def _symlink(name, target=b"/etc/passwd"):
    zi = zipfile.ZipInfo(name)
    zi.create_system = 3
    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
    return (zi, target)


@pytest.mark.parametrize("entry", [
    ("../x.jpg", PNG), ("/abs.jpg", PNG), ("images/../../x.jpg", PNG), ("images/a/../../../x.jpg", PNG),
    ("images\\..\\x.jpg", PNG), _symlink("images/link.jpg"), _symlink("images/sub"),
], ids=["dotdot", "absolute", "images-dotdot", "nested-dotdot", "backslash", "symlink", "symlink-dir"])
def test_unsafe_entries_refused(agent, tunnel, entry):
    """Gallery: a zip with a "..", an absolute path, a backslash path or a symlink is refused with a 4xx, and nothing
    is written anywhere (not outside the set, not the set itself)."""
    p = proj()
    root = rg.gallery_root().parent
    before = files_under(root)
    r = push(agent, p, make_zip(p, entries=[entry]))
    assert 400 <= r.status_code < 500, r.text
    assert tunnel.get(f"/api/v1/projects/{p}/galleries").json()["galleries"] == []
    new = {f for f in files_under(root) - before if not f.endswith(("monitor.db-wal", "monitor.db-shm"))}
    assert new <= {"galleries", f"galleries/{p}", "galleries/.incoming"}, new
    assert not list(root.rglob("x.jpg")) and not list(root.rglob("abs.jpg"))


@pytest.mark.parametrize("entry", [("images/notes.txt", b"hi"), ("readme.txt", b"hi"), ("images/a.svg", b"<svg/>"), ("thumbs/a.html", b"<b>")])
def test_non_image_refused(agent, tunnel, entry):
    """Gallery: an entry that is not index.json or an image (.jpg .jpeg .png .webp) under images/ or thumbs/ is a 422."""
    p = proj()
    r = push(agent, p, make_zip(p, entries=[entry]))
    assert r.status_code == 422, r.text
    assert tunnel.get(f"/api/v1/projects/{p}/galleries").json()["galleries"] == []


def test_caps(agent, monkeypatch):
    """Gallery: an upload over AVM_GALLERY_MAX_BYTES is a 413, so is a small zip that unpacks past it (zip bomb) and
    an index with too many items; the global max_payload_bytes does not apply to gallery pushes."""
    p = proj()
    monkeypatch.setattr(settings, "max_payload_bytes", 100)
    assert push(agent, p, make_zip(p)).status_code == 200   # ~1 KB, far over max_payload_bytes
    monkeypatch.setattr(settings, "gallery_max_bytes", 4000)
    big = make_zip(p, entries=[("images/big.png", bytes(range(256)) * 40)])   # stored size > cap
    assert len(big) > 4000
    assert push(agent, p, big).status_code == 413
    bomb = io.BytesIO()
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("index.json", json.dumps({"project": p, "set": "b", "items": []}))
        z.writestr("images/zeros.png", b"\0" * 200_000)
    assert len(bomb.getvalue()) < 4000
    r = push(agent, p, bomb.getvalue())
    assert r.status_code == 413 and "AVM_GALLERY_MAX_BYTES" in r.text
    monkeypatch.setattr(settings, "gallery_max_bytes", 300 * 1024 * 1024)
    monkeypatch.setattr(rg, "MAX_ITEMS", 1)
    assert push(agent, p, make_zip(p, set_name="two")).status_code == 413


def test_index_must_match_files(agent):
    """Gallery: an index naming a file that is not in images/ is a 422 that names it; so are a project that does not
    match the URL, a bad bbox and non-scalar tags."""
    p = proj()
    r = push(agent, p, make_zip(p, items=[item("a.png"), item("missing.png")], files=["a.png"]))
    assert r.status_code == 422 and "missing.png" in r.json()["detail"]
    assert push(agent, p, make_zip(p, index={"project": "other", "set": "x", "items": []})).status_code == 422
    assert push(agent, p, make_zip(p, items=[item("a.png", boxes=[{"label": "cup", "score": 0.5, "bbox": [1, 2, 3]}])])).status_code == 422
    assert push(agent, p, make_zip(p, items=[item("a.png", tags={"k": [1, 2]})])).status_code == 422
    assert push(agent, p, make_zip(p, items=[item("a.png", width=0)])).status_code == 422


def test_restricted_host_refused(agent, tunnel):
    """Gallery: a data-use-restricted host may not push a gallery (raw images): 403, nothing stored."""
    report_policy(agent, {"max_mode": "plan", "actions": True, "jobs": True, "transcript": "off", "snippets": False})
    p = proj()
    r = push(agent, p, make_zip(p))
    assert r.status_code == 403 and "data-use" in r.json()["detail"]
    assert tunnel.get(f"/api/v1/projects/{p}/galleries").json()["galleries"] == []


def test_delete_needs_operator(agent, client, tunnel):
    """Gallery: a trusted-network viewer cannot DELETE a set (read-only, 403); an Access operator can, and it is gone."""
    p = proj()
    assert push(agent, p, make_zip(p)).status_code == 200
    url = f"/api/v1/projects/{p}/galleries/synthetic__cup__a"
    assert client.delete(url, headers=WRITE).status_code == 403
    assert tunnel.get(url).status_code == 200
    assert tunnel.delete(url, headers=WRITE).status_code == 200
    assert tunnel.get(url).status_code == 404
    assert tunnel.get(f"/api/v1/projects/{p}/galleries").json()["galleries"] == []


@pytest.mark.parametrize("path", [
    "image/..%2Findex.json", "image/..%2Fmeta.json", "image/..%2F..%2F..%2Fmonitor.db", "image/%2Fetc%2Fpasswd",
    "thumb/..%2Findex.json", "thumb/..%2F..%2F..%2Fmonitor.db", "image/nope.png", "image/sub",
])
def test_image_route_traversal(agent, tunnel, path):
    """Gallery: the image and thumb routes resolve the path and refuse anything outside the set's images/ (or thumbs/)
    with 404: "..", absolute paths, directories, missing files."""
    p = proj()
    assert push(agent, p, make_zip(p)).status_code == 200
    r = tunnel.get(f"/api/v1/projects/{p}/galleries/synthetic__cup__a/{path}")
    assert r.status_code == 404, (r.status_code, r.content[:80])


def test_bad_slug_404(tunnel):
    """Gallery: a slug that could name a temp or parent directory is a 404."""
    p = proj()
    for slug in ("..", ".incoming", ".tmp-x"):
        assert tunnel.get(f"/api/v1/projects/{p}/galleries/{slug}").status_code in (404, 405)


def test_pusher_dry_run(tmp_path, agent, tunnel):
    """Gallery: tools/gallery_push.py --dry-run builds a zip (index without image_root, images/<file>) from a temp
    agent.conf, uploads nothing, and that zip is one the hub accepts."""
    p = proj()
    imgs = tmp_path / "imgs"
    (imgs / "sub").mkdir(parents=True)
    (imgs / "a.png").write_bytes(PNG)
    (imgs / "sub" / "b.jpg").write_bytes(PNG)
    idx = {"project": p, "set": "real/cup", "kind": "real", "image_root": str(imgs), "labels": ["cup"],
           "items": [item("a.png"), item("sub/b.jpg")]}
    (tmp_path / "index.json").write_text(json.dumps(idx))
    conf = tmp_path / "agent.conf"
    conf.write_text("AVM_SERVER_URL=http://127.0.0.1:9\nAVM_TOKEN=nope\n")   # never contacted on a dry run
    out = tmp_path / "set.zip"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "gallery_push.py"), str(tmp_path / "index.json"),
                        "--dry-run", "--config", str(conf), "--out", str(out)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "2 items" in r.stdout and "dry run" in r.stdout
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        sent = json.loads(z.read("index.json"))
    assert {"index.json", "images/a.png", "images/sub/b.jpg"} <= names
    assert "image_root" not in sent
    assert push(agent, p, out.read_bytes()).status_code == 200
    assert tunnel.get(f"/api/v1/projects/{p}/galleries/real__cup/image/sub/b.jpg").content == PNG


def test_pusher_refuses_escape(tmp_path):
    """Gallery: the pusher refuses an index whose file climbs out of image_root."""
    imgs = tmp_path / "imgs"
    imgs.mkdir()
    (tmp_path / "secret.png").write_bytes(PNG)
    idx = {"project": "x", "set": "s", "image_root": str(imgs), "items": [item("../secret.png")]}
    (tmp_path / "i.json").write_text(json.dumps(idx))
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "gallery_push.py"), str(tmp_path / "i.json"), "--dry-run"],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0 and "items[0].file" in r.stderr


@needs_node
def test_ui_js():
    """Gallery: research.js passes node --check, renders a box overlay (SVG in image pixel space) over each tile and a
    0-1 score slider, lazy-renders in chunks, and its filters behave: label at/above the threshold, kept/dropped, session."""
    subprocess.run(["node", "--check", str(STATIC / "research.js")], check=True, timeout=30)
    src = (STATIC / "research.js").read_text()
    assert 'class: "gal-ovl", viewBox: `0 0 ${W} ${H}`' in src
    assert 'type: "range", min: "0", max: "1"' in src
    assert "boxOverlay(it, f.thr, colorOf, false, vOf)" in src and "IntersectionObserver" in src and "CHUNK = 120" in src
    assert 'e.key === "Escape"' in src and 'e.key === "ArrowLeft"' in src and "guard(h(\"button\"" in src
    js = """
import { filterItems, boxesAbove, galleryUrl, NO_BOX } from %s;
const items = [
  {session: "s1", tags: {kept: true}, boxes: [{label: "cup", score: 0.4}]},
  {session: "s2", tags: {kept: false}, boxes: [{label: "cup", score: 0.9}, {label: "mug", score: 0.2}]},
  {session: null, tags: {}, boxes: []},
];
const F = (o) => filterItems(items, {label: "", session: "", kept: "", thr: 0, ...o}).length;
console.log(JSON.stringify({
  all: F({}), cup5: F({label: "cup", thr: 0.5}), mug: F({label: "mug"}), mug3: F({label: "mug", thr: 0.3}),
  none5: F({label: NO_BOX, thr: 0.5}), kept: F({kept: "kept"}), dropped: F({kept: "dropped"}), s2: F({session: "s2"}),
  above: boxesAbove(items[1], 0.5).length,
  url: galleryUrl("p", "a__b", "thumb", "sub dir/x#1.png"),
}));
""" % json.dumps((STATIC / "research.js").as_uri())
    out = json.loads(subprocess.run(["node", "--input-type=module", "-e", js], capture_output=True, text=True, timeout=30, check=True).stdout)
    assert out == {"all": 3, "cup5": 1, "mug": 1, "mug3": 0, "none5": 2, "kept": 1, "dropped": 1, "s2": 1, "above": 1,
                   "url": "/api/v1/projects/p/galleries/a__b/thumb/sub%20dir/x%231.png"}


def test_readme_documents_galleries():
    """Gallery: the README's results section documents the push command, the caps and the data-use refusal."""
    text = (ROOT / "README.md").read_text()
    assert "tools/gallery_push.py" in text and "AVM_GALLERY_MAX_BYTES" in text and "every dashboard viewer" in text
