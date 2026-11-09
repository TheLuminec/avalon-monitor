#!/usr/bin/env python3
"""Push an image set to the Avalon Monitor hub's gallery (research page -> Gallery).

    python3 tools/gallery_push.py INDEX.json [--dry-run] [--config agent.conf] [--out set.zip]

INDEX.json (written by the project, one per set):
    {"project": "tinyseek", "set": "synthetic/cup/sd15_lcm_esp32v2", "kind": "synthetic|real",
     "image_root": "<abs dir: where the files are; used only here, never sent>", "labels": ["cup", ...],
     "items": [{"file": "000123.jpg", "width": 512, "height": 512, "text": "...", "seed": 123, "session": null,
                "tags": {"kept": true, "reason": null, "luma": 64.0},
                "boxes": [{"id": "c1a2b3", "label": "cup", "score": 0.41, "bbox": [x, y, w, h]}]}]}
bbox in pixels, top-left origin; tags flat, scalar values only; file relative to image_root. A box's "id" is
optional ([A-Za-z0-9_-]{1,32}, unique within its item, stable across re-exports): only boxes with one can be
reviewed (good/bad) on the dashboard; tools/gallery_verdicts.py fetches that review.

Builds a zip (index.json without image_root + images/<file> + thumbs/<file>.jpg, ~320 px, when Pillow is
importable; without it the hub serves the full images as thumbnails) and streams it to
POST /api/v1/projects/<project>/gallery with this host's agent token. Re-pushing a set replaces it. The hub caps a
set at AVM_GALLERY_MAX_BYTES (default 300 MB) and 20,000 items, and refuses data-use hosts: a gallery is raw images,
and every dashboard viewer can see it. --dry-run builds and checks the zip without uploading (--out keeps it).

Hub URL and token come from agent.conf, found like avm finds it: --config, $AVM_CONFIG,
~/.config/avalon-agent/agent.conf, /etc/avalon-agent/agent.conf (AVM_SERVER_URL / AVM_TOKEN in the environment
override). Standard library only; Pillow optional.
"""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

PROJECT_RE = re.compile(r"^[a-z0-9_-]{1,40}$")
BOX_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")   # optional per-box "id": what the dashboard's per-box review keys on
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
MAX_ITEMS = 20_000
THUMB_PX = 320


# ------------------------------------------------------------------ config (as agent/avm.py finds it)
def _candidates(explicit: str = "") -> list:
    c = [explicit, os.environ.get("AVM_CONFIG", "")]
    c.append(os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "avalon-agent", "agent.conf"))
    c.append("/etc/avalon-agent/agent.conf")
    return [x for x in c if x]


def _read_conf(path: str) -> dict:
    vals: dict = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    v = v.strip()
                    if v[:1] not in ('"', "'"):   # unquoted: an inline comment starts at " #"
                        v = re.split(r"\s+#", v, maxsplit=1)[0]
                    vals.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except (OSError, ValueError):
        return {}
    return vals


