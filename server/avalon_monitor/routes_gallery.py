"""Image galleries: a project pushes an image set (a zip of index.json + images/ + thumbs/, made by
tools/gallery_push.py) and the research page browses it with its boxes drawn over each image.

Push, like results: the hub runs as its own user and cannot read project directories, so it serves only what was
pushed, from <dir of AVM_DB_PATH>/galleries/<project>/<set-slug>/{index.json, meta.json, images/, thumbs/}.
A gallery carries raw images, so a data-use host (datause.restricted) may not push one at all, and everything pushed
is visible to every dashboard viewer.

The upload is untrusted input: it is streamed to a temp file under its own cap (AVM_GALLERY_MAX_BYTES; the global
max_payload_bytes does not apply here), every zip entry name is checked before anything is written (no absolute
paths, no "..", no symlinks or special files, only index.json / images/* / thumbs/* with an image extension), sizes
are checked from the zip headers and again while extracting (zip bombs), and the index must describe exactly what
was sent. A re-push of the same set replaces it atomically: unpacked beside it, then swapped in with one rename.

Per-box review: a box may carry an "id" ([A-Za-z0-9_-]{1,32}, unique within its item; Tinyseek uses a short hash of
label + rounded bbox, stable across re-exports). An operator marks such a box good or bad (PUT .../verdicts); the
verdicts live in the DB (gallery_verdicts), keyed by (project, slug, file, box_id), so they outlive re-pushes. Every
reader (the page, the counts, the agent export for training pipelines) only sees verdicts whose (file, box_id) is in
the set's current index; the others stay stored and come back if a later push brings the box back. Deleting the set
deletes its verdicts.

Drawn boxes: the teacher model misses objects, so an operator can draw a box by hand in the full-size view (POST
.../added, then PATCH / DELETE .../added/<box_id>). They live in the DB (gallery_added_boxes) with a hub-made id
("u" + 8 hex characters), are clamped to the image, and follow the verdicts' rules: kept across re-pushes, reported
only while their file is in the current index, deleted with the set. The agent export carries them next to the
verdicts ("added"): owner-confirmed positives for training and the real test set.
"""
from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from . import datause
from .auth import Identity
from .config import settings
from .core import db, hub, require_agent, require_login, require_operator
from .db import HostRow
from .routes_results import PROJECT_RE, _project

log = logging.getLogger("avalon.gallery")
router = APIRouter(prefix="/api/v1")

MAX_ITEMS = 20_000
INDEX_MAX_BYTES = 32 * 1024 * 1024      # index.json alone (inside the set cap)
MAX_ENTRIES = 2 * MAX_ITEMS + 200       # images + thumbs + index + directory entries
SET_MAX_CHARS = 120
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,199}$")
BOX_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
VERDICTS = ("good", "bad")
LABEL_MAX_CHARS = 40   # a drawn box's label (any string: a class the set's labels don't have yet is fine)
MIN_DRAWN_PX = 2       # a drawn box narrower or lower than this, after clamping to the image, is refused
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\:]")   # control characters, backslashes, drive letters
CACHE = {"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"}
_swap_lock = threading.Lock()


def gallery_root() -> Path:
    """<dir of AVM_DB_PATH>/galleries (the live hub: /var/lib/avalon-monitor/galleries, its only writable dir)."""
    return Path(settings.db_path).resolve().parent / "galleries"


def set_slug(name: str) -> str:
    """A set's directory name: lower case, "/" -> "__", anything outside [a-z0-9._-] -> "-", no leading dot or dash
    (temp dirs start with a dot). The display name stays in the index."""
    s = re.sub(r"[^a-z0-9._-]", "-", name.strip().lower().replace("/", "__")).lstrip(".-_")
    return s[:200]


def safe_rel(path: Any) -> bool:
    """A relative path that stays where it is put: no leading "/", no "", "." or ".." segments, no control
    characters, backslashes or drive letters."""
    if not isinstance(path, str) or not path or len(path) > 400 or path.startswith("/") or _BAD_CHARS.search(path):
        return False
    return all(p not in ("", ".", "..") for p in path.split("/"))


def _ext_ok(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_TYPES


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, bool, int)) or (isinstance(v, float) and math.isfinite(v))


