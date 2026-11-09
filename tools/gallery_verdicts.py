#!/usr/bin/env python3
"""Fetch a gallery set's per-box review (the good/bad verdicts and the boxes drawn by hand on the research page) for a
training pipeline.

    python3 tools/gallery_verdicts.py PROJECT SET-OR-SLUG [--out FILE] [--config agent.conf]

SET-OR-SLUG is the set's name as pushed (e.g. "synthetic/cup/sd15_lcm_esp32v2") or its slug
("synthetic__cup__sd15_lcm_esp32v2"); the slug is derived the same way the hub derives it. Writes, to stdout or --out:

    {"project", "set", "slug", "exported_at": ISO-8601,
     "verdicts": [{"file", "box_id", "verdict": "good" | "bad", "at": ISO-8601, "by": reviewer}],
     "added": [{"file", "box_id": "u" + 8 hex, "label", "bbox": [x, y, w, h], "at": ISO-8601, "by": reviewer}]}

"added" are boxes an operator drew where the teacher model missed the object: owner-confirmed positives (bbox in
image pixels from the top-left, clamped to the image). Written through exactly as the hub sends them.

Only verdicts whose (file, box_id) is in the set's current index, and drawn boxes whose file is, are exported: the hub
keeps both across re-pushes, and those for boxes or files a later push dropped stay stored but are left out here.

Reads GET /api/v1/agent/projects/<project>/galleries/<slug>/verdicts with this host's agent token (read-only).
Hub URL and token come from agent.conf, found like gallery_push.py / avm find it: --config, $AVM_CONFIG,
~/.config/avalon-agent/agent.conf, /etc/avalon-agent/agent.conf (AVM_SERVER_URL / AVM_TOKEN in the environment
override). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PROJECT_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


# ------------------------------------------------------------------ config (as gallery_push.py / agent/avm.py find it)
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


def set_slug(name: str) -> str:
    """The hub's routes_gallery.set_slug: lower case, "/" -> "__", anything outside [a-z0-9._-] -> "-", no leading
    dot, dash or underscore. A slug maps to itself, so either the set name or its slug may be given."""
    s = re.sub(r"[^a-z0-9._-]", "-", name.strip().lower().replace("/", "__")).lstrip(".-_")
    return s[:200]


def fetch(cfg: dict, project: str, slug: str) -> dict:
    url = cfg["AVM_SERVER_URL"].rstrip("/") + "/api/v1/agent/projects/%s/galleries/%s/verdicts" % (
        urllib.parse.quote(project, safe=""), urllib.parse.quote(slug, safe=""))
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + cfg["AVM_TOKEN"], "User-Agent": "gallery_verdicts/1"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fetch a gallery set's per-box review (good/bad verdicts, drawn boxes) as JSON.")
    ap.add_argument("project")
    ap.add_argument("set", help="the set's name as pushed, or its slug")
    ap.add_argument("--out", default="", help="write the JSON here (default: stdout)")
    ap.add_argument("--config", default="", help="agent.conf to read AVM_SERVER_URL / AVM_TOKEN from")
    a = ap.parse_args(argv)
    if not PROJECT_RE.match(a.project):
        print("gallery_verdicts: project must match [a-z0-9_-]{1,40}", file=sys.stderr)
        return 2
    slug = set_slug(a.set)
    if not slug:
        print("gallery_verdicts: the set name needs at least one letter or digit", file=sys.stderr)
        return 2
    cfg = load_config(a.config)
    if not (cfg.get("AVM_SERVER_URL") and cfg.get("AVM_TOKEN")):
        print("gallery_verdicts: no agent config found (looked in %s); set AVM_SERVER_URL and AVM_TOKEN"
              % ", ".join(_candidates(a.config)), file=sys.stderr)
        return 2
    try:
        doc = fetch(cfg, a.project, slug)
    except urllib.error.HTTPError as e:
        print("gallery_verdicts: hub refused: HTTP %s %s" % (e.code, e.read().decode(errors="replace")[:400]), file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        print("gallery_verdicts: cannot reach the hub at %s: %s" % (cfg["AVM_SERVER_URL"], e), file=sys.stderr)
        return 1
    text = json.dumps(doc, indent=1) + "\n"
    if a.out:
        tmp = a.out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, a.out)
        print("%s / %s: %d verdicts, %d drawn boxes -> %s" % (doc.get("project"), doc.get("set"), len(doc.get("verdicts") or []),
                                                             len(doc.get("added") or []), a.out), file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