def load_config(explicit: str = "") -> dict:
    """The first config that has both AVM_SERVER_URL and AVM_TOKEN; the environment overrides either."""
    cfg: dict = {}
    for path in _candidates(explicit):
        if os.path.isfile(path):
            vals = _read_conf(path)
            if vals.get("AVM_SERVER_URL") and vals.get("AVM_TOKEN"):
                cfg = dict(vals, _path=path)
                break
    for k in ("AVM_SERVER_URL", "AVM_TOKEN"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg


# ------------------------------------------------------------------ checks (the hub checks again, and decides)
def _safe_rel(p) -> bool:
    if not isinstance(p, str) or not p or p.startswith("/") or "\\" in p or ":" in p or re.search(r"[\x00-\x1f\x7f]", p):
        return False
    return all(s not in ("", ".", "..") for s in p.split("/"))


def _finite(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _scalar(v) -> bool:
    return v is None or isinstance(v, (str, bool, int)) or (isinstance(v, float) and math.isfinite(v))


def check_index(doc) -> list:
    """Problems that would make the hub refuse the index (files are checked while zipping)."""
    errs = []
    if not isinstance(doc, dict):
        return ["the index must be a JSON object"]
    if not isinstance(doc.get("project"), str) or not PROJECT_RE.match(doc["project"]):
        errs.append("project must match [a-z0-9_-]{1,40}")
    s = doc.get("set")
    if not isinstance(s, str) or not s.strip() or len(s) > 120:
        errs.append("set must be a non-empty string of at most 120 characters")
    if not isinstance(doc.get("image_root"), str) or not os.path.isdir(doc["image_root"]):
        errs.append("image_root must be an existing directory")
    items = doc.get("items")
    if not isinstance(items, list):
        return errs + ["items must be a list"]
    if len(items) > MAX_ITEMS:
        errs.append("too many items (%d; the hub takes at most %d)" % (len(items), MAX_ITEMS))
    for i, it in enumerate(items):
        w = "items[%d]" % i
        if not isinstance(it, dict):
            errs.append(w + " must be an object")
            continue
        if not _safe_rel(it.get("file")) or not it["file"].lower().endswith(IMAGE_EXT):
            errs.append(w + ".file must be a relative .jpg/.jpeg/.png/.webp path inside image_root")
        for k in ("width", "height"):
            if not isinstance(it.get(k), int) or isinstance(it.get(k), bool) or it[k] <= 0:
                errs.append("%s.%s must be a positive integer" % (w, k))
        tags = it.get("tags") or {}
        if not isinstance(tags, dict) or not all(_scalar(v) for v in tags.values()):
            errs.append(w + ".tags must be an object of scalar values")
        for j, b in enumerate(it.get("boxes") or []):
            bb = b.get("bbox") if isinstance(b, dict) else None
            if not (isinstance(b, dict) and isinstance(b.get("label"), str) and _finite(b.get("score"))
                    and isinstance(bb, list) and len(bb) == 4 and all(_finite(x) for x in bb)):
                errs.append("%s.boxes[%d] needs a string label, a numeric score and bbox [x, y, w, h]" % (w, j))
        ids = [b.get("id") for b in it.get("boxes") or [] if isinstance(b, dict) and b.get("id") is not None]
        if not all(isinstance(x, str) and BOX_ID_RE.match(x) for x in ids):
            errs.append(w + ".boxes: an id must be 1-32 characters of A-Z a-z 0-9 _ -")
        elif len(set(ids)) != len(ids):
            errs.append(w + ".boxes: box ids must be unique within an item")
        if len(errs) > 20:
            errs.append("...")
            break
    return errs


# ------------------------------------------------------------------ zip
def _thumb(path: str):
    """~320 px JPEG bytes, or None without Pillow (or for an image it cannot read)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            im.thumbnail((THUMB_PX, THUMB_PX))
            buf = io.BytesIO()
            im.convert("RGB").save(buf, "JPEG", quality=82)
            return buf.getvalue()
    except Exception:
        return None


def build_zip(doc: dict, out_path: str) -> dict:
    """Write the upload zip; returns {items, images, thumbs, bytes}. Images are stored (already compressed)."""
    root = os.path.realpath(doc["image_root"])
    sent = {k: v for k, v in doc.items() if k != "image_root"}
    files, thumbs = [], 0
    for it in doc["items"]:
        f = it["file"]
        if f in files:
            continue
        src = os.path.realpath(os.path.join(root, f))
        if os.path.commonpath([src, root]) != root:
            raise SystemExit("gallery_push: %s resolves outside image_root" % f)
        if not os.path.isfile(src):
            raise SystemExit("gallery_push: missing image %s" % os.path.join(root, f))
        files.append(f)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("index.json", json.dumps(sent, separators=(",", ":"), allow_nan=False), compress_type=zipfile.ZIP_DEFLATED)
        for f in files:
            src = os.path.join(root, f)
            z.write(src, "images/" + f)
            t = _thumb(src)
            if t is not None:
                z.writestr("thumbs/" + f + ".jpg", t)
                thumbs += 1
    return {"items": len(doc["items"]), "images": len(files), "thumbs": thumbs, "bytes": os.path.getsize(out_path)}


def verify_zip(path: str) -> None:
    """Re-open what was built and check it the way the hub will (entry names, index, every file present)."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        bad = z.testzip()
        if bad:
            raise SystemExit("gallery_push: corrupt entry %s" % bad)
        idx = json.loads(z.read("index.json"))
    if "image_root" in idx:
        raise SystemExit("gallery_push: image_root leaked into the zip")
    for n in names:
        if n != "index.json" and not (n.split("/", 1)[0] in ("images", "thumbs") and _safe_rel(n) and n.lower().endswith(IMAGE_EXT)):
            raise SystemExit("gallery_push: unexpected zip entry %s" % n)
    missing = [it["file"] for it in idx["items"] if "images/" + it["file"] not in names]
    if missing:
        raise SystemExit("gallery_push: %d image(s) missing from the zip, e.g. %s" % (len(missing), missing[0]))


def upload(cfg: dict, project: str, path: str) -> dict:
    url = cfg["AVM_SERVER_URL"].rstrip("/") + "/api/v1/projects/%s/gallery" % project
    size = os.path.getsize(path)
    with open(path, "rb") as f:   # streamed: urllib sends a file object in blocks when Content-Length is set
        req = urllib.request.Request(url, data=f, method="POST", headers={
            "Content-Type": "application/zip", "Content-Length": str(size),
            "Authorization": "Bearer " + cfg["AVM_TOKEN"], "User-Agent": "gallery_push/1"})
        with urllib.request.urlopen(req, timeout=900) as r:
            return json.loads(r.read().decode("utf-8") or "{}")


def _mb(n: int) -> str:
    return "%.1f MB" % (n / 1048576)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Push an image set to the Avalon Monitor gallery.")
    ap.add_argument("index", help="the set's index JSON")
    ap.add_argument("--dry-run", action="store_true", help="build and check the zip, do not upload")
    ap.add_argument("--config", default="", help="agent.conf to read AVM_SERVER_URL / AVM_TOKEN from")
    ap.add_argument("--out", default="", help="keep the zip at this path")
    a = ap.parse_args(argv)
    try:
        with open(a.index, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print("gallery_push: cannot read %s: %s" % (a.index, e), file=sys.stderr)
        return 2
    errs = check_index(doc)
    if errs:
        print("gallery_push: the index is not valid:\n  " + "\n  ".join(errs), file=sys.stderr)
        return 2
    cfg = load_config(a.config)
    if not a.dry_run and not (cfg.get("AVM_SERVER_URL") and cfg.get("AVM_TOKEN")):
        print("gallery_push: no agent config found (looked in %s); set AVM_SERVER_URL and AVM_TOKEN"
              % ", ".join(_candidates(a.config)), file=sys.stderr)
        return 2
    if a.out:
        out, keep = a.out, True
    else:
        fd, out = tempfile.mkstemp(prefix="gallery-", suffix=".zip")
        os.close(fd)
        keep = False
    try:
        st = build_zip(doc, out)
        verify_zip(out)
        print("%s / %s: %d items, %d images, %d thumbnails%s, zip %s"
              % (doc["project"], doc["set"], st["items"], st["images"], st["thumbs"],
                 "" if st["thumbs"] else " (no Pillow: the hub shows full images)", _mb(st["bytes"])))
        if a.dry_run:
            print("dry run: zip OK, not uploaded" + (" (kept at %s)" % out if keep else ""))
            return 0
        try:
            reply = upload(cfg, doc["project"], out)
        except urllib.error.HTTPError as e:
            print("gallery_push: hub refused the push: HTTP %s %s" % (e.code, e.read().decode(errors="replace")[:400]), file=sys.stderr)
            return 1
        except (OSError, ValueError) as e:
            print("gallery_push: cannot reach the hub at %s: %s" % (cfg["AVM_SERVER_URL"], e), file=sys.stderr)
            return 1
        print("hub: " + json.dumps(reply))
        return 0
    finally:
        if not keep:
            try:
                os.unlink(out)
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