# ------------------------------------------------------------------ validation
def check_entries(zf: zipfile.ZipFile, cap: int) -> list[zipfile.ZipInfo]:
    """The zip's file entries, after refusing anything unsafe. Nothing has been written yet when this raises."""
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise HTTPException(413, f"too many zip entries ({len(infos)}; at most {MAX_ENTRIES})")
    files, seen, total = [], set(), 0
    for zi in infos:
        name = zi.filename
        mode = zi.external_attr >> 16
        if stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise HTTPException(422, f"zip entry {name!r} is a symlink or special file")
        if zi.flag_bits & 0x1:
            raise HTTPException(422, f"zip entry {name!r} is encrypted")
        if zi.is_dir():
            d = name.rstrip("/")
            if d not in ("images", "thumbs") and not (safe_rel(d) and d.split("/", 1)[0] in ("images", "thumbs")):
                raise HTTPException(422, f"zip entry {name!r} is not allowed (only index.json, images/*, thumbs/*)")
            continue
        if not safe_rel(name):
            raise HTTPException(422, f"zip entry {name!r} has an unsafe path (absolute, '..', or odd characters)")
        top, _, rest = name.partition("/")
        if name != "index.json" and not (top in ("images", "thumbs") and rest):
            raise HTTPException(422, f"zip entry {name!r} is not allowed (only index.json, images/*, thumbs/*)")
        if name != "index.json" and not _ext_ok(name):
            raise HTTPException(422, f"zip entry {name!r}: images must be .jpg, .jpeg, .png or .webp")
        if name in seen:
            raise HTTPException(422, f"zip entry {name!r} appears twice")
        seen.add(name)
        total += zi.file_size
        if total > cap:
            raise HTTPException(413, f"unpacked set exceeds {cap} bytes (AVM_GALLERY_MAX_BYTES)")
        files.append(zi)
    if "index.json" not in seen:
        raise HTTPException(422, "zip has no index.json")
    return files


def validate_index(project: str, doc: Any, images: set[str]) -> dict:
    """The index, normalised to what the page reads. Every item's file must be among the pushed images."""
    if not isinstance(doc, dict):
        raise HTTPException(422, "index.json must be a JSON object")
    if doc.get("project") != project:
        raise HTTPException(422, f"index project {doc.get('project')!r} does not match URL project {project!r}")
    name = doc.get("set")
    if not isinstance(name, str) or not name.strip() or len(name) > SET_MAX_CHARS:
        raise HTTPException(422, f"set must be a non-empty string of at most {SET_MAX_CHARS} characters")
    if not set_slug(name):
        raise HTTPException(422, "set needs at least one letter or digit")
    kind = doc.get("kind")
    if kind is not None and (not isinstance(kind, str) or len(kind) > 40):
        raise HTTPException(422, "kind must be a short string (e.g. synthetic or real)")
    labels = doc.get("labels")
    if labels is not None and (not isinstance(labels, list) or not all(isinstance(x, str) for x in labels)):
        raise HTTPException(422, "labels must be a list of strings")
    items = doc.get("items")
    if not isinstance(items, list):
        raise HTTPException(422, "items must be a list")
    if len(items) > MAX_ITEMS:
        raise HTTPException(413, f"too many items ({len(items)}; at most {MAX_ITEMS})")
    out_items, seen_labels = [], []
    for i, it in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(it, dict):
            raise HTTPException(422, f"{where} must be an object")
        f = it.get("file")
        if not safe_rel(f) or not _ext_ok(f):
            raise HTTPException(422, f"{where}.file must be a relative .jpg/.jpeg/.png/.webp path")
        if f not in images:
            raise HTTPException(422, f"{where}: file {f!r} is not in images/")
        for k in ("width", "height"):
            v = it.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise HTTPException(422, f"{where}.{k} must be a positive integer")
        tags = it.get("tags") if it.get("tags") is not None else {}
        if not isinstance(tags, dict) or not all(isinstance(k, str) and _scalar(v) for k, v in tags.items()):
            raise HTTPException(422, f"{where}.tags must be an object of scalar values")
        boxes = it.get("boxes") if it.get("boxes") is not None else []
        if not isinstance(boxes, list):
            raise HTTPException(422, f"{where}.boxes must be a list")
        nb, ids = [], set()
        for j, b in enumerate(boxes):
            bb = b.get("bbox") if isinstance(b, dict) else None
            if not (isinstance(b, dict) and isinstance(b.get("label"), str) and _finite(b.get("score"))
                    and isinstance(bb, list) and len(bb) == 4 and all(_finite(x) for x in bb)):
                raise HTTPException(422, f"{where}.boxes[{j}] needs a string label, a numeric score and bbox [x, y, w, h]")
            box = {"label": b["label"], "score": b["score"], "bbox": bb}
            bid = b.get("id")
            if bid is not None:   # optional: a box without an id simply can't be reviewed
                if not isinstance(bid, str) or not BOX_ID_RE.match(bid):
                    raise HTTPException(422, f"{where}.boxes[{j}].id must be 1-32 characters of A-Z a-z 0-9 _ -")
                if bid in ids:
                    raise HTTPException(422, f"{where}.boxes[{j}].id {bid!r} appears twice in this item"
                                             " (box ids must be unique within an item)")
                ids.add(bid)
                box["id"] = bid
            nb.append(box)
            if b["label"] not in seen_labels:
                seen_labels.append(b["label"])
        for k in ("text", "seed", "session"):
            if not _scalar(it.get(k)):
                raise HTTPException(422, f"{where}.{k} must be a scalar (string, number, true/false or null)")
        item = {"file": f, "width": it["width"], "height": it["height"], "text": it.get("text"), "seed": it.get("seed"),
                "session": it.get("session"), "tags": tags, "boxes": nb}
        # other scalar fields the project sends are kept for the detail view
        item.update({k: v for k, v in it.items() if k not in item and isinstance(k, str) and _scalar(v)})
        out_items.append(item)
    return {"project": project, "set": name, "kind": kind,
            "labels": labels if labels is not None else seen_labels, "items": out_items}


