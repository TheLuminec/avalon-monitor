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
"""
from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from . import datause
from .auth import Identity
from .config import settings
from .core import hub, require_agent, require_operator, require_viewer
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
        nb = []
        for j, b in enumerate(boxes):
            bb = b.get("bbox") if isinstance(b, dict) else None
            if not (isinstance(b, dict) and isinstance(b.get("label"), str) and _finite(b.get("score"))
                    and isinstance(bb, list) and len(bb) == 4 and all(_finite(x) for x in bb)):
                raise HTTPException(422, f"{where}.boxes[{j}] needs a string label, a numeric score and bbox [x, y, w, h]")
            nb.append({"label": b["label"], "score": b["score"], "bbox": bb})
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
    await hub.broadcast({"type": "gallery", "project": project, "slug": meta["slug"]})
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
async def gallery_list(project: str, ident: Identity = Depends(require_viewer)):
    return {"project": project, "galleries": await run_in_threadpool(list_sets, _project(project))}


@router.get("/projects/{project}/galleries/{slug}")
async def gallery_index(project: str, slug: str, ident: Identity = Depends(require_viewer)):
    d = _set_dir(project, slug)
    return Response((d / "index.json").read_bytes(), media_type="application/json", headers={"Cache-Control": "no-cache"})


@router.get("/projects/{project}/galleries/{slug}/image/{file:path}")
async def gallery_image(project: str, slug: str, file: str, ident: Identity = Depends(require_viewer)):
    p = _file_in(_set_dir(project, slug) / "images", file)
    if p is None:
        raise HTTPException(404, "no such image")
    return FileResponse(p, media_type=IMAGE_TYPES[p.suffix.lower()], headers=CACHE)


@router.get("/projects/{project}/galleries/{slug}/thumb/{file:path}")
async def gallery_thumb(project: str, slug: str, file: str, ident: Identity = Depends(require_viewer)):
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
    log.info("gallery %s/%s deleted by %s", project, slug, ident.subject)
    await hub.broadcast({"type": "gallery", "project": project, "slug": slug, "deleted": True})
    return {"ok": True}