# ------------------------------------------------------------------ unpacking
def _no_constants(c: str):
    raise ValueError(f"{c} is not valid JSON")


def _exchange(a: Path, b: Path) -> bool:
    """Swap two paths in one step (Linux renameat2 RENAME_EXCHANGE); False where that isn't available."""
    try:
        fn = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return False
    return fn(-100, os.fsencode(str(a)), -100, os.fsencode(str(b)), 2) == 0   # AT_FDCWD, RENAME_EXCHANGE


def _sweep(d: Path) -> None:
    """Leftovers of a push that crashed mid-way (older than an hour, so never a push in progress)."""
    for p in d.glob(".*"):
        try:
            if time.time() - p.stat().st_mtime > 3600:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
        except OSError:
            pass


def unpack(project: str, upload: Path, upload_bytes: int, host: str) -> dict:
    """Check, unpack and swap in one uploaded zip. Returns the set's meta."""
    cap = settings.gallery_max_bytes
    proj_dir = gallery_root() / project
    proj_dir.mkdir(parents=True, exist_ok=True)
    _sweep(proj_dir)
    tmp = proj_dir / f".tmp-{uuid.uuid4().hex}"
    try:
        try:
            zf = zipfile.ZipFile(upload)
        except (zipfile.BadZipFile, OSError) as e:
            raise HTTPException(422, f"not a valid zip: {e}") from None
        with zf:
            files = check_entries(zf, cap)
            idx = next(zi for zi in files if zi.filename == "index.json")
            if idx.file_size > INDEX_MAX_BYTES:
                raise HTTPException(413, f"index.json exceeds {INDEX_MAX_BYTES} bytes")
            tmp.mkdir()
            base, written = tmp.resolve(), 0
            for zi in files:
                dest = (tmp / zi.filename).resolve()
                if base not in dest.parents:   # cannot happen after check_entries; belt and braces
                    raise HTTPException(422, f"zip entry {zi.filename!r} escapes the set")
                dest.parent.mkdir(parents=True, exist_ok=True)
                n = 0
                try:
                    with zf.open(zi) as src, open(dest, "xb") as dst:
                        while True:
                            chunk = src.read(1 << 20)
                            if not chunk:
                                break
                            n += len(chunk)
                            written += len(chunk)
                            if n > zi.file_size or written > cap:   # more than its header said: a zip bomb
                                raise HTTPException(413, f"zip entry {zi.filename!r} unpacks larger than declared")
                            dst.write(chunk)
                except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as e:
                    raise HTTPException(422, f"cannot unpack {zi.filename!r}: {e}") from None
        try:
            doc = json.loads((tmp / "index.json").read_text("utf-8"), parse_constant=_no_constants)
        except (ValueError, UnicodeDecodeError) as e:
            raise HTTPException(422, f"index.json is not valid JSON: {e}") from None
        images = {zi.filename[len("images/"):] for zi in files if zi.filename.startswith("images/")}
        index = validate_index(project, doc, images)
        slug = set_slug(index["set"])
        meta = {"slug": slug, "set": index["set"], "kind": index["kind"], "items": len(index["items"]),
                "labels": index["labels"], "pushed_at": time.time(), "host": host, "bytes": written,
                "upload_bytes": upload_bytes, "thumbs": any(zi.filename.startswith("thumbs/") for zi in files)}
        index.update({k: meta[k] for k in ("slug", "pushed_at", "host", "bytes", "thumbs")})
        (tmp / "index.json").write_text(json.dumps(index, separators=(",", ":")), "utf-8")
        (tmp / "meta.json").write_text(json.dumps(meta), "utf-8")
        final = proj_dir / slug
        with _swap_lock:
            if final.exists():
                if _exchange(tmp, final):   # tmp now holds the old set
                    shutil.rmtree(tmp, ignore_errors=True)
                else:
                    old = proj_dir / f".old-{uuid.uuid4().hex}"
                    os.rename(final, old)
                    os.rename(tmp, final)
                    shutil.rmtree(old, ignore_errors=True)
            else:
                os.rename(tmp, final)
        return meta
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ routes
@router.post("/projects/{project}/gallery")
async def gallery_push(project: str, request: Request, host: HostRow = Depends(require_agent)):
    """Body: the zip (application/zip). Read here in chunks: max_payload_bytes does not apply, the set cap does."""
    _project(project)
    if datause.restricted(host):
        raise HTTPException(403, "data-use host: galleries carry raw images and are refused from this host")
    cap = settings.gallery_max_bytes
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > cap:
        raise HTTPException(413, f"upload of {declared} bytes exceeds {cap} bytes (AVM_GALLERY_MAX_BYTES)")
    incoming = gallery_root() / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=incoming, suffix=".zip")
    path, size = Path(name), 0
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > cap:
                    raise HTTPException(413, f"upload exceeds {cap} bytes (AVM_GALLERY_MAX_BYTES)")
                f.write(chunk)
        meta = await run_in_threadpool(unpack, project, path, size, host.name)
    finally:
        path.unlink(missing_ok=True)
    log.info("gallery %s/%s pushed from %s: %d items, %d bytes", project, meta["slug"], host.name, meta["items"], meta["bytes"])
    await hub.broadcast({"type": "gallery", "project": project, "slug": meta["slug"]}, login_only=True)
    return {"ok": True, "project": project, **meta}


def list_sets(project: str) -> list[dict]:
    d = gallery_root() / project
    out = []
    if d.is_dir():
        for p in d.iterdir():
            if not SLUG_RE.match(p.name):
                continue
            try:
                out.append(json.loads((p / "meta.json").read_text("utf-8")))
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda m: m.get("pushed_at") or 0, reverse=True)


def gallery_projects() -> dict[str, int]:
    """Projects that have galleries -> how many sets (for /api/v1/projects)."""
    root, out = gallery_root(), {}
    if root.is_dir():
        for p in root.iterdir():
            if p.is_dir() and PROJECT_RE.match(p.name):
                n = sum(1 for s in p.iterdir() if SLUG_RE.match(s.name) and (s / "meta.json").is_file())
                if n:
                    out[p.name] = n
    return out


def _set_dir(project: str, slug: str) -> Path:
    _project(project)
    d = gallery_root() / project / slug
    if not SLUG_RE.match(slug) or not d.is_dir():
        raise HTTPException(404, "no such gallery set")
    return d


def _file_in(base: Path, rel: str) -> Optional[Path]:
    """`rel` under `base`, or None: resolved first, then confirmed to still be inside base (no "..", no symlink out)."""
    try:
        base = base.resolve()
        p = (base / rel).resolve()
    except (OSError, ValueError):
        return None
    if base not in p.parents or not p.is_file() or p.suffix.lower() not in IMAGE_TYPES:
        return None
    return p


@router.get("/projects/{project}/galleries")
async def gallery_list(project: str, ident: Identity = Depends(require_login)):
    return {"project": project, "galleries": await run_in_threadpool(list_sets, _project(project))}


@router.get("/projects/{project}/galleries/{slug}")
async def gallery_index(project: str, slug: str, ident: Identity = Depends(require_login)):
    d = _set_dir(project, slug)
    return Response((d / "index.json").read_bytes(), media_type="application/json", headers={"Cache-Control": "no-cache"})


@router.get("/projects/{project}/galleries/{slug}/image/{file:path}")
async def gallery_image(project: str, slug: str, file: str, ident: Identity = Depends(require_login)):
    p = _file_in(_set_dir(project, slug) / "images", file)
    if p is None:
        raise HTTPException(404, "no such image")
    return FileResponse(p, media_type=IMAGE_TYPES[p.suffix.lower()], headers=CACHE)


@router.get("/projects/{project}/galleries/{slug}/thumb/{file:path}")
async def gallery_thumb(project: str, slug: str, file: str, ident: Identity = Depends(require_login)):
    """The ~320 px thumbnail; the full image when the set was pushed without thumbnails (no Pillow on the pusher)."""
    d = _set_dir(project, slug)
    p = _file_in(d / "thumbs", file + ".jpg") if safe_rel(file) else None
    p = p or _file_in(d / "images", file)
    if p is None:
        raise HTTPException(404, "no such image")
    return FileResponse(p, media_type=IMAGE_TYPES[p.suffix.lower()], headers=CACHE)


@router.delete("/projects/{project}/galleries/{slug}")
async def gallery_delete(project: str, slug: str, ident: Identity = Depends(require_operator)):
    d = _set_dir(project, slug)
    gone = d.parent / f".del-{uuid.uuid4().hex}"
    try:
        os.rename(d, gone)   # out of sight at once, then removed
    except OSError:
        raise HTTPException(404, "no such gallery set") from None
    await run_in_threadpool(shutil.rmtree, gone, True)
    await run_in_threadpool(db.verdicts_delete_set, project, slug)
    await run_in_threadpool(db.added_delete_set, project, slug)
    log.info("gallery %s/%s deleted by %s", project, slug, ident.subject)
    await hub.broadcast({"type": "gallery", "project": project, "slug": slug, "deleted": True}, login_only=True)
    return {"ok": True}


# ------------------------------------------------------------------ per-box review
_ids_cache: dict[str, tuple[tuple, str, frozenset, dict]] = {}
_ids_lock = threading.Lock()


def index_boxes(d: Path) -> tuple[str, frozenset]:
    """(set display name, {(file, box_id)} of every box with an id) from a set's current index."""
    name, ids, _ = index_info(d)
    return name, ids


def index_info(d: Path) -> tuple[str, frozenset, dict]:
    """(set display name, {(file, box_id)} of every box with an id, {file: (width, height)}) from a set's current
    index. Cached per index file (inode, mtime, size): a re-push swaps in a new file, so the cache never answers for a
    replaced index."""
    p = d / "index.json"
    try:
        st = p.stat()
    except OSError:
        raise HTTPException(404, "no such gallery set") from None
    key, stamp = str(p), (st.st_ino, st.st_mtime_ns, st.st_size)
    with _ids_lock:
        hit = _ids_cache.get(key)
        if hit and hit[0] == stamp:
            return hit[1], hit[2], hit[3]
    try:
        doc = json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        raise HTTPException(404, "no such gallery set") from None
    ids = frozenset((it["file"], b["id"]) for it in doc.get("items") or [] for b in it.get("boxes") or [] if "id" in b)
    dims = {it["file"]: (it["width"], it["height"]) for it in doc.get("items") or []}
    name = doc.get("set") or ""
    with _ids_lock:
        if len(_ids_cache) > 64:
            _ids_cache.clear()
        _ids_cache[key] = (stamp, name, ids, dims)
    return name, ids, dims


def current_verdicts(project: str, slug: str) -> tuple[str, list[dict]]:
    """(set name, the stored verdicts whose box is in the current index). Orphans stay in the DB, unreported."""
    name, ids = index_boxes(_set_dir(project, slug))
    return name, [v for v in db.verdicts_list(project, slug) if (v["file"], v["box_id"]) in ids]


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


@router.get("/projects/{project}/galleries/{slug}/verdicts")
async def gallery_verdicts(project: str, slug: str, ident: Identity = Depends(require_login)):
    _, verdicts = await run_in_threadpool(current_verdicts, project, slug)
    return {"verdicts": verdicts}


@router.put("/projects/{project}/galleries/{slug}/verdicts")
async def gallery_verdict_put(project: str, slug: str, request: Request, ident: Identity = Depends(require_operator)):
    """Body {file, box_id, verdict: "good" | "bad" | null}; null clears it. The box must be in the current index."""
    d = _set_dir(project, slug)
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "body must be JSON {file, box_id, verdict}") from None
    if not isinstance(body, dict) or "verdict" not in body:
        raise HTTPException(422, "body must be a JSON object {file, box_id, verdict}")
    file, box_id, verdict = body.get("file"), body.get("box_id"), body["verdict"]
    if verdict is not None and verdict not in VERDICTS:
        raise HTTPException(422, 'verdict must be "good", "bad" or null (clears it)')
    if not isinstance(file, str) or not isinstance(box_id, str):
        raise HTTPException(422, "file and box_id must be strings")
    _, ids = await run_in_threadpool(index_boxes, d)
    if (file, box_id) not in ids:
        raise HTTPException(404, "no box with that id on that file in this set's current index")
    row = await run_in_threadpool(db.verdict_set, project, slug, file, box_id, verdict, ident.subject)
    out = row or {"file": file, "box_id": box_id, "verdict": None, "at": time.time(), "by": ident.subject}
    log.info("gallery %s/%s verdict %s#%s -> %s by %s", project, slug, file, box_id, verdict, ident.subject)
    await hub.broadcast({"type": "gallery_verdict", "project": project, "slug": slug, **out}, login_only=True)
    return {"ok": True, **out}


@router.get("/agent/projects/{project}/galleries/{slug}/verdicts")
async def gallery_verdicts_export(project: str, slug: str, host: HostRow = Depends(require_agent)):
    """Read-only, with an agent token: the review, for a training pipeline (tools/gallery_verdicts.py)."""
    name, verdicts = await run_in_threadpool(current_verdicts, project, slug)
    added = await run_in_threadpool(current_added, project, slug)
    return {"project": project, "set": name, "slug": slug, "exported_at": _iso(time.time()),
            "verdicts": [{**v, "at": _iso(v["at"])} for v in verdicts],
            "added": [{**a, "at": _iso(a["at"])} for a in added]}


# ------------------------------------------------------------------ drawn boxes
def current_added(project: str, slug: str) -> list[dict]:
    """The stored drawn boxes whose file is in the current index. Orphans stay in the DB, unreported."""
    _, _, dims = index_info(_set_dir(project, slug))
    return [a for a in db.added_list(project, slug) if a["file"] in dims]


def check_label(v: Any) -> str:
    """A drawn box's label: a non-empty string of at most LABEL_MAX_CHARS, no control characters (surrounding blanks
    dropped). Not limited to the set's labels: a class the teacher doesn't know is the point."""
    if not isinstance(v, str) or not v.strip() or len(v.strip()) > LABEL_MAX_CHARS or re.search(r"[\x00-\x1f\x7f]", v):
        raise HTTPException(422, f"label must be a non-empty string of at most {LABEL_MAX_CHARS} characters")
    return v.strip()


def clamp_bbox(v: Any, width: int, height: int) -> list[float]:
    """[x, y, w, h] (4 finite numbers) clamped to the image (0..width, 0..height), rounded to 0.01 px. 422 if it is
    not 4 numbers, or if what is left inside the image is narrower or lower than MIN_DRAWN_PX."""
    if not (isinstance(v, list) and len(v) == 4 and all(_finite(x) for x in v)):
        raise HTTPException(422, "bbox must be [x, y, w, h]: 4 finite numbers, in image pixels from the top-left")
    x, y, w, h = (float(t) for t in v)
    x0, y0 = min(max(x, 0.0), width), min(max(y, 0.0), height)
    x1, y1 = min(max(x + w, 0.0), width), min(max(y + h, 0.0), height)
    if x1 - x0 < MIN_DRAWN_PX or y1 - y0 < MIN_DRAWN_PX:
        raise HTTPException(422, f"bbox is smaller than {MIN_DRAWN_PX} x {MIN_DRAWN_PX} px inside the image "
                                 f"({width} x {height}) after clamping")
    return [round(x0, 2), round(y0, 2), round(x1 - x0, 2), round(y1 - y0, 2)]


async def _body(request: Request, what: str) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, f"body must be JSON {what}") from None
    if not isinstance(body, dict):
        raise HTTPException(422, f"body must be a JSON object {what}")
    return body


def _current_box(project: str, slug: str, d: Path, box_id: str) -> tuple[dict, tuple[int, int]]:
    """A drawn box whose file is in the current index, and that file's (width, height); 404 otherwise (an orphan
    can't be changed until a push brings its file back)."""
    row = db.added_get(project, slug, box_id) if isinstance(box_id, str) and BOX_ID_RE.match(box_id) else None
    dims = index_info(d)[2]
    if row is None or row["file"] not in dims:
        raise HTTPException(404, "no drawn box with that id on a file in this set's current index")
    return row, dims[row["file"]]


@router.get("/projects/{project}/galleries/{slug}/added")
async def gallery_added(project: str, slug: str, ident: Identity = Depends(require_login)):
    return {"added": await run_in_threadpool(current_added, project, slug)}


@router.post("/projects/{project}/galleries/{slug}/added")
async def gallery_added_post(project: str, slug: str, request: Request, ident: Identity = Depends(require_operator)):
    """Body {file, label, bbox: [x, y, w, h]}. The file must be in the current index; the bbox is clamped to it."""
    d = _set_dir(project, slug)
    body = await _body(request, "{file, label, bbox}")
    file = body.get("file")
    dims = (await run_in_threadpool(index_info, d))[2]
    if not isinstance(file, str) or file not in dims:
        raise HTTPException(404, "no such file in this set's current index")
    label, bbox = check_label(body.get("label")), clamp_bbox(body.get("bbox"), *dims[file])
    row = None
    while row is None:   # 32 bits per id: a clash within one set is all but impossible, but cheap to retry
        row = await run_in_threadpool(db.added_insert, project, slug, file, "u" + secrets.token_hex(4), label, bbox, ident.subject)
    log.info("gallery %s/%s drawn box %s#%s %s %s by %s", project, slug, file, row["box_id"], label, bbox, ident.subject)
    await hub.broadcast({"type": "gallery_added", "project": project, "slug": slug, "op": "create", **row}, login_only=True)
    return {"ok": True, **row}


@router.patch("/projects/{project}/galleries/{slug}/added/{box_id}")
async def gallery_added_patch(project: str, slug: str, box_id: str, request: Request, ident: Identity = Depends(require_operator)):
    """Body {label?, bbox?}: relabel and/or move a drawn box, validated like a new one."""
    d = _set_dir(project, slug)
    body = await _body(request, "{label?, bbox?}")
    if "label" not in body and "bbox" not in body:
        raise HTTPException(422, "nothing to change: give label and/or bbox")
    cur, (width, height) = await run_in_threadpool(_current_box, project, slug, d, box_id)
    label = check_label(body["label"]) if "label" in body else cur["label"]
    bbox = clamp_bbox(body["bbox"], width, height) if "bbox" in body else cur["bbox"]
    row = await run_in_threadpool(db.added_update, project, slug, box_id, label, bbox, ident.subject)
    if row is None:   # deleted meanwhile
        raise HTTPException(404, "no drawn box with that id on a file in this set's current index")
    log.info("gallery %s/%s drawn box %s#%s -> %s %s by %s", project, slug, row["file"], box_id, label, bbox, ident.subject)
    await hub.broadcast({"type": "gallery_added", "project": project, "slug": slug, "op": "update", **row}, login_only=True)
    return {"ok": True, **row}


@router.delete("/projects/{project}/galleries/{slug}/added/{box_id}")
async def gallery_added_delete(project: str, slug: str, box_id: str, ident: Identity = Depends(require_operator)):
    d = _set_dir(project, slug)
    cur, _ = await run_in_threadpool(_current_box, project, slug, d, box_id)
    if not await run_in_threadpool(db.added_delete, project, slug, box_id):
        raise HTTPException(404, "no drawn box with that id on a file in this set's current index")
    log.info("gallery %s/%s drawn box %s#%s deleted by %s", project, slug, cur["file"], box_id, ident.subject)
    await hub.broadcast({"type": "gallery_added", "project": project, "slug": slug, "op": "delete", "file": cur["file"],
                         "box_id": box_id, "by": ident.subject}, login_only=True)
    return {"ok": True}
