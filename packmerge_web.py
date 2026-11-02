"""
packmerge_web -- a browser UI for packmerge, served from your own machine.

    python3 packmerge_web.py [--port 8471] [--host 127.0.0.1] [--no-browser]

Then open the printed address in your browser. Same engine (packmerge.py)
and shared state file -- this page is the face of the app.
Stdlib only: no pip packages, no JS frameworks.

Binds to 127.0.0.1 by default (this machine only). Pass --host 0.0.0.0 to
reach it from other devices on your network (e.g. your phone on WiFi).
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import packmerge as _engine  # engine reuse: Modrinth resolve + downloads

HERE = os.path.dirname(os.path.abspath(__file__))
PACKMERGE_PY = os.path.join(HERE, "packmerge.py")
STATE_PATH = os.path.join(HERE, "packmerge-gui-state.json")
LAST_LOG_PATH = os.path.join(HERE, "packmerge-last-run.log")
MODRINTH_API = "https://api.modrinth.com/v2"


def default_state():
    return {
        "mc_version": "latest",
        "pack_name": "",
        "description": "",
        "output": "",
        "smart_json_merge": True,
        "merge_respackopts": True,
        "sources": [],
        "overrides": {},
        "exclude": [],
    }


def load_state():
    try:
        with open(STATE_PATH) as f:
            data = json.load(f)
        if isinstance(data, dict):
            base = default_state()
            base.update(data)
            return base
    except Exception:
        pass
    return default_state()


def save_state(data):
    with open(STATE_PATH, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


# ------------------------------------------------------------ run control
run_lock = threading.Lock()
run_info = {"running": False, "lines": [], "done": True, "rc": 0}

# restore the last run's log so the page shows it after reload/restart
try:
    with open(LAST_LOG_PATH, encoding="utf-8", errors="replace") as f:
        run_info["lines"] = f.read().splitlines(keepends=True)
except OSError:
    pass


def _append_log_line(line):
    with run_lock:
        run_info["lines"].append(line)
    try:
        with open(LAST_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except OSError:
        pass


def start_run(mode, manifest_path=None, after=None):
    with run_lock:
        if run_info["running"]:
            return False
        run_info.update(running=True, lines=[], done=False, rc=0)
    try:
        # fresh log file for this run
        open(LAST_LOG_PATH, "w").close()
    except OSError:
        pass
    threading.Thread(target=_run_worker,
                     args=(mode, manifest_path or STATE_PATH, after),
                     daemon=True).start()
    return True


def _run_worker(mode, manifest_path, after=None):
    cmd = [sys.executable, PACKMERGE_PY, "-m", manifest_path]
    if mode == "check":
        cmd.append("--check")
    lines = []
    rc = 1
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        for line in proc.stdout:
            lines.append(line)
            _append_log_line(line)
        proc.wait()
        rc = proc.returncode
    except Exception as e:
        msg = f"[packmerge] failed to start: {e}\n"
        lines.append(msg)
        _append_log_line(msg)
    if after is not None:
        # run before marking done, so a client polling /api/log never
        # sees done=True while the post-build sync is still in flight
        try:
            after(rc)
        except Exception as e:
            _append_log_line(f"[packmerge-web] workbench sync failed: {e}\n")
    with run_lock:
        run_info.update(running=False, done=True, rc=rc)


# ------------------------------------------------------------ file browse
def browse(path):
    start = os.path.join(os.path.expanduser("~"), "Downloads")
    p = os.path.abspath(path) if path else start
    if not os.path.isdir(p):
        p = start
    try:
        entries = sorted(os.listdir(p), key=str.lower)
    except OSError:
        entries = []
    dirs, zips = [], []
    for e in entries:
        if e.startswith("."):
            continue
        full = os.path.join(p, e)
        if os.path.isdir(full):
            dirs.append(e)
        elif e.lower().endswith(".zip"):
            zips.append(e)
    parent = os.path.dirname(p)
    return {"path": p, "parent": parent if parent != p else None,
            "dirs": dirs, "zips": zips}


def normalize_path(path):
    """Store local paths relative to the script dir when possible."""
    p = os.path.abspath(os.path.expanduser(path))
    try:
        rel = os.path.relpath(p, HERE)
    except ValueError:
        return p
    return rel if not rel.startswith("..") else p


def open_output_folder():
    st = load_state()
    out = st.get("output") or "MergedPack.zip"
    folder = os.path.dirname(os.path.abspath(os.path.join(HERE, out)))
    try:
        if sys.platform.startswith("win"):
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ------------------------------------------------------------ workbench (2.0 slice 1)
# The 2.0 Workbench tab (served at /workbench) runs on these real
# endpoints. The 1.x page above is untouched; this section only adds
# new routes. Slice 1 covers sources + build. Conflicts, RPO Toggles,
# Discover, and Activity stay demo in the workbench page and are not
# wired here.
#
# State: the workbench shares packmerge-gui-state.json with 1.x and
# only ADDS fields to it, so 1.x keeps working:
#   - per-source "enabled" (default true) and "pack_type"
#     ("resourcepack" / "datapack" / "unknown")
#   - per-source "files" (file count, when known) and "version"
#     (Modrinth version number, when known)
#   - "wb_last_build" (ISO timestamp) and "wb_last_output_size" (bytes)
# "enabled" is a workbench-level concept: the 1.x page ignores it and
# builds all sources, exactly as before.

WB_BUILD_MANIFEST = os.path.join(HERE, "packmerge-workbench-build.json")


def wb_downloads_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads")


def wb_output_filename(pack_name):
    name = (pack_name or "").strip() or "packmerge"
    name = re.sub(r'[\\/:*?"<>|]', "-", name).strip() or "packmerge"
    return name + ".zip"


def wb_output_path(pack_name):
    return os.path.join(wb_downloads_dir(), wb_output_filename(pack_name))


def wb_open_output():
    folder = wb_downloads_dir()
    try:
        if sys.platform.startswith("win"):
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def wb_modrinth_slug(value):
    """Modrinth project slug from a full URL or a bare slug, else None."""
    v = (value or "").strip()
    m = re.match(r"https?://(?:www\.)?modrinth\.com/"
                 r"(?:resourcepack|datapack|mod|plugin)/([A-Za-z0-9_-]+)", v)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]+", v):
        return v
    return None


def wb_modrinth_project(slug):
    req = urllib.request.Request(
        MODRINTH_API + "/project/" + urllib.parse.quote(slug),
        headers={"User-Agent": "packmerge-workbench/2.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def wb_modrinth_all_loaders(slug):
    """Every loader across ALL of a project's versions (empty on failure).

    Type determination must scan all versions, not just the newest:
    dual-published projects (e.g. dungeons-and-taverns) ship mod
    versions alongside datapack versions, and the newest version is
    often the mod one. The project endpoint does NOT mark data packs:
    a data pack's project_type is "mod" and its categories lack
    "datapack" (verified live for Terralith and Structory). The
    version loaders are the marker: data-pack versions carry loader
    "datapack", resource-pack versions loader "minecraft"."""
    try:
        req = urllib.request.Request(
            MODRINTH_API + "/project/" + urllib.parse.quote(slug)
            + "/version",
            headers={"User-Agent": "packmerge-workbench/2.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            versions = json.loads(r.read().decode("utf-8"))
    except Exception:
        return set()
    loaders = set()
    for v in versions or []:
        loaders.update(v.get("loaders") or [])
    return loaders


def wb_inspect_zip(path):
    """Return (file_count, pack_type) for a zip, or (None, 'unknown').

    Pack type comes from the zip's own folders: assets/ means a
    resource pack, data/ means a data pack. A pack with both is
    grouped as a resource pack (the engine merges both sides either
    way); a pack with neither is "unknown" until a build says more.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except Exception:
        return None, "unknown"
    files = [n for n in names if not n.endswith("/")]
    segments = set()
    for n in names:
        segments.update(n.split("/"))
    has_assets = "assets" in segments
    has_data = "data" in segments
    if has_assets:
        ptype = "resourcepack"
    elif has_data:
        ptype = "datapack"
    else:
        ptype = "unknown"
    return len(files), ptype


def wb_source_view(i, s):
    if "modrinth" in s:
        kind = "modrinth"
    elif "url" in s:
        kind = "url"
    else:
        kind = "path"
    return {
        "index": i,
        "name": s.get("name") or ("source-%d" % i),
        "kind": kind,
        "pack_type": s.get("pack_type", "unknown"),
        "enabled": s.get("enabled", True),
        "files": s.get("files"),
        "version": s.get("version"),
        "ref": s.get("modrinth") or s.get("url") or s.get("path"),
        "subscribed": bool(s.get("subscribed")),
        "icon_url": s.get("icon_url"),
    }


def wb_state_view():
    st = load_state()
    srcs = st.get("sources", [])
    enabled = [s for s in srcs if s.get("enabled", True)]
    return {
        "pack_name": st.get("pack_name", ""),
        "description": st.get("description", ""),
        "output_filename": wb_output_filename(st.get("pack_name", "")),
        "output_folder": wb_downloads_dir(),
        "sources": [wb_source_view(i, s) for i, s in enumerate(srcs)],
        "included": len(enabled),
        "total": len(srcs),
        # The Discover nav badge counts real subscriptions only, and
        # hides at zero (see the slice-4 frontend).
        "subscription_count": sum(1 for s in srcs if s.get("subscribed")),
        # The merge queue is unified (slice 5): queued pack updates
        # plus pending RPO setting changes, together.
        "queue_count": (len(st.get("wb_queued_updates") or {})
                        + wb_rpo_pending_count(st)),
        "queue_breakdown": {
            "updates": len(st.get("wb_queued_updates") or {}),
            "rpo": wb_rpo_pending_count(st),
        },
        "auto_rebuild": bool(st.get("wb_auto_rebuild", False)),
        "last_build": st.get("wb_last_build"),
        "last_output_size": st.get("wb_last_output_size"),
    }


def wb_add_url(url):
    """Validate + resolve a URL add. Returns (source_dict, error).

    Pack type is never asked for: Modrinth links resolve it from the
    project's type, direct .zip links start as "unknown" and get their
    real type from the downloaded file after the first build.
    """
    url = (url or "").strip()
    if not url:
        return None, "Paste a link first."
    slug = wb_modrinth_slug(url)
    if slug is not None:
        try:
            proj = wb_modrinth_project(slug)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "No Modrinth project found for that link."
            return None, "Could not reach Modrinth (HTTP %s)." % e.code
        except Exception as e:
            return None, "Could not reach Modrinth (%s)." % e
        if proj.get("project_type") == "resourcepack":
            ptype = "resourcepack"
        elif "datapack" in wb_modrinth_all_loaders(slug):
            # Data packs report project_type "mod"; only their version
            # loaders say "datapack" (see wb_modrinth_all_loaders).
            ptype = "datapack"
        else:
            return None, ("That Modrinth project is a %s, not a resource "
                           "or data pack." % (proj.get("project_type")
                                              or "project"))
        return {"name": proj.get("title") or slug, "modrinth": url,
                "pack_type": ptype}, None
    if not re.match(r"https?://", url):
        return None, ("That doesn't look like a link. Paste a Modrinth "
                       "project link or a direct .zip link.")
    path_part = urllib.parse.urlparse(url).path
    if not path_part.lower().endswith(".zip"):
        return None, "That link doesn't point to a .zip file."
    fname = urllib.parse.unquote(
        path_part.rstrip("/").split("/")[-1])[:-4]
    name = re.sub(r"[-_]+", " ", fname).strip() or "Downloaded pack"
    return {"name": name, "url": url, "pack_type": "unknown"}, None


def wb_add_path(path):
    """Validate + describe a local path add. Returns (source_dict, error)."""
    raw = (path or "").strip()
    if not raw:
        return None, "No file selected."
    p = raw if os.path.isabs(raw) else os.path.join(
        HERE, os.path.expanduser(raw))
    if not os.path.isfile(p):
        return None, "Local file not found: %s" % p
    if not p.lower().endswith(".zip"):
        return None, "Local sources must be .zip files."
    count, ptype = wb_inspect_zip(p)
    if count is None:
        return None, "That file is not a valid zip file."
    stem = os.path.basename(p)[:-4]
    return {"name": stem, "path": normalize_path(p),
            "pack_type": ptype, "files": count}, None


def wb_parse_multipart(body, content_type):
    """Parse a multipart/form-data body (stdlib only, byte-exact).

    Returns a list of (field_name, filename_or_None, content_bytes),
    or None when the content type is not multipart/form-data.
    """
    m = re.search(r"multipart/form-data;\s*boundary=(.+)$",
                  content_type or "")
    if not m:
        return None
    boundary = m.group(1).strip().strip('"')
    if not boundary:
        return None
    sep = ("--" + boundary).encode("utf-8", "surrogateescape")
    parts = []
    for chunk in body.split(sep)[1:]:
        if chunk.startswith(b"--"):
            break  # closing boundary; the rest is epilogue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        elif chunk.startswith(b"\n"):
            chunk = chunk[1:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        elif chunk.endswith(b"\n"):
            chunk = chunk[:-1]
        head, found, content = chunk.partition(b"\r\n\r\n")
        if not found:
            head, found, content = chunk.partition(b"\n\n")
            if not found:
                continue
        headers = head.decode("utf-8", "replace")
        name_m = re.search(r'name="([^"]*)"', headers)
        if not name_m:
            continue
        file_m = re.search(r'filename="([^"]*)"', headers)
        parts.append((name_m.group(1),
                      file_m.group(1) if file_m else None,
                      content))
    return parts


def wb_upload_filename(raw):
    """A browser-supplied filename, made safe for the uploads folder."""
    base = os.path.basename((raw or "").replace("\\", "/"))
    base = "".join(c for c in base if c not in '/\\:*?"<>|\x00')
    return base.strip().strip(".")


def wb_sources_upload(body, content_type):
    """Handle a multipart upload of .zip files into packmerge-uploads/.

    Each file becomes a local path source in the slice-1 shape
    (wb_add_path does the validation + zip inspection, so pack_type
    is known at upload time instead of after the first build).
    Non-zips are skipped per file, never failing the whole batch;
    a path already in Sources is a per-file duplicate error, the
    same rule as the slice-1 path add. Returns (payload, status).
    """
    parts = wb_parse_multipart(body, content_type)
    if parts is None:
        return {"ok": False, "error": "Expected a file upload."}, 400
    files = [(fn, data) for (name, fn, data) in parts
             if name == "files" and fn]
    if not files:
        return {"ok": False, "error": "No files were uploaded."}, 400
    os.makedirs(WB_UPLOAD_DIR, exist_ok=True)
    st = load_state()
    srcs = st.get("sources", [])
    results = []
    for raw_name, data in files:
        fname = wb_upload_filename(raw_name)
        if not fname.lower().endswith(".zip"):
            results.append({"name": raw_name or "file",
                            "status": "skipped",
                            "error": "Only .zip files can be added."})
            continue
        target = os.path.join(WB_UPLOAD_DIR, fname)
        # Same name already on disk with different bytes: keep both
        # (don't clobber the earlier upload); identical bytes reuse
        # the file, and the duplicate check below still applies.
        if os.path.isfile(target):
            try:
                with open(target, "rb") as f:
                    same = (hashlib.sha256(f.read()).digest()
                            == hashlib.sha256(data).digest())
            except OSError:
                same = False
            if not same:
                stem, ext = os.path.splitext(fname)
                n = 2
                while os.path.isfile(os.path.join(
                        WB_UPLOAD_DIR, "%s (%d)%s" % (stem, n, ext))):
                    n += 1
                target = os.path.join(
                    WB_UPLOAD_DIR, "%s (%d)%s" % (stem, n, ext))
        if any("path" in s and os.path.normpath(s["path"])
               == os.path.normpath(normalize_path(target))
               for s in srcs):
            results.append({"name": fname, "status": "skipped",
                            "error": "That pack is already in the list."})
            continue
        try:
            with open(target, "wb") as f:
                f.write(data)
        except OSError as e:
            results.append({"name": fname, "status": "skipped",
                            "error": "Could not save the file (%s)." % e})
            continue
        src, err = wb_add_path(target)
        if err:
            try:
                os.remove(target)
            except OSError:
                pass
            results.append({"name": fname, "status": "skipped",
                            "error": err})
            continue
        srcs.append(src)
        results.append({"name": src["name"], "status": "added"})
    st["sources"] = srcs
    save_state(st)
    payload = wb_state_view()
    payload["ok"] = True
    payload["results"] = results
    return payload, 200


def wb_build_manifest(mode="build"):
    """The manifest the engine runs for a workbench build.

    Only enabled sources reach the engine, and only engine-known keys
    are passed (workbench-only keys like "enabled" are stripped).
    Output is an absolute path in the Downloads folder, matching the
    Build card's promise. For build-mode runs, a source with a queued
    update is pinned to the queued version (modrinth_version_id);
    check-mode runs resolve live, as before.
    """
    st = load_state()
    all_srcs = st.get("sources", [])
    enabled_idx = [i for i, s in enumerate(all_srcs)
                   if s.get("enabled", True)]
    enabled = [all_srcs[i] for i in enabled_idx]
    temp_pos = {oi: ti for ti, oi in enumerate(enabled_idx)}
    enabled_names = set()
    for oi, s in zip(enabled_idx, enabled):
        enabled_names.add(s.get("name") or ("source-%d" % oi))
    queued = (st.get("wb_queued_updates") or {}) if mode == "build" else {}
    clean = []
    for s in enabled:
        entry = {k: s[k] for k in ("name", "modrinth", "url", "path",
                                   "pack_type")
                 if k in s}
        # A queued update pins this build to the queued version: the
        # engine otherwise resolves newest at build time, which would
        # make the queue theater (a build would pull whatever is
        # newest regardless of what was queued). The pin is a Modrinth
        # version id, so the lock stays modrinth-shaped.
        slug = (s.get("subscribed_slug")
                or (wb_modrinth_slug(s["modrinth"]) if "modrinth" in s
                    else None))
        if slug and slug in queued:
            entry["modrinth_version_id"] = queued[slug]["new_version_id"]
        clean.append(entry)
    # Translate overrides into the filtered manifest's terms: name refs
    # stay names (the engine resolves a name to the last same-named
    # enabled source); 1.x index refs are full-list positions and are
    # translated to temp indices. Overrides whose winner is disabled
    # or removed are dropped so the build can't crash on an unknown
    # source — the decision stays in state and applies again if the
    # source returns.
    overrides = {}
    for p, ref in (st.get("overrides", {}) or {}).items():
        ref = str(ref)
        if ref in enabled_names:
            overrides[p] = ref
        elif ref.isdigit() and int(ref) in temp_pos:
            overrides[p] = str(temp_pos[int(ref)])
    # RPO baking: the engine bakes EFFECTIVE values as merged-config
    # defaults — baked values overridden by pending ones — for enabled
    # sources only. Passing baked values every build (not just pending
    # ones) is what keeps a baked value in later outputs after pending
    # is consumed; otherwise the next build would revert to the source
    # pack's defaults. Keys are filtered to settings that exist right
    # now, so stale entries (pack changed since) never warn in the log.
    # Pending/renames for disabled/removed sources stay in state and
    # apply again if the source returns.
    pending = st.get("wb_rpo_pending", {}) or {}
    baked = st.get("wb_rpo_baked", {}) or {}
    renames = st.get("wb_rpo_namespaces", {}) or {}
    try:
        _existing = {s["name"]: {t["key"] for t in s["settings"]}
                     for s in wb_rpo_scan()["sources"]}
    except Exception:
        _existing = {}
    rpo_defaults = {}
    for _n in enabled_names:
        _eff = dict(baked.get(_n, {}) or {})
        _eff.update(pending.get(_n, {}) or {})
        if _n in _existing:
            _eff = {k: v for k, v in _eff.items() if k in _existing[_n]}
        if _eff:
            rpo_defaults[_n] = _eff
    return {
        "mc_version": st.get("mc_version", "latest"),
        "pack_name": st.get("pack_name", "") or "packmerge",
        "description": st.get("description", ""),
        "output": wb_output_path(st.get("pack_name", "")),
        "smart_json_merge": st.get("smart_json_merge", True),
        "merge_respackopts": st.get("merge_respackopts", True),
        "sources": clean,
        "overrides": overrides,
        "exclude": st.get("exclude", []),
        "rpo_defaults": rpo_defaults,
        "rpo_namespace_overrides": {n: v for n, v in renames.items()
                                    if n in enabled_names and v},
    }, enabled


def wb_lock_by_name(lock):
    by_name = {}
    for key, val in lock.items():
        _i, _, name = key.partition(":")
        by_name.setdefault(name, val)
    return by_name


def wb_prepare_build(mode="build"):
    """Write the filtered build manifest + translated lock.

    The engine keys its lock by "index:name", so filtering disabled
    sources out would shift every index and break Modrinth update
    tracking. The lock is therefore translated by name: temp indices
    for the run, original indices when writing back.
    Returns (enabled_sources, orig_index_by_name, temp_index_by_orig).
    """
    manifest, enabled = wb_build_manifest(mode)
    with open(WB_BUILD_MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
    real_lock = {}
    try:
        with open(STATE_PATH + ".lock.json") as f:
            real_lock = json.load(f)
    except Exception:
        pass
    by_name = wb_lock_by_name(real_lock)
    temp_lock = {}
    for ti, s in enumerate(enabled):
        name = s.get("name", "source-%d" % ti)
        if name in by_name:
            temp_lock["%d:%s" % (ti, name)] = by_name[name]
    with open(WB_BUILD_MANIFEST + ".lock.json", "w") as f:
        json.dump(temp_lock, f, indent=2)
    st = load_state()
    orig_index = {}
    for i, s in enumerate(st.get("sources", [])):
        orig_index.setdefault(s.get("name", "source-%d" % i), i)
    temp_index = {}
    for ti, s in enumerate(enabled):
        oi = orig_index.get(s.get("name", "source-%d" % ti))
        if oi is not None:
            temp_index[oi] = ti
    return enabled, orig_index, temp_index


def wb_after_build(rc, mode, orig_index, temp_index):
    """Sync engine results back into the shared state (builds only
    update last-build info; failed runs change nothing)."""
    if rc != 0:
        return
    try:
        with open(WB_BUILD_MANIFEST + ".lock.json") as f:
            temp_lock = json.load(f)
    except Exception:
        temp_lock = {}
    real_lock = {}
    try:
        with open(STATE_PATH + ".lock.json") as f:
            real_lock = json.load(f)
    except Exception:
        pass
    by_name = wb_lock_by_name(temp_lock)
    for name, val in by_name.items():
        if name in orig_index:
            real_lock["%d:%s" % (orig_index[name], name)] = val
    try:
        with open(STATE_PATH + ".lock.json", "w") as f:
            json.dump(real_lock, f, indent=2)
    except OSError:
        pass
    st = load_state()
    cache_dir = os.path.join(HERE, ".packmerge-cache")
    queued_slugs = set(st.get("wb_queued_updates") or {})
    for i, s in enumerate(st.get("sources", [])):
        name = s.get("name", "source-%d" % i)
        if "modrinth" in s and name in by_name:
            # A queued update owns this source's displayed version
            # until a build consumes it: a check run resolves live
            # (unpinned) and must not advance the row early. (The
            # lock translation above is untouched — it only drives
            # the engine's change detection, and the pinned build
            # reconciles it.)
            slug = (s.get("subscribed_slug")
                    or (wb_modrinth_slug(s["modrinth"])
                        if "modrinth" in s else None))
            if (slug not in queued_slugs
                    and by_name[name].get("version_number")):
                s["version"] = by_name[name]["version_number"]
        if "path" in s:
            p = s["path"] if os.path.isabs(s["path"]) else os.path.join(
                HERE, os.path.expanduser(s["path"]))
            count, ptype = wb_inspect_zip(p)
            if count is not None:
                s["files"] = count
                if s.get("pack_type") in (None, "unknown"):
                    s["pack_type"] = ptype
        elif s.get("enabled", True) and i in temp_index:
            # engine cache files are named "<temp index>-<filename>"
            ti = temp_index[i]
            try:
                cands = sorted(fn for fn in os.listdir(cache_dir)
                               if fn.startswith("%d-" % ti))
            except OSError:
                cands = []
            if cands:
                count, ptype = wb_inspect_zip(
                    os.path.join(cache_dir, cands[0]))
                if count is not None:
                    s["files"] = count
                    if s.get("pack_type") in (None, "unknown"):
                        s["pack_type"] = ptype
    if mode == "build":
        out_path = wb_output_path(st.get("pack_name", ""))
        if os.path.exists(out_path):
            st["wb_last_build"] = datetime.now(timezone.utc).isoformat()
            st["wb_last_output_size"] = os.path.getsize(out_path)
        # Queued updates are consumed by this build, in the same gate
        # as RPO pending values (successful build-mode runs only;
        # wb_after_build already early-returns on failure, and check
        # runs never reach this block).
    save_state(st)
    # A successful build bakes pending RPO values; they become the new
    # baseline and pending clears. Check runs never consume (nothing
    # was baked), and failed runs change nothing (early return above).
    # Queued updates drain in the same gate, after RPO consumption
    # (which saves state itself, so the queue read below is fresh).
    if mode == "build":
        wb_rpo_consume_pending()
        wb_updates_consume()


def wb_start_run(mode):
    st = load_state()
    enabled = [s for s in st.get("sources", []) if s.get("enabled", True)]
    if not enabled:
        return False, "Add at least one enabled pack first."
    _enabled, orig_index, temp_index = wb_prepare_build(mode)

    def after(rc):
        wb_after_build(rc, mode, orig_index, temp_index)

    ok = start_run(mode, manifest_path=WB_BUILD_MANIFEST, after=after)
    if not ok:
        return False, "A build is already running."
    return True, None


# ------------------------------------------------------------ workbench conflicts (2.0 slice 2)
# The Conflicts tab runs on real scans of the enabled sources. A file
# present in 2+ enabled sources is "contested" and is classified by
# what the engine would do with it (strategies mirror
# packmerge.json_merge_dispatch):
#   - smart-merged automatically: the merge loses nothing (lang key
#     unions with no differing values, tag values unions, sounds
#     events combined with no lossy overlap, splices and deep merges
#     with no scalar conflicts, respackopts namespacing)
#   - needs a decision: the merge would discard something (binary
#     files, differing lang keys, lossy sounds/model/blockstate
#     conflicts, deep scalar conflicts)
#   - settled by list priority: the user explicitly chose priority
#     for the file (state "wb_priority_settled")
#   - decided: the user chose a winner, stored as a normal engine
#     override (state "overrides", by source name) so workbench builds
#     and 1.x builds both honor it.
# Per-key JSON winners are NOT honored by the engine (overrides are
# file-level only); the diff endpoint reports differing keys honestly
# and the page offers file-level winners only. Wiring per-key winners
# needs an engine change (a key_overrides manifest field).

WB_CACHE_DIR = os.path.join(HERE, ".packmerge-cache")
# Drag-dropped zips live here, NOT in the cache: the engine deletes
# stale cache entries, but an uploaded source must survive restarts
# and rebuilds exactly as the user dropped it.
WB_UPLOAD_DIR = os.path.join(HERE, "packmerge-uploads")


def _wb_canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _wb_n(n, one, many):
    return one if n == 1 else many


def wb_merge_kind(path):
    """Merge strategy for a path, mirroring the engine's
    json_merge_dispatch. Returns a strategy name or None for plain
    top-wins."""
    if os.path.basename(path) == "respackopts.json5":
        return "respackopts"
    p = path.lower()
    base = os.path.basename(p)
    if p.endswith(".json") and "/lang/" in p:
        return "lang"
    if p.endswith(".json") and "/items/" in p:
        return "item"
    if p.endswith(".json") and "/models/" in p:
        return "model"
    if p.endswith(".json") and "/blockstates/" in p:
        return "blockstate"
    if base == "sounds.json":
        return "sounds"
    if p.endswith(".json") and "/atlases/" in p:
        return "atlas"
    if p.endswith(".json") and "/font/" in p:
        return "font"
    if p.endswith(".json") and "/particles/" in p:
        return "particle"
    if p.endswith(".mcmeta") or p.endswith(".json"):
        return "deep"
    return None


def wb_is_tag(path):
    parts = path.split("/")
    return (len(parts) >= 4 and parts[0] == "data"
            and parts[2] == "tags" and path.endswith(".json"))


def wb_namespace(path):
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] in ("assets", "data"):
        return parts[1]
    return ""


def wb_zip_names(zpath):
    """{normalized_name: (arcname, file_size)} for a zip, mirroring
    the engine's read_pack_zip top-folder unwrap, without reading
    file contents."""
    with zipfile.ZipFile(zpath) as z:
        infos = [i for i in z.infolist()
                 if not i.filename.endswith("/")
                 and not i.filename.startswith("__MACOSX/")
                 and os.path.basename(i.filename) != ".DS_Store"]
    names = [i.filename for i in infos]
    if "pack.mcmeta" not in names:
        tops = {n.split("/")[0] for n in names if "/" in n}
        if len(tops) == 1:
            top = next(iter(tops))
            if "%s/pack.mcmeta" % top in names:
                return {i.filename[len(top) + 1:]: (i.filename, i.file_size)
                        for i in infos
                        if i.filename.startswith(top + "/")
                        and len(i.filename) > len(top) + 1}
    return {i.filename: (i.filename, i.file_size) for i in infos}


def wb_resolve_one(index):
    """Resolve one enabled source (full-list index) to a local zip.
    Path sources resolve directly; remote sources use the engine cache
    (same naming builds use) and are downloaded on first use.
    Returns (zip_path, error_or_None)."""
    st = load_state()
    srcs = st.get("sources", [])
    if index < 0 or index >= len(srcs):
        return None, "no such source"
    s = srcs[index]
    if not s.get("enabled", True):
        return None, "source is disabled"
    if "path" in s:
        p = s["path"] if os.path.isabs(s["path"]) else os.path.join(
            HERE, os.path.expanduser(s["path"]))
        if not os.path.isfile(p):
            return None, "Local file not found: %s" % p
        return p, None
    enabled = [j for j, t in enumerate(srcs) if t.get("enabled", True)]
    ti = enabled.index(index)
    os.makedirs(WB_CACHE_DIR, exist_ok=True)
    try:
        if "modrinth" in s:
            info = _engine.resolve_modrinth(
                s["modrinth"], st.get("mc_version", "latest"),
                loader=("datapack" if s.get("pack_type") == "datapack"
                        else None))
            fname, url, sha = info["filename"], info["url"], info["sha512"]
        elif "url" in s:
            url = s["url"]
            fname = urllib.parse.unquote(
                url.split("?")[0].rstrip("/").split("/")[-1]) or "pack.zip"
            sha = None
        else:
            return None, "source has no path, url, or modrinth"
    except Exception as e:
        return None, "Could not resolve source: %s" % str(e)[:160]
    zpath = os.path.join(WB_CACHE_DIR, "%d-%s" % (ti, fname))
    if not os.path.exists(zpath):
        try:
            _engine.download_file(url, zpath, sha)
        except Exception as e:
            return None, "Could not download source: %s" % str(e)[:160]
    return zpath, None


def wb_scan_sources():
    """Resolve all enabled sources. Returns (resolved, unscanned);
    resolved entries are {index, name, zip, names}."""
    st = load_state()
    srcs = st.get("sources", [])
    resolved, unscanned = [], []
    for i, s in enumerate(srcs):
        if not s.get("enabled", True):
            continue
        name = s.get("name") or ("source-%d" % i)
        zpath, err = wb_resolve_one(i)
        if err:
            unscanned.append({"index": i, "name": name, "error": err})
            continue
        try:
            names = wb_zip_names(zpath)
        except Exception as e:
            unscanned.append({"index": i, "name": name,
                              "error": "Could not read zip: %s"
                                       % str(e)[:160]})
            continue
        resolved.append({"index": i, "name": name, "zip": zpath,
                         "names": names})
    return resolved, unscanned


def wb_read_entry(res, norm_path):
    arc = res["names"].get(norm_path, (None, None))[0]
    if arc is None:
        return None
    try:
        with zipfile.ZipFile(res["zip"]) as z:
            return z.read(arc)
    except Exception:
        return None


def wb_read_prefix(res, norm_path, n):
    arc = res["names"].get(norm_path, (None, None))[0]
    if arc is None:
        return None
    try:
        with zipfile.ZipFile(res["zip"]) as z:
            with z.open(arc) as f:
                return f.read(n)
    except Exception:
        return None


def wb_png_size(res, norm_path):
    """(width, height) for a PNG party, or None."""
    head = wb_read_prefix(res, norm_path, 33)
    if not head or len(head) < 24 or head[12:16] != b"IHDR":
        return None
    return [int.from_bytes(head[16:20], "big"),
            int.from_bytes(head[20:24], "big")]


def wb_scalar_conflicts(indexed, lists_atomic=False):
    """[{key, values}] for scalar disagreements across indexed
    [(source_index, parsed_json)] in priority order. Dicts recurse
    per key; lists union losslessly unless lists_atomic, in which
    case any canonical list difference is a conflict. Missing keys
    are not conflicts."""
    found = []

    def walk(pairs, path):
        vals = [v for _i, v in pairs]
        if all(isinstance(v, dict) for v in vals):
            keys = set()
            for v in vals:
                keys.update(v.keys())
            for k in sorted(keys):
                sub = [(i, v[k]) for i, v in pairs if k in v]
                if len(sub) >= 2:
                    walk(sub, "%s.%s" % (path, k) if path else str(k))
        elif all(isinstance(v, list) for v in vals):
            if lists_atomic and len({_wb_canon(v) for v in vals}) > 1:
                found.append({"key": path or "value",
                              "values": {str(i): v for i, v in pairs}})
        else:
            if len({_wb_canon(v) for v in vals}) > 1:
                found.append({"key": path or "value",
                              "values": {str(i): v for i, v in pairs}})

    walk(indexed, "")
    return found


def wb_lang_analysis(indexed):
    """(differing, total_keys) for lang files. differing entries are
    {key, values} with values keyed by source index (as strings)."""
    keys = set()
    for _i, v in indexed:
        if isinstance(v, dict):
            keys.update(v.keys())
    differing = []
    for k in sorted(keys):
        present = [(i, v[k]) for i, v in indexed
                   if isinstance(v, dict) and k in v]
        if len({_wb_canon(v) for _i, v in present}) > 1:
            differing.append({"key": k,
                              "values": {str(i): v for i, v in present}})
    return differing, len(keys)


def wb_sounds_analysis(indexed):
    """(lossy_events, total_events) for sounds.json. An event is
    lossy when a higher version sets replace:true, a version isn't a
    dict, or a shared non-'sounds' key differs. Sounds-list
    differences union losslessly and are not lossy."""
    events = set()
    for _i, v in indexed:
        if isinstance(v, dict):
            events.update(v.keys())
    lossy = []
    for ev in sorted(events):
        present = [(i, v[ev]) for i, v in indexed
                   if isinstance(v, dict) and ev in v]
        if len(present) < 2:
            continue
        bad = False
        if not all(isinstance(d, dict) for _i, d in present):
            if len({_wb_canon(d) for _i, d in present}) > 1:
                bad = True
        if not bad:
            for _i, d in present[1:]:
                if isinstance(d, dict) and d.get("replace"):
                    bad = True
                    break
        if not bad:
            keys = set()
            for _i, d in present:
                keys.update(k for k in d.keys() if k != "sounds")
            for k in keys:
                vals = [(i, d[k]) for i, d in present
                        if isinstance(d, dict) and k in d]
                if len({_wb_canon(v) for _i, v in vals}) > 1:
                    bad = True
                    break
        if bad:
            lossy.append({"key": ev,
                          "values": {str(i): d for i, d in present}})
    return lossy, len(events)


def wb_model_analysis(indexed):
    """Predicate collisions + scalar conflicts in non-overrides
    keys. Returns [{key, values}]."""
    conflicts = []
    preds = {}
    for i, v in indexed:
        if not isinstance(v, dict):
            continue
        for e in v.get("overrides") or []:
            if isinstance(e, dict):
                preds.setdefault(_wb_canon(e.get("predicate", {})),
                                 []).append((i, e.get("model")))
    for pred, lst in sorted(preds.items()):
        if len({_wb_canon(m) for _i, m in lst}) > 1:
            conflicts.append({"key": "overrides[%s]" % pred,
                              "values": {str(i): m for i, m in lst}})
    rests = [({k: v for k, v in val.items() if k != "overrides"}
              if isinstance(val, dict) else val)
             for _i, val in indexed]
    conflicts.extend(wb_scalar_conflicts(
        [(i, r) for (i, _v), r in zip(indexed, rests)]))
    return conflicts


def wb_blockstate_analysis(indexed):
    """Variant-key disagreements + scalar conflicts elsewhere
    (multipart lists concatenate losslessly)."""
    conflicts = []
    variants = {}
    for i, v in indexed:
        if isinstance(v, dict) and isinstance(v.get("variants"), dict):
            for k, val in v["variants"].items():
                variants.setdefault(k, []).append((i, val))
    for k, lst in sorted(variants.items()):
        if len({_wb_canon(v) for _i, v in lst}) > 1:
            conflicts.append({"key": "variants.%s" % k,
                              "values": {str(i): v for i, v in lst}})
    rests = [({k: v for k, v in val.items()
               if k not in ("variants", "multipart")}
              if isinstance(val, dict) else val)
             for _i, val in indexed]
    conflicts.extend(wb_scalar_conflicts(
        [(i, r) for (i, _v), r in zip(indexed, rests)]))
    return conflicts


def wb_analyze_json(path, indexed):
    """indexed: [(source_index, parsed_json)] in priority order.
    Returns {merge_kind, lossy, differing, detail, total}. differing
    entries are {key, values} with values keyed by source index."""
    kind = wb_merge_kind(path)
    if kind == "respackopts":
        return {"merge_kind": kind, "lossy": False, "differing": [],
                "detail": "configs namespaced by the RPO merger",
                "total": None}
    if kind == "lang":
        differing, total = wb_lang_analysis(indexed)
        return {"merge_kind": kind, "lossy": bool(differing),
                "differing": differing,
                "detail": ("%d keys unioned" % total) if not differing
                          else ("%d keys unioned, %d %s differ"
                                % (total, len(differing),
                                   _wb_n(len(differing), "key", "keys"))),
                "total": total}
    if kind == "sounds":
        differing, total = wb_sounds_analysis(indexed)
        return {"merge_kind": kind, "lossy": bool(differing),
                "differing": differing,
                "detail": ("%d sound events combined" % total)
                          if not differing else
                          ("%d sound events combined, %d conflict"
                           % (total, len(differing))),
                "total": total}
    if kind == "model":
        differing = wb_model_analysis(indexed)
        return {"merge_kind": kind, "lossy": bool(differing),
                "differing": differing,
                "detail": "overrides spliced, no collisions"
                          if not differing else
                          "%d %s differ" % (len(differing),
                                            _wb_n(len(differing),
                                                  "property", "properties")),
                "total": None}
    if kind == "blockstate":
        differing = wb_blockstate_analysis(indexed)
        return {"merge_kind": kind, "lossy": bool(differing),
                "differing": differing,
                "detail": "variants spliced, no collisions"
                          if not differing else
                          "%d %s differ" % (len(differing),
                                            _wb_n(len(differing),
                                                  "property", "properties")),
                "total": None}
    if kind in ("atlas", "font", "particle"):
        concat = {"atlas": "sources", "font": "providers",
                  "particle": "textures"}[kind]
        rests = [({k: v for k, v in val.items() if k != concat}
                  if isinstance(val, dict) else val)
                 for _i, val in indexed]
        differing = wb_scalar_conflicts(
            [(i, r) for (i, _v), r in zip(indexed, rests)])
        return {"merge_kind": kind, "lossy": bool(differing),
                "differing": differing,
                "detail": "%s combined" % concat if not differing else
                          "%d %s differ" % (len(differing),
                                            _wb_n(len(differing),
                                                  "property", "properties")),
                "total": None}
    if kind == "item":
        # Conservative: the engine's item-definition splicing keys
        # entries by threshold/when, so any canonical list difference
        # is treated as a conflict rather than risking silent loss.
        differing = wb_scalar_conflicts(indexed, lists_atomic=True)
        return {"merge_kind": kind, "lossy": bool(differing),
                "differing": differing,
                "detail": "entries combined, no conflicts"
                          if not differing else
                          "%d %s differ" % (len(differing),
                                            _wb_n(len(differing),
                                                  "property", "properties")),
                "total": None}
    # deep (other JSON/.mcmeta) and tags: lists union losslessly.
    differing = wb_scalar_conflicts(indexed)
    if wb_is_tag(path):
        vals = set()
        for _i, v in indexed:
            if isinstance(v, dict) and isinstance(v.get("values"), list):
                vals.update(_wb_canon(x) for x in v["values"])
        detail = ("values unioned, %d total" % len(vals)) if not differing \
            else "%d %s differ" % (len(differing),
                                   _wb_n(len(differing), "value", "values"))
        return {"merge_kind": "tag", "lossy": bool(differing),
                "differing": differing, "detail": detail,
                "total": len(vals)}
    return {"merge_kind": kind, "lossy": bool(differing),
            "differing": differing,
            "detail": "merged with no conflicting values"
                      if not differing else
                      "%d %s differ" % (len(differing),
                                            _wb_n(len(differing),
                                                  "value", "values")),
            "total": None}


def _wb_conflict_row(path, parties_res, base_status, merge_kind, detail,
                     differ_count):
    parties = []
    for res in parties_res:
        _arc, size = res["names"][path]
        p = {"index": res["index"], "name": res["name"], "size": size}
        if path.lower().endswith(".png"):
            dims = wb_png_size(res, path)
            if dims:
                p["dimensions"] = dims
        parties.append(p)
    return {
        "path": path,
        "kind": "json" if path.lower().endswith((".json", ".mcmeta"))
                else "texture",
        "tag": wb_is_tag(path),
        "namespace": wb_namespace(path),
        "status": base_status,
        "merge_kind": merge_kind,
        "detail": detail,
        "parties": parties,
        "priority_winner": parties[-1],
        "winner": None,
        "winner_name": None,
        "differ_count": differ_count or 0,
    }


def wb_scan_conflicts():
    """Contested-file report for the enabled sources."""
    st = load_state()
    overrides = st.get("overrides", {}) or {}
    settled = set(st.get("wb_priority_settled", []) or [])
    resolved, unscanned = wb_scan_sources()
    by_path = {}
    for res in resolved:
        for norm in res["names"]:
            if norm in ("pack.mcmeta", "credits.txt"):
                continue
            by_path.setdefault(norm, []).append(res)
    conflicts = []
    for path in sorted(by_path):
        parties = by_path[path]
        if len(parties) < 2:
            continue
        datas = [wb_read_entry(res, path) for res in parties]
        if any(d is None for d in datas):
            conflicts.append(_wb_conflict_row(
                path, parties, "open", None,
                "could not read from some sources", None))
            continue
        if all(d == datas[0] for d in datas):
            continue  # byte-identical: nothing to decide, nothing lost
        is_json = path.lower().endswith((".json", ".mcmeta"))
        if not is_json:
            conflicts.append(_wb_conflict_row(
                path, parties, "open", None, "binary files differ", None))
            continue
        parsed, bad = [], False
        for d in datas:
            try:
                parsed.append(json.loads(d.decode("utf-8")))
            except Exception:
                bad = True
                parsed.append(None)
        if bad:
            conflicts.append(_wb_conflict_row(
                path, parties, "open", None,
                "not valid JSON in every source — top-wins applies", None))
            continue
        if len({_wb_canon(p) for p in parsed}) == 1:
            continue  # same JSON, different bytes only
        indexed = [(res["index"], p) for res, p in zip(parties, parsed)]
        analysis = wb_analyze_json(path, indexed)
        if analysis["lossy"]:
            conflicts.append(_wb_conflict_row(
                path, parties, "open", analysis["merge_kind"],
                analysis["detail"], len(analysis["differing"])))
        else:
            conflicts.append(_wb_conflict_row(
                path, parties, "smart", analysis["merge_kind"],
                analysis["detail"], 0))
    for c in conflicts:
        if c["path"] in overrides:
            c["status"] = "decided"
            c["winner_name"] = str(overrides[c["path"]])
            same = [p for p in c["parties"]
                    if p["name"] == c["winner_name"]]
            c["winner"] = same[-1] if same else None
        elif c["path"] in settled:
            c["status"] = "priority"
    counts = {"open": 0, "smart": 0, "priority": 0, "decided": 0,
              "vanilla": 0}
    for c in conflicts:
        counts[c["status"]] += 1
        if c["namespace"] == "minecraft":
            counts["vanilla"] += 1
    rules = []
    for c in conflicts:
        if c["status"] == "decided":
            rules.append({"path": c["path"], "method": "winner",
                          "winner": c["winner_name"]})
        elif c["status"] == "priority":
            rules.append({"path": c["path"], "method": "priority",
                          "winner": None})
    return {
        "sources": [{"index": r["index"], "name": r["name"]}
                    for r in resolved],
        "unscanned": unscanned,
        "complete": not unscanned,
        "counts": counts,
        "conflicts": conflicts,
        "rules": rules,
    }


def wb_conflict_diff(path):
    """(payload, error, status) for a contested JSON file."""
    if not path or path.startswith("/") or ".." in path.split("/"):
        return None, "invalid path", 400
    resolved, _unscanned = wb_scan_sources()
    parties = [r for r in resolved if path in r["names"]]
    if len(parties) < 2:
        return None, "not a contested file", 404
    if not path.lower().endswith((".json", ".mcmeta")):
        return None, "not a JSON file", 400
    parsed = []
    for res in parties:
        d = wb_read_entry(res, path)
        if d is None:
            return None, "could not read file from a source", 502
        try:
            parsed.append(json.loads(d.decode("utf-8")))
        except Exception:
            return None, "not valid JSON in every source", 400
    if len({_wb_canon(p) for p in parsed}) == 1:
        return None, "no differences", 404
    indexed = [(res["index"], p) for res, p in zip(parties, parsed)]
    analysis = wb_analyze_json(path, indexed)
    st = load_state()
    overrides = st.get("overrides", {}) or {}
    settled = set(st.get("wb_priority_settled", []) or [])
    if path in overrides:
        status = "decided"
    elif path in settled:
        status = "priority"
    elif analysis["lossy"]:
        status = "open"
    else:
        status = "smart"

    def trim(v):
        s = json.dumps(v, ensure_ascii=False)
        return v if len(s) <= 1000 else s[:1000] + "…"

    differing = [{"key": d["key"],
                  "values": {k: trim(v) for k, v in d["values"].items()}}
                 for d in analysis["differing"][:200]]
    return {
        "path": path,
        "kind": "json",
        "merge_kind": analysis["merge_kind"],
        "tag": wb_is_tag(path),
        "namespace": wb_namespace(path),
        "status": status,
        "parties": [{"index": res["index"], "name": res["name"]}
                    for res in parties],
        "priority_winner": {"index": parties[-1]["index"],
                            "name": parties[-1]["name"]},
        "differing": differing,
        "differ_count": len(analysis["differing"]),
        "truncated": len(analysis["differing"]) > 200,
        "detail": analysis["detail"],
        "total": analysis["total"],
    }, None, 200


def wb_mutate_conflict(path, action, winner_index=None):
    """Validate + apply a conflict mutation. Returns
    (report, error, status). Actions: decide | clear | priority |
    priority-all | rules-all. Decisions are stored as engine
    overrides (by source name); priority settlements in
    wb_priority_settled. Both persist in shared state."""
    report = wb_scan_conflicts()
    st = load_state()
    if action in ("priority-all", "rules-all"):
        if action == "priority-all":
            settled = set(st.get("wb_priority_settled", []) or [])
            for c in report["conflicts"]:
                if c["status"] == "open":
                    settled.add(c["path"])
            st["wb_priority_settled"] = sorted(settled)
        else:
            overrides = st.get("overrides", {}) or {}
            for c in report["conflicts"]:
                if c["status"] == "open":
                    overrides[c["path"]] = c["priority_winner"]["name"]
            st["overrides"] = overrides
        save_state(st)
        return wb_scan_conflicts(), None, 200
    conf = next((c for c in report["conflicts"] if c["path"] == path),
                None)
    if conf is None:
        return None, "not a contested file", 404
    overrides = st.get("overrides", {}) or {}
    settled = set(st.get("wb_priority_settled", []) or [])
    if action == "decide":
        if winner_index is None:
            return None, "winner is required", 400
        party = next((p for p in conf["parties"]
                      if p["index"] == winner_index), None)
        if party is None:
            return None, "winner does not contain this file", 400
        # The engine resolves a name to the LAST same-named enabled
        # source; refuse when that would pick a different source than
        # the one the user chose.
        all_enabled = [(i, s) for i, s in enumerate(st.get("sources", []))
                       if s.get("enabled", True)]
        last_same = None
        for i, s in all_enabled:
            if (s.get("name") or ("source-%d" % i)) == party["name"]:
                last_same = i
        if last_same != winner_index:
            return None, ("That source shares its name with another "
                           "source; rename it first to decide this "
                           "file."), 400
        overrides[path] = party["name"]
        settled.discard(path)
    elif action == "clear":
        overrides.pop(path, None)
        settled.discard(path)
    elif action == "priority":
        overrides.pop(path, None)
        settled.add(path)
    else:
        return None, "unknown action", 400
    st["overrides"] = overrides
    st["wb_priority_settled"] = sorted(settled)
    save_state(st)
    return wb_scan_conflicts(), None, 200


# ------------------------------------------------------------ workbench RPO (2.0 slice 3)
# The RPO Toggles tab runs on real configs from the enabled sources.
# A source's respackopts.json5 declares entries (switch/enum/slider);
# labels come from its lang files (rpo.<id>.<path>[.tooltip]) with the
# key itself as fallback. Changes are PENDING until the next build:
# they never rewrite the source packs, only the merged pack's baked
# defaults (via manifest rpo_defaults), and a successful build consumes
# them into wb_rpo_baked. Namespaces are the merged config's per-source
# categories; a rename is a manifest rpo_namespace_overrides entry.
# Pending/renames are keyed by source NAME (same convention as slice 2
# decisions); an ambiguous name is refused, never guessed.

def wb_rpo_pending_count(st=None):
    """Pending RPO changes, counted as settings. Set-time discipline
    (a value equal to its baseline drops its entry) and build-time
    consumption keep this exact without scanning any zip."""
    st = st if st is not None else load_state()
    n = 0
    for vals in (st.get("wb_rpo_pending", {}) or {}).values():
        if isinstance(vals, dict):
            n += len(vals)
    return n


def _wb_rpo_eq(a, b):
    """Type-strict equality: True != 1, but 1 == 1.0 for numbers."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def wb_rpo_entry_at(conf, dotted):
    """The entry node at a dotted path, or None for unknown paths and
    categories."""
    if not isinstance(conf, dict):
        return None
    node = conf
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    if isinstance(node, dict) and "type" not in node:
        return None  # a category, not an entry
    return node


def wb_rpo_entry_view(entry):
    """Normalize one entry to UI shape. Returns None for categories
    and values that carry no usable type."""
    if isinstance(entry, dict) and "type" in entry:
        t = str(entry.get("type", "string"))
        default = entry.get("default")
        values = entry.get("values")
        if values is None:
            values = entry.get("options")
        mn = entry.get("min", entry.get("minimum"))
        mx = entry.get("max", entry.get("maximum"))
        step = entry.get("step")
        unit = entry.get("unit")
    elif isinstance(entry, bool):
        t, default = "boolean", entry
        values, mn, mx, step, unit = None, None, None, None, None
    elif isinstance(entry, int):
        t, default = "integer", entry
        values, mn, mx, step, unit = None, None, None, None, None
    elif isinstance(entry, float):
        t, default = "number", entry
        values, mn, mx, step, unit = None, None, None, None, None
    elif isinstance(entry, str):
        t, default = "string", entry
        values, mn, mx, step, unit = None, None, None, None, None
    else:
        return None
    if t == "boolean":
        ui = "switch"
    elif values is not None:
        ui = "dropdown"
    elif t in ("integer", "number"):
        ui = "slider"
    elif t == "string":
        ui = "text"
    else:
        ui = "unsupported"
    view = {"type": t, "ui": ui, "default": default}
    if values is not None:
        view["values"] = values if isinstance(values, list) else [values]
    if mn is not None:
        view["min"] = mn
    if mx is not None:
        view["max"] = mx
    if step is not None:
        view["step"] = step
    if unit is not None:
        view["unit"] = unit
    return view


def wb_rpo_validate(view, value):
    """Error string for an invalid value, else None."""
    t, ui = view["type"], view["ui"]
    if ui == "unsupported":
        return "this setting type is not editable here"
    if t == "boolean":
        if not isinstance(value, bool):
            return "expected true or false"
        return None
    if ui == "dropdown":
        for v in view["values"]:
            if _wb_rpo_eq(v, value):
                return None
        return "value is not one of the allowed options"
    if t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "expected a whole number"
        if view.get("min") is not None and value < view["min"]:
            return "value is below the minimum (%s)" % view["min"]
        if view.get("max") is not None and value > view["max"]:
            return "value is above the maximum (%s)" % view["max"]
        return None
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "expected a number"
        if view.get("min") is not None and value < view["min"]:
            return "value is below the minimum (%s)" % view["min"]
        if view.get("max") is not None and value > view["max"]:
            return "value is above the maximum (%s)" % view["max"]
        return None
    if t == "string":
        if not isinstance(value, str):
            return "expected text"
        return None
    return None


def wb_rpo_scan():
    """Full RPO report for enabled sources. Sources without a parseable
    respackopts.json5 simply don't appear."""
    st = load_state()
    pending = st.get("wb_rpo_pending", {}) or {}
    baked = st.get("wb_rpo_baked", {}) or {}
    renames = st.get("wb_rpo_namespaces", {}) or {}
    resolved, unscanned = wb_scan_sources()
    parsed = {}  # full-list index -> (res, cfg)
    for res in resolved:
        try:
            with zipfile.ZipFile(res["zip"]) as z:
                arc = res["names"].get("respackopts.json5")
                if arc is None:
                    continue
                cfg = _engine.json5_loads(z.read(arc[0]).decode("utf-8"))
        except Exception:
            continue
        if isinstance(cfg, dict):
            parsed[res["index"]] = (res, cfg)
    # Natural categories in source order, derived exactly like the
    # engine's, then renames. Keyed by NAME like the engine: a later
    # same-named source overwrites, which is why set/rename refuse
    # ambiguous names instead of guessing.
    natural = {}
    for idx in sorted(parsed):
        name = parsed[idx][0]["name"]
        base = _engine.sanitize_rpo_id(name)
        cat, k = base, 0
        while cat in natural.values():
            k += 1
            cat = base + chr(ord("A") + k - 1)
        natural[name] = cat

    def namespace_of(name):
        if name in renames and renames[name]:
            return renames[name]
        return natural.get(name)

    sources_out, namespaces_out = [], []
    total_settings = total_gated = 0
    switches_on = switches_total = 0
    pending_count = 0
    for idx in sorted(parsed):
        res, cfg = parsed[idx]
        name = res["name"]
        ns = namespace_of(name)
        sid = cfg.get("id", "") or ""
        labels = {}
        try:
            with zipfile.ZipFile(res["zip"]) as z:
                for norm, (arc, _sz) in res["names"].items():
                    if not norm.endswith(".json"):
                        continue
                    if sid and (norm.startswith("assets/%s/lang/" % sid)
                                or norm.startswith("assets/%s/lang/" % sid.lower())):
                        try:
                            data = json.loads(z.read(arc).decode("utf-8"))
                        except Exception:
                            continue
                        if isinstance(data, dict):
                            labels.update(data)
        except Exception:
            pass
        # .rpo files: condition string -> governed files.
        rpo_files = []  # (norm, condition, samples, governed_count)
        try:
            with zipfile.ZipFile(res["zip"]) as z:
                for norm, (arc, _sz) in res["names"].items():
                    if not norm.endswith(".rpo"):
                        continue
                    try:
                        rpo = _engine.json5_loads(
                            z.read(arc).decode("utf-8"))
                    except Exception:
                        continue
                    cond = rpo.get("condition")
                    if not isinstance(cond, str):
                        continue
                    if os.path.basename(norm) == ".rpo":
                        d = os.path.dirname(norm)
                        if d:
                            gov = [p for p in res["names"]
                                   if p.startswith(d + "/")
                                   and not p.endswith(".rpo")]
                        else:
                            gov = [p for p in res["names"]
                                   if "/" not in p
                                   and not p.endswith(".rpo")]
                        rpo_files.append((norm, cond, gov[:3], len(gov)))
                    else:
                        rpo_files.append((norm, cond, [norm[:-4]], 1))
        except Exception:
            pass
        conf = cfg.get("conf", {}) if isinstance(cfg.get("conf", {}), dict) else {}
        settings = []
        gated_total = 0
        for _path in _engine.collect_entry_paths(conf):
            dotted = ".".join(_path)
            node = wb_rpo_entry_at(conf, dotted)
            if node is None:
                continue
            view = wb_rpo_entry_view(node)
            if view is None:
                continue
            default = view["default"]
            baseline = (baked.get(name, {}) or {}).get(dotted, default)
            pend = pending.get(name, {}) or {}
            if dotted in pend:
                current = pend[dotted]
                is_pending = not _wb_rpo_eq(current, baseline)
            else:
                current = baseline
                is_pending = False
            if is_pending:
                pending_count += 1
            conds = []
            for norm, cond, samples, gcount in rpo_files:
                if re.search(r"(?<![A-Za-z0-9_.])" + re.escape(dotted) +
                             r"(?![A-Za-z0-9_])", cond):
                    conds.append({"rpo": norm, "condition": cond,
                                  "governed": samples,
                                  "governed_count": gcount})
                    gated_total += gcount
            label = labels.get("rpo.%s.%s" % (sid, dotted)) if sid else None
            tooltip = labels.get(
                "rpo.%s.%s.tooltip" % (sid, dotted)) if sid else None
            item = dict(view)
            item.update({
                "key": dotted,
                "label": label if isinstance(label, str)
                         else dotted.split(".")[-1],
                "tooltip": tooltip if isinstance(tooltip, str) else "",
                "value": current,
                "pending": is_pending,
                "conditions": conds,
                "files_gated": sum(c["governed_count"] for c in conds),
            })
            settings.append(item)
            total_settings += 1
            if view["ui"] == "switch":
                switches_total += 1
                if current is True:
                    switches_on += 1
        total_gated += gated_total
        sources_out.append({
            "index": idx, "name": name, "namespace": ns,
            "natural_namespace": natural.get(name),
            "config_id": sid, "settings": settings,
            "files_gated": gated_total,
        })
        namespaces_out.append({
            "source": name, "namespace": ns,
            "natural": natural.get(name),
            "entries": len(settings), "files_gated": gated_total,
        })
    return {
        "sources": sources_out,
        "namespaces": namespaces_out,
        "unscanned": unscanned,
        "stats": {"settings": total_settings,
                  "configs": len(sources_out),
                  "files_gated": total_gated,
                  "switches_on": switches_on,
                  "switches_total": switches_total},
        "pending_count": pending_count,
    }


def wb_rpo_matching_sources(source_name):
    """Full-list indices of enabled sources with this name."""
    st = load_state()
    srcs = st.get("sources", [])
    return [i for i, s in enumerate(srcs)
            if s.get("enabled", True)
            and (s.get("name") or ("source-%d" % i)) == source_name]


def _wb_rpo_name_exists(source_name):
    """True if any source (enabled or not) carries this name."""
    st = load_state()
    return any((s.get("name") or ("source-%d" % i)) == source_name
               for i, s in enumerate(st.get("sources", [])))


def wb_rpo_set(source_name, key, value):
    """Set a pending value. Returns (payload, error, status)."""
    matches = wb_rpo_matching_sources(source_name)
    if not matches:
        if _wb_rpo_name_exists(source_name):
            return None, ("source is disabled; enable it first to change "
                           "its settings"), 404
        return None, "unknown source", 404
    if len(matches) > 1:
        return None, ("That source shares its name with another source; "
                       "rename it first to change this setting."), 400
    report = wb_rpo_scan()
    entry_src = next((s for s in report["sources"]
                      if s["name"] == source_name), None)
    if entry_src is None:
        return None, "source has no Respackopts settings", 404
    item = next((t for t in entry_src["settings"] if t["key"] == key), None)
    if item is None:
        return None, "unknown setting", 404
    err = wb_rpo_validate(item, value)
    if err:
        return None, err, 400
    st = load_state()
    baked = st.get("wb_rpo_baked", {}) or {}
    baseline = (baked.get(source_name, {}) or {}).get(key, item["default"])
    pending = st.setdefault("wb_rpo_pending", {})
    if _wb_rpo_eq(value, baseline):
        if source_name in pending:
            pending[source_name].pop(key, None)
            if not pending[source_name]:
                del pending[source_name]
    else:
        pending.setdefault(source_name, {})[key] = value
    save_state(st)
    return {"ok": True, "report": wb_rpo_scan()}, None, 200


def wb_rpo_discard():
    """Drop all pending values (baked values and renames untouched)."""
    st = load_state()
    st["wb_rpo_pending"] = {}
    save_state(st)
    return {"ok": True, "report": wb_rpo_scan()}


def wb_rpo_rename(source_name, namespace):
    """Rename a pack's RPO namespace. Returns (payload, error, status)."""
    ns = (namespace or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_]+", ns or ""):
        return None, ("Namespaces use lowercase letters, numbers, and "
                       "underscores only."), 400
    matches = wb_rpo_matching_sources(source_name)
    if not matches:
        if _wb_rpo_name_exists(source_name):
            return None, ("source is disabled; enable it first to rename "
                           "its namespace"), 404
        return None, "unknown source", 404
    if len(matches) > 1:
        return None, ("That source shares its name with another source; "
                       "rename it first to rename this namespace."), 400
    report = wb_rpo_scan()
    entry_src = next((s for s in report["sources"]
                      if s["name"] == source_name), None)
    if entry_src is None:
        return None, "source has no Respackopts config", 404
    others = [n["namespace"] for n in report["namespaces"]
              if n["source"] != source_name]
    if ns in others:
        return None, "That namespace is already used by another config.", 400
    st = load_state()
    renames = st.setdefault("wb_rpo_namespaces", {})
    # Input is force-lowercased (demo validation), while natural
    # categories are camelCase ("rPOAlpha"), so compare lowercased:
    # typing the natural name in any case removes the override.
    if ns == (entry_src["natural_namespace"] or "").lower():
        renames.pop(source_name, None)
    else:
        renames[source_name] = ns
    save_state(st)
    return {"ok": True, "report": wb_rpo_scan()}, None, 200


def wb_rpo_consume_pending():
    """After a successful workbench build, pending values are baked:
    they become the new baseline and pending clears. Best-effort: on
    any scan failure pending is left intact rather than lost."""
    try:
        report = wb_rpo_scan()
    except Exception:
        return
    st = load_state()
    baked = {}
    for src in report["sources"]:
        vals = {}
        for t in src["settings"]:
            vals[t["key"]] = t["value"]
        if vals:
            baked[src["name"]] = vals
    st["wb_rpo_baked"] = baked
    st["wb_rpo_pending"] = {}
    save_state(st)


# ------------------------------------------------------------ workbench discover (2.0 slice 4)
# The Discover tab runs on live Modrinth search. Two facts about
# Modrinth's API shape this code (both verified live, not assumed):
#   - Data packs are NOT project_type:datapack. On Modrinth a data pack
#     is a project of type "mod" carrying the "datapack" category
#     (Terralith's project_type is "mod"). So the "data packs" facet
#     is categories:datapack, and "all pack types" is the OR group
#     [project_type:resourcepack, categories:datapack]. A
#     project_type:datapack facet matches nothing.
#   - Shaders are excluded at the facet level: neither facet group can
#     match a shader project. On top of that, a query mentioning
#     "shader" returns no results at all — live Modrinth returns ~300
#     shader-mentioning resource packs for that query, which would
#     mislead a user looking for shaders in a tab that carries none.

WB_DISCOVER_LIMIT = 20
WB_DISCOVER_SORTS = ("relevance", "downloads", "follows", "newest",
                     "updated")


def _wb_discover_facets(pack_type, category, version):
    if pack_type == "resourcepack":
        type_group = ["project_type:resourcepack"]
    elif pack_type == "datapack":
        type_group = ["categories:datapack"]
    else:
        type_group = ["project_type:resourcepack", "categories:datapack"]
    facets = [type_group]
    if category:
        facets.append(["categories:" + category])
    if version:
        facets.append(["versions:" + version])
    return facets


def wb_discover_search(qs):
    """Live Modrinth search. qs is the parsed query dict. Returns
    (payload, error, status)."""
    query = (qs.get("query") or [""])[0]
    if len(query) > 200:
        return None, "search query is too long", 400
    pack_type = {"all": "all", "RP": "resourcepack", "DP": "datapack",
                 "resourcepack": "resourcepack",
                 "datapack": "datapack"}.get(
                     (qs.get("pack_type") or ["all"])[0])
    if pack_type is None:
        return None, "unknown pack_type", 400
    category = (qs.get("category") or ["all"])[0]
    if category == "all":
        category = ""
    elif not re.fullmatch(r"[a-z0-9_-]+", category):
        return None, "unknown category", 400
    version = (qs.get("version") or ["all"])[0]
    if version == "all":
        version = ""
    elif not re.fullmatch(r"[A-Za-z0-9._-]+", version):
        return None, "unknown version", 400
    sort = (qs.get("sort") or ["relevance"])[0]
    if sort not in WB_DISCOVER_SORTS:
        sort = "relevance"
    offset_raw = (qs.get("offset") or ["0"])[0]
    if not re.fullmatch(r"[0-9]+", offset_raw):
        return None, "unknown offset", 400
    offset = int(offset_raw)
    if offset > 100000:
        return None, "unknown offset", 400
    q = query.strip()
    if "shader" in q.lower():
        # Discover carries no shaders (see the header comment); a
        # shader-seeking query has no valid results here.
        return {"ok": True, "total": 0, "results": [],
                "shader_query": True}, None, 200
    facets = _wb_discover_facets(pack_type, category, version)
    url = (MODRINTH_API + "/search?query="
           + urllib.parse.quote(q)
           + "&facets=" + urllib.parse.quote(json.dumps(facets))
           + "&index=" + sort
           + "&offset=" + str(offset)
           + "&limit=" + str(WB_DISCOVER_LIMIT))
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "packmerge-workbench/2.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return None, "Modrinth search failed (HTTP %s)." % e.code, 502
    except Exception as e:
        return None, "Could not reach Modrinth (%s)." % e, 502
    st = load_state()
    subscribed_slugs, known_slugs = set(), set()
    for s in st.get("sources", []):
        if "modrinth" in s:
            slug = wb_modrinth_slug(s["modrinth"])
            if slug:
                known_slugs.add(slug)
                if s.get("subscribed"):
                    subscribed_slugs.add(slug)
    results = []
    for h in data.get("hits", []):
        cats = h.get("categories") or []
        if "datapack" in cats:
            ptype = "datapack"
        elif h.get("project_type") == "resourcepack":
            ptype = "resourcepack"
        else:
            continue  # facets should prevent this; never surface it
        slug = h.get("slug") or ""
        results.append({
            "slug": slug,
            "title": h.get("title") or slug,
            "author": h.get("author") or "",
            "description": h.get("description") or "",
            "pack_type": ptype,
            "categories": cats,
            "versions": h.get("versions") or [],
            "downloads": h.get("downloads") or 0,
            "follows": h.get("follows") or 0,
            "icon_url": h.get("icon_url"),
            "date_modified": h.get("date_modified"),
            "subscribed": slug in subscribed_slugs,
            "in_sources": slug in known_slugs,
        })
    return {"ok": True, "total": data.get("total_hits", len(results)),
            "offset": offset,
            "results": results}, None, 200


def wb_discover_download(index, info):
    """Download a resolved Modrinth file into the scan cache.
    Returns (downloaded_bool, error_or_None, zip_path_or_None).
    Mirrors wb_resolve_one's cache naming so scans and builds reuse
    the file."""
    st = load_state()
    srcs = st.get("sources", [])
    if index < 0 or index >= len(srcs):
        return False, "no such source", None
    enabled = [j for j, t in enumerate(srcs) if t.get("enabled", True)]
    if index not in enabled:
        return False, "source is disabled", None
    ti = enabled.index(index)
    try:
        os.makedirs(WB_CACHE_DIR, exist_ok=True)
        zpath = os.path.join(WB_CACHE_DIR, "%d-%s" % (ti, info["filename"]))
        if not os.path.exists(zpath):
            _engine.download_file(info["url"], zpath, info["sha512"])
    except Exception as e:
        return False, str(e)[:160], None
    return True, None, zpath


def wb_discover_subscribe(slug):
    """Subscribe: resolve + download + add (or flag) a Modrinth pack.
    Returns (payload, error, status). The download is best-effort: if
    it fails the source is still added and the next build retries —
    the response says so honestly instead of failing the subscribe."""
    slug = (slug or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        return None, "Provide a Modrinth project slug.", 400
    try:
        proj = wb_modrinth_project(slug)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "No Modrinth project found for that link.", 404
        return None, "Could not reach Modrinth (HTTP %s)." % e.code, 502
    except Exception as e:
        return None, "Could not reach Modrinth (%s)." % e, 502
    if proj.get("project_type") == "resourcepack":
        ptype = "resourcepack"
    elif "datapack" in wb_modrinth_all_loaders(slug):
        ptype = "datapack"
    else:
        return None, ("That Modrinth project is a %s, not a resource "
                      "or data pack." % (proj.get("project_type")
                                         or "project")), 400
    st = load_state()
    srcs = st.get("sources", [])
    idx, existing = None, None
    for i, s in enumerate(srcs):
        if "modrinth" in s and wb_modrinth_slug(s["modrinth"]) == slug:
            idx, existing = i, s
            break
    if existing is not None and existing.get("subscribed"):
        return {"ok": True, "already": True}, None, 200
    try:
        info = _engine.resolve_modrinth(
            slug, st.get("mc_version", "latest"),
            loader=("datapack" if ptype == "datapack" else None))
    except Exception as e:
        return None, "Could not resolve that pack (%s)." % e, 502
    title = proj.get("title") or slug
    if existing is None:
        srcs.append({"name": title,
                     "modrinth": "https://modrinth.com/%s/%s" % (ptype,
                                                                slug),
                     "pack_type": ptype,
                     "subscribed": True,
                     "subscribed_slug": slug,
                     "subscribed_version": info["version_number"],
                     "version": info["version_number"],
                     "icon_url": proj.get("icon_url")})
        idx = len(srcs) - 1
    else:
        # Already a source (added via URL): subscribing flags it for
        # update checks without duplicating it. The name/ref stay as
        # the user set them.
        existing["subscribed"] = True
        existing["subscribed_slug"] = slug
        existing["subscribed_version"] = info["version_number"]
        existing["version"] = info["version_number"]
        existing["icon_url"] = proj.get("icon_url")
    save_state(st)
    downloaded, dl_err, zpath = wb_discover_download(idx, info)
    if downloaded and zpath:
        # Fill the file count now from the downloaded zip, so the
        # Sources row shows it immediately instead of a dash.
        count, _ptype = wb_inspect_zip(zpath)
        if count is not None:
            st2 = load_state()
            for s in st2.get("sources", []):
                if (s.get("subscribed_slug") == slug or
                        ("modrinth" in s and
                         wb_modrinth_slug(s["modrinth"]) == slug)):
                    s["files"] = count
                    break
            save_state(st2)
    return {"ok": True, "already": False, "downloaded": downloaded,
            "download_error": dl_err}, None, 200


def wb_discover_unsubscribe(slug):
    """Unsubscribe: remove the subscribed source. Overrides and
    excludes are left alone (same rule as clear-all): they stay in
    state and apply again if the source returns."""
    slug = (slug or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", slug):
        return None, "Provide a Modrinth project slug.", 400
    st = load_state()
    srcs = st.get("sources", [])
    for i, s in enumerate(srcs):
        if not s.get("subscribed"):
            continue
        if s.get("subscribed_slug") == slug or (
                "modrinth" in s
                and wb_modrinth_slug(s["modrinth"]) == slug):
            srcs.pop(i)
            save_state(st)
            return {"ok": True}, None, 200
    # Idempotent: a stale card (source already removed, e.g. by Clear
    # all) unsubscribes harmlessly instead of erroring, so the card
    # flips to Subscribe like a normal success.
    return {"ok": True, "already": False}, None, 200


def wb_sources_backfill_icons():
    """Fill in Modrinth icon URLs for subscribed sources that lack one
    (subscribed before icons were stored at subscribe time). Runs once
    on demand from the frontend and persists, so later loads skip
    filled sources. A failure for one source is skipped, never fails
    the call; sources that already have an icon_url are untouched.
    Never called from the state view or on every load."""
    st = load_state()
    srcs = st.get("sources", [])
    filled, skipped = [], []
    changed = False
    for s in srcs:
        if not s.get("subscribed") or s.get("icon_url"):
            continue
        slug = s.get("subscribed_slug") or (
            wb_modrinth_slug(s["modrinth"]) if "modrinth" in s else None)
        name = s.get("name") or slug or "unnamed"
        if not slug:
            skipped.append(name)
            continue
        try:
            req = urllib.request.Request(
                MODRINTH_API + "/project/" + urllib.parse.quote(slug),
                headers={"User-Agent": "packmerge-workbench/2.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                icon = json.loads(r.read().decode("utf-8")).get("icon_url")
        except Exception:
            skipped.append(name)
            continue
        if not icon:
            skipped.append(name)
            continue
        s["icon_url"] = icon
        filled.append(name)
        changed = True
    if changed:
        save_state(st)
    return {"ok": True, "filled": filled, "skipped": skipped}


def wb_sources_backfill_meta():
    """Fill version/files for existing sources without re-subscribing
    or building. Version copies from subscribed_version when known;
    files come from the cached download when one exists (same
    prefix-match the build fill uses). Fully offline — never
    downloads. Sources with no cache file keep their dashes until
    the next build fills them."""
    st = load_state()
    srcs = st.get("sources", [])
    enabled = [j for j, t in enumerate(srcs) if t.get("enabled", True)]
    filled_version, filled_files = [], []
    changed = False
    for i, s in enumerate(srcs):
        name = s.get("name") or ("source-%d" % i)
        if s.get("version") is None and s.get("subscribed_version"):
            s["version"] = s["subscribed_version"]
            filled_version.append(name)
            changed = True
        if s.get("files") is None:
            count = None
            if "path" in s:
                p = s["path"] if os.path.isabs(s["path"]) else os.path.join(
                    HERE, os.path.expanduser(s["path"]))
                count, _pt = wb_inspect_zip(p)
            elif i in enabled:
                ti = enabled.index(i)
                try:
                    cands = sorted(
                        fn for fn in os.listdir(WB_CACHE_DIR)
                        if fn.startswith("%d-" % ti))
                except OSError:
                    cands = []
                if cands:
                    count, _pt = wb_inspect_zip(
                        os.path.join(WB_CACHE_DIR, cands[0]))
            if count is not None:
                s["files"] = count
                filled_files.append(name)
                changed = True
    if changed:
        save_state(st)
    return {"ok": True, "filled_version": filled_version,
            "filled_files": filled_files}


# ------------------------------------------------------------ workbench updates (2.0 slice 5)
# The Activity tab runs on real Modrinth update checks. The state
# keys are all workbench-only (1.x ignores them):
#   wb_updates_available: dict slug -> update entry, REBUILT fresh on
#       each check. A slug with a queued update is excluded from
#       available entirely (its update is already in hand); the next
#       check after consumption finds anything newer still.
#   wb_queued_updates: dict slug -> queued entry (old vs new version,
#       new version id, download path, file count). Queued entries
#       persist across restarts and are consumed by a successful
#       build-mode run (see wb_updates_consume).
#   wb_last_update_check: ISO timestamp of the last check, or None.
#   wb_auto_rebuild: bool, the one switch mirrored on the Build card
#       and the Activity tab.
# Version ordering never compares version_number strings: "6.0.2" vs
# "6.0.2+mod" does not order reliably. The newest pack-loader version
# is compared to the subscribed version by date_published (the
# subscribed version is looked up in the same version list by
# number); only when it cannot be found there does the check fall
# back to "different number means newer".


def wb_pack_loader(pack_type):
    """The Modrinth loader that marks a pack version: datapack for
    data packs, minecraft for resource packs. None for unknown types
    (either loader counts). Same rule as fixup 3's resolution."""
    if pack_type == "datapack":
        return "datapack"
    if pack_type == "resourcepack":
        return "minecraft"
    return None


def wb_updates_cache_size():
    """Real size of the scan/build cache dir, or None if it cannot
    be walked (the UI then shows an honest dash)."""
    total = 0
    try:
        for root, _dirs, files in os.walk(WB_CACHE_DIR):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
    except OSError:
        return None
    return total


def wb_updates_view():
    """Pure state read: available + queued updates, badge count,
    last check, cache size, switch state. No Modrinth calls."""
    st = load_state()
    avail = st.get("wb_updates_available") or {}
    queued = st.get("wb_queued_updates") or {}
    by_name = lambda e: (e.get("name") or e.get("slug") or "").lower()
    return {
        "ok": True,
        "auto_rebuild": bool(st.get("wb_auto_rebuild", False)),
        "available": sorted(avail.values(), key=by_name),
        "queued": sorted(queued.values(), key=by_name),
        # Badge definition: updates waiting for action = available
        # updates not yet queued/applied PLUS queued updates still
        # awaiting a build. Hidden at zero. A queued update still
        # needs a build, so it still counts.
        "badge": len(avail) + len(queued),
        "last_check": st.get("wb_last_update_check"),
        "cache_size": wb_updates_cache_size(),
    }


def wb_updates_check():
    """Run a real update check over every enabled subscribed source
    and rebuild wb_updates_available from scratch. Per-source fetch
    failures skip that source without failing the whole check (the
    response carries an errors list so the UI can say so)."""
    st = load_state()
    queued = st.get("wb_queued_updates") or {}
    available = {}
    errors = []
    for s in st.get("sources", []):
        if not s.get("subscribed") or not s.get("enabled", True):
            continue
        slug = (s.get("subscribed_slug")
                or (wb_modrinth_slug(s["modrinth"])
                    if "modrinth" in s else None))
        if not slug or slug in queued:
            continue
        try:
            versions = _engine.api_get(
                "%s/project/%s/version" % (_engine.MODRINTH_API, slug))
        except Exception as e:
            errors.append({"slug": slug, "error": str(e)})
            continue
        loader = wb_pack_loader(s.get("pack_type"))
        if loader is None:
            cands = [v for v in versions
                     if "datapack" in (v.get("loaders") or [])
                     or "minecraft" in (v.get("loaders") or [])]
        else:
            cands = [v for v in versions
                     if loader in (v.get("loaders") or [])]
        if not cands:
            continue
        cands.sort(key=lambda v: v.get("date_published", ""), reverse=True)
        newest = cands[0]
        sub_num = s.get("subscribed_version")
        sub = None
        for v in cands:
            if v.get("version_number") == sub_num:
                sub = v
                break
        if sub is not None:
            is_newer = (newest.get("date_published", "")
                        > sub.get("date_published", ""))
        else:
            is_newer = newest.get("version_number") != sub_num
        if not is_newer:
            continue
        available[slug] = {
            "slug": slug,
            "name": s.get("name") or slug,
            "old_version": sub_num,
            "new_version": newest.get("version_number"),
            "new_version_id": newest.get("id"),
            "new_date": newest.get("date_published"),
            "changelog": (newest.get("changelog") or "")[:4000],
            "icon_url": s.get("icon_url"),
            "pack_type": s.get("pack_type"),
        }
    st["wb_updates_available"] = available
    st["wb_last_update_check"] = datetime.now(timezone.utc).isoformat()
    save_state(st)
    view = wb_updates_view()
    view["errors"] = errors
    return view


def wb_updates_download_version(slug, version_id):
    """Best-effort download of one specific Modrinth version into the
    queue area of the cache. Returns (filename, path, file_count,
    error): path/count are None and error is set when the download
    could not happen, but the caller still queues the pin — the
    build resolves the pinned version itself, so a failed apply-time
    download costs one retry at build time, not a lost update."""
    try:
        ver = _engine.api_get("%s/version/%s"
                              % (_engine.MODRINTH_API, version_id))
        files = ver.get("files") or []
        if not files:
            return None, None, None, "That version has no files."
        f = next((x for x in files if x.get("primary")), files[0])
        dest = os.path.join(WB_CACHE_DIR,
                            "queued-%s-%s" % (slug, f["filename"]))
        os.makedirs(WB_CACHE_DIR, exist_ok=True)
        _engine.download_file(f["url"], dest,
                              expected_sha512=f.get("hashes", {}).get("sha512"))
        count, _ptype = wb_inspect_zip(dest)
        return f["filename"], dest, count, None
    except Exception as e:
        return None, None, None, str(e)


def _wb_updates_apply_one(st, slug):
    """Queue one available update in the given (unsaved) state.
    Returns ("ok" | "already" | "noupdate", entry-or-None). Never
    raises for a download failure: the pin is what the build needs,
    and the entry records the failure honestly."""
    queued = st.setdefault("wb_queued_updates", {})
    if slug in queued:
        return "already", queued[slug]
    avail = st.get("wb_updates_available") or {}
    entry = avail.get(slug)
    if not entry:
        return "noupdate", None
    _fn, path, count, err = wb_updates_download_version(
        slug, entry["new_version_id"])
    queued[slug] = {
        "slug": slug,
        "name": entry["name"],
        "old_version": entry["old_version"],
        "new_version": entry["new_version"],
        "new_version_id": entry["new_version_id"],
        "changelog": entry.get("changelog", ""),
        "icon_url": entry.get("icon_url"),
        "pack_type": entry.get("pack_type"),
        "file_count": count,
        "downloaded": path is not None,
        "download_error": err,
        "downloaded_path": path,
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    avail.pop(slug, None)
    return "ok", queued[slug]


def wb_updates_apply(slug):
    """Apply one update: queue it (auto OFF) or queue + build (auto
    ON). Returns (payload, http_status)."""
    st = load_state()
    status, _entry = _wb_updates_apply_one(st, slug)
    if status == "noupdate":
        return {"ok": False,
                "error": "No update available for that pack."}, 404
    save_state(st)
    build_started = False
    if st.get("wb_auto_rebuild", False):
        ok, _err = wb_start_run("build")
        build_started = ok
    view = wb_updates_view()
    view["already"] = (status == "already")
    view["build_started"] = build_started
    return view, 200


def wb_updates_apply_all():
    """Queue (or queue + one build, auto ON) every available update.
    Per-item failures are collected, never aborting the rest."""
    st = load_state()
    avail = st.get("wb_updates_available") or {}
    applied, already, failed = [], [], []
    for slug in list(avail.keys()):
        try:
            status, _entry = _wb_updates_apply_one(st, slug)
        except Exception as e:
            failed.append({"slug": slug, "error": str(e)})
            continue
        if status == "ok":
            applied.append(slug)
        elif status == "already":
            already.append(slug)
        else:
            failed.append({"slug": slug,
                           "error": "No update available for that pack."})
    save_state(st)
    build_started = False
    if applied and st.get("wb_auto_rebuild", False):
        ok, _err = wb_start_run("build")
        build_started = ok
    view = wb_updates_view()
    view["applied"] = applied
    view["already"] = already
    view["failed"] = failed
    view["build_started"] = build_started
    return view, 200


def wb_updates_dequeue(slug):
    """Remove a queued update and put it back in available, so the
    update waits again instead of vanishing until the next check.
    Idempotent: dequeuing a non-queued slug is a no-op success."""
    st = load_state()
    queued = st.get("wb_queued_updates") or {}
    entry = queued.pop(slug, None)
    if entry is None:
        return {"ok": True, "removed": False}, 200
    path = entry.get("downloaded_path")
    if path:
        try:
            os.remove(path)
        except OSError:
            pass
    avail = st.setdefault("wb_updates_available", {})
    avail[slug] = {
        "slug": slug,
        "name": entry["name"],
        "old_version": entry["old_version"],
        "new_version": entry["new_version"],
        "new_version_id": entry["new_version_id"],
        "new_date": None,
        "changelog": entry.get("changelog", ""),
        "icon_url": entry.get("icon_url"),
        "pack_type": entry.get("pack_type"),
    }
    save_state(st)
    view = wb_updates_view()
    view["removed"] = True
    return view, 200


def wb_updates_auto(on):
    """Set the one Auto Rebuild switch. Turning it ON with a
    non-empty queue triggers a build run (the queued items merge).
    Turning it OFF triggers nothing. Returns (payload, status)."""
    st = load_state()
    st["wb_auto_rebuild"] = bool(on)
    save_state(st)
    build_started = False
    if on and st.get("wb_queued_updates"):
        ok, _err = wb_start_run("build")
        build_started = ok
    view = wb_updates_view()
    view["auto_rebuild"] = bool(on)
    view["build_started"] = build_started
    return view, 200


def wb_updates_consume():
    """Drain queued updates after a successful build-mode run (called
    from wb_after_build's build-mode gate, after RPO consumption).
    The pinned build already merged the new version, so consumption
    only updates the source record: subscribed_version/version become
    the new version, files from the queued count when known, and the
    queued download file is deleted. Entries for removed sources are
    dropped (they can never merge); entries for disabled sources are
    kept (they resume if the source is re-enabled)."""
    st = load_state()
    queued = st.get("wb_queued_updates") or {}
    if not queued:
        return
    by_slug = {}
    for s in st.get("sources", []):
        slug = (s.get("subscribed_slug")
                or (wb_modrinth_slug(s["modrinth"])
                    if "modrinth" in s else None))
        if slug:
            by_slug[slug] = s
    changed = False
    for slug, q in list(queued.items()):
        s = by_slug.get(slug)
        if s is None:
            del queued[slug]
            changed = True
            continue
        if not s.get("enabled", True):
            continue
        s["subscribed_version"] = q["new_version"]
        s["version"] = q["new_version"]
        if q.get("file_count") is not None:
            s["files"] = q["file_count"]
        path = q.get("downloaded_path")
        if path:
            try:
                os.remove(path)
            except OSError:
                pass
        del queued[slug]
        changed = True
    if changed:
        save_state(st)


def wb_watch_loop():
    """Filesystem watch mode, polling mtimes every 5s. Only runs
    while Auto Rebuild is ON (the switch is labeled "Watch for
    changes", and this is the watch half of it). While OFF the mtime
    map is re-baselined each loop, so enabling later never fires for
    old changes. Only local path sources are watched; a new path
    joins silently, a deleted path drops silently (it must never
    loop-fail builds), and a change seen while a build is already
    running is NOT advanced in the map, so it retriggers after the
    run finishes. Builds never modify source files, so a triggered
    build cannot retrigger itself. Every iteration is guarded so a
    watch failure can never crash the server."""
    mtimes = {}
    while True:
        try:
            time.sleep(5)
            st = load_state()
            if not st.get("wb_auto_rebuild", False):
                mtimes = {}
                for s in st.get("sources", []):
                    p = s.get("path")
                    if p and os.path.isfile(p):
                        try:
                            mtimes[p] = os.path.getmtime(p)
                        except OSError:
                            pass
                continue
            changed = []
            for s in st.get("sources", []):
                if not s.get("enabled", True):
                    continue
                p = s.get("path")
                if not p:
                    continue
                try:
                    cur = os.path.getmtime(p)
                except OSError:
                    mtimes.pop(p, None)
                    continue
                if p in mtimes and mtimes[p] != cur:
                    changed.append(p)
                elif p not in mtimes:
                    mtimes[p] = cur
            if changed:
                with run_lock:
                    busy = run_info["running"]
                if not busy:
                    ok, _err = wb_start_run("build")
                    if ok:
                        for p in changed:
                            try:
                                mtimes[p] = os.path.getmtime(p)
                            except OSError:
                                mtimes.pop(p, None)
                # Busy or failed start: the map keeps the old values,
                # so the change retriggers on a later loop.
        except Exception:
            continue


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "packmerge-web/1.0"

    def log_message(self, *args):
        pass  # quiet

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _icon(self):
        path = os.path.join(HERE, "icon.png")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _custom(self, name, content_type):
        """Serve an optional user overlay file (custom.css / custom.js).

        These are never touched by the updater: the user's own tweaks live
        here, so packmerge_web.py itself can stay pristine and keep updating.
        Returns 204 when the file doesn't exist.
        """
        path = os.path.join(HERE, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def _wb_html(self):
        path = os.path.join(HERE, "packmerge_workbench.html")
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            return self._html()
        if parsed.path == "/workbench":
            return self._wb_html()
        if parsed.path in ("/icon.png", "/favicon.ico"):
            return self._icon()
        if parsed.path == "/custom.css":
            return self._custom("custom.css", "text/css; charset=utf-8")
        if parsed.path == "/custom.js":
            return self._custom("custom.js", "text/javascript; charset=utf-8")
        if parsed.path == "/api/state":
            return self._json(load_state())
        if parsed.path == "/api/wb/state":
            return self._json(wb_state_view())
        if parsed.path == "/api/wb/updates":
            return self._json(wb_updates_view())
        if parsed.path == "/api/wb/open-output":
            return self._json(wb_open_output())
        if parsed.path == "/api/log":
            with run_lock:
                return self._json({
                    "lines": list(run_info["lines"]),
                    "done": run_info["done"],
                    "rc": run_info["rc"],
                    "running": run_info["running"],
                })
        if parsed.path == "/api/browse":
            return self._json(browse(qs.get("path", [""])[0]))
        if parsed.path == "/api/normalize-path":
            return self._json({"path": normalize_path(qs.get("path", [""])[0])})
        if parsed.path == "/api/open-output":
            return self._json(open_output_folder())
        if parsed.path == "/api/wb/conflicts":
            return self._json(wb_scan_conflicts())
        if parsed.path == "/api/wb/rpo":
            return self._json(wb_rpo_scan())
        if parsed.path == "/api/wb/discover/search":
            payload, err, status = wb_discover_search(qs)
            if err:
                return self._json({"ok": False, "error": err}, status)
            return self._json(payload)
        if parsed.path == "/api/wb/conflicts/diff":
            q = urllib.parse.parse_qs(parsed.query)
            path = (q.get("path") or [""])[0]
            payload, err, status = wb_conflict_diff(path)
            if err:
                return self._json({"ok": False, "error": err}, status)
            return self._json(payload)
        if parsed.path == "/api/wb/file":
            q = urllib.parse.parse_qs(parsed.query)
            try:
                sidx = int((q.get("source") or [""])[0])
            except ValueError:
                return self._json({"ok": False,
                                   "error": "invalid source"}, 400)
            fpath = (q.get("path") or [""])[0]
            if (not fpath or fpath.startswith("/")
                    or ".." in fpath.split("/")):
                return self._json({"ok": False,
                                   "error": "invalid path"}, 400)
            resolved, _u = wb_scan_sources()
            res = next((r for r in resolved if r["index"] == sidx), None)
            if res is None:
                return self._json({"ok": False,
                                   "error": "source not available"}, 404)
            if fpath not in res["names"]:
                return self._json({"ok": False,
                                   "error": "file not in source"}, 404)
            data = wb_read_entry(res, fpath)
            if data is None:
                return self._json({"ok": False,
                                   "error": "could not read file"}, 502)
            ext = fpath.lower().rsplit(".", 1)[-1] if "." in fpath else ""
            ctype = {".png": "image/png", ".jpg": "image/jpeg",
                     ".jpeg": "image/jpeg", ".ogg": "audio/ogg",
                     ".json": "application/json",
                     ".mcmeta": "application/json",
                     ".txt": "text/plain",
                     ".properties": "text/plain",
                     ".lang": "text/plain"}.get("." + ext,
                                                 "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        if parsed.path == "/api/state":
            try:
                save_state(json.loads(body))
            except Exception:
                return self._json({"ok": False}, 400)
            return self._json({"ok": True})
        if parsed.path == "/api/run":
            try:
                mode = json.loads(body).get("mode", "build")
            except Exception:
                mode = "build"
            ok = start_run(mode if mode in ("build", "check") else "build")
            return self._json({"ok": ok})
        if parsed.path == "/api/app-update":
            return self._app_update(body)
        if parsed.path == "/api/wb/state":
            try:
                payload = json.loads(body)
            except Exception:
                return self._json({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return self._json({"ok": False, "error": "invalid JSON"}, 400)
            st = load_state()
            if "pack_name" in payload:
                st["pack_name"] = str(payload["pack_name"])[:200]
            if "description" in payload:
                st["description"] = str(payload["description"])[:1000]
            if not st.get("output"):
                # Keep the shared state valid for the 1.x page: the
                # engine treats an empty output as "the app folder
                # itself" and crashes replacing it. Mirror 1.x's
                # auto-output (name + ".zip"). A deliberately set 1.x
                # output is never overwritten.
                st["output"] = wb_output_filename(st.get("pack_name", ""))
            save_state(st)
            return self._json(wb_state_view())
        if parsed.path == "/api/wb/sources/upload":
            # multipart upload, not JSON: the body is raw bytes, so the
            # route must come before the JSON-only sources add below.
            if length > 1024 * 1024 * 1024:
                return self._json({"ok": False,
                                   "error": "Upload too large."}, 413)
            payload, status = wb_sources_upload(
                body, self.headers.get("Content-Type", ""))
            return self._json(payload, status)
        if parsed.path == "/api/wb/sources":
            try:
                payload = json.loads(body)
            except Exception:
                return self._json({"ok": False, "error": "invalid JSON"}, 400)
            if not isinstance(payload, dict):
                return self._json({"ok": False, "error": "invalid JSON"}, 400)
            st = load_state()
            src, err = None, None
            if "url" in payload:
                src, err = wb_add_url(payload.get("url"))
                if src is not None:
                    if "modrinth" in src:
                        slug = wb_modrinth_slug(payload.get("url"))
                        dup = any("modrinth" in s
                                  and wb_modrinth_slug(s["modrinth"]) == slug
                                  for s in st.get("sources", []))
                    else:
                        dup = any(s.get("url") == src["url"]
                                  for s in st.get("sources", []))
                    if dup:
                        src, err = None, "That pack is already in the list."
            elif "path" in payload:
                src, err = wb_add_path(payload.get("path"))
                if src is not None:
                    dup = any("path" in s
                              and os.path.normpath(s["path"])
                              == os.path.normpath(src["path"])
                              for s in st.get("sources", []))
                    if dup:
                        src, err = None, "That pack is already in the list."
            else:
                err = "Provide a url or a path."
            if err:
                return self._json({"ok": False, "error": err}, 400)
            st["sources"] = st.get("sources", []) + [src]
            save_state(st)
            return self._json(wb_state_view())
        if parsed.path == "/api/wb/sources/reorder":
            try:
                order = json.loads(body).get("order")
            except Exception:
                order = None
            st = load_state()
            srcs = st.get("sources", [])
            if (not isinstance(order, list)
                    or sorted(order) != list(range(len(srcs)))):
                return self._json(
                    {"ok": False,
                     "error": "order must list every source index once"}, 400)
            st["sources"] = [srcs[i] for i in order]
            save_state(st)
            return self._json(wb_state_view())
        m = re.match(r"^/api/wb/sources/(\d+)/enabled$", parsed.path)
        if m:
            try:
                enabled = json.loads(body).get("enabled")
            except Exception:
                enabled = None
            if not isinstance(enabled, bool):
                return self._json(
                    {"ok": False, "error": "enabled must be true or false"},
                    400)
            st = load_state()
            idx = int(m.group(1))
            srcs = st.get("sources", [])
            if idx < 0 or idx >= len(srcs):
                return self._json({"ok": False, "error": "no such source"},
                                  404)
            srcs[idx]["enabled"] = enabled
            save_state(st)
            return self._json(wb_state_view())
        if parsed.path == "/api/wb/run":
            try:
                mode = json.loads(body).get("mode", "build")
            except Exception:
                mode = "build"
            if mode not in ("build", "check"):
                mode = "build"
            ok, err = wb_start_run(mode)
            if not ok:
                return self._json({"ok": False, "error": err}, 409)
            return self._json({"ok": True})
        if parsed.path == "/api/wb/conflicts/priority-all":
            report, err, status = wb_mutate_conflict("", "priority-all")
            if err:
                return self._json({"ok": False, "error": err}, status)
            return self._json(report)
        if parsed.path == "/api/wb/conflicts/rules-all":
            report, err, status = wb_mutate_conflict("", "rules-all")
            if err:
                return self._json({"ok": False, "error": err}, status)
            return self._json(report)
        m = re.match(r"^/api/wb/conflicts/(decide|clear|priority)$",
                     parsed.path)
        if m:
            try:
                data = json.loads(body)
            except Exception:
                return self._json({"ok": False,
                                   "error": "invalid JSON"}, 400)
            if not isinstance(data, dict) or not data.get("path"):
                return self._json({"ok": False,
                                   "error": "path is required"}, 400)
            winner = data.get("winner")
            if winner is not None and not isinstance(winner, int):
                return self._json({"ok": False,
                                   "error": "winner must be an index"},
                                  400)
            report, err, status = wb_mutate_conflict(
                data["path"], m.group(1), winner)
            if err:
                return self._json({"ok": False, "error": err}, status)
            return self._json(report)
        if parsed.path == "/api/wb/rpo/discard":
            return self._json(wb_rpo_discard())
        if parsed.path == "/api/wb/rpo/set":
            try:
                data = json.loads(body)
            except Exception:
                return self._json({"ok": False,
                                   "error": "invalid JSON"}, 400)
            if not isinstance(data, dict) or not data.get("source") \
                    or not data.get("setting") or "value" not in data:
                return self._json(
                    {"ok": False,
                     "error": "source, setting, and value are required"},
                    400)
            payload, err, status = wb_rpo_set(
                data["source"], data["setting"], data["value"])
            if err:
                return self._json({"ok": False, "error": err}, status)
            return self._json(payload)
        if parsed.path == "/api/wb/rpo/namespace":
            try:
                data = json.loads(body)
            except Exception:
                return self._json({"ok": False,
                                   "error": "invalid JSON"}, 400)
            if not isinstance(data, dict) or not data.get("source") \
                    or "namespace" not in data:
                return self._json(
                    {"ok": False,
                     "error": "source and namespace are required"}, 400)
            payload, err, status = wb_rpo_rename(
                data["source"], data["namespace"])
            if err:
                return self._json({"ok": False, "error": err}, status)
            return self._json(payload)
        if parsed.path in ("/api/wb/discover/subscribe",
                           "/api/wb/discover/unsubscribe"):
            try:
                data = json.loads(body)
            except Exception:
                return self._json({"ok": False,
                                   "error": "invalid JSON"}, 400)
            if not isinstance(data, dict) or not data.get("slug"):
                return self._json({"ok": False,
                                   "error": "slug is required"}, 400)
            if parsed.path.endswith("/subscribe"):
                payload, err, status = wb_discover_subscribe(data["slug"])
            else:
                payload, err, status = wb_discover_unsubscribe(data["slug"])
            if err:
                return self._json({"ok": False, "error": err}, status)
            return self._json(payload)
        if parsed.path == "/api/wb/updates/check":
            return self._json(wb_updates_check())
        if parsed.path == "/api/wb/updates/apply":
            try:
                slug = json.loads(body).get("slug", "")
            except Exception:
                slug = ""
            if not slug:
                return self._json({"ok": False,
                                   "error": "Missing pack."}, 400)
            payload, status = wb_updates_apply(slug)
            return self._json(payload, status)
        if parsed.path == "/api/wb/updates/apply-all":
            payload, status = wb_updates_apply_all()
            return self._json(payload, status)
        if parsed.path == "/api/wb/updates/dequeue":
            try:
                slug = json.loads(body).get("slug", "")
            except Exception:
                slug = ""
            if not slug:
                return self._json({"ok": False,
                                   "error": "Missing pack."}, 400)
            payload, status = wb_updates_dequeue(slug)
            return self._json(payload, status)
        if parsed.path == "/api/wb/updates/auto":
            try:
                on = bool(json.loads(body).get("on", False))
            except Exception:
                on = False
            payload, status = wb_updates_auto(on)
            return self._json(payload, status)
        if parsed.path == "/api/wb/sources/backfill-icons":
            return self._json(wb_sources_backfill_icons())
        if parsed.path == "/api/wb/sources/backfill-meta":
            return self._json(wb_sources_backfill_meta())
        self.send_error(404)

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        m = re.match(r"^/api/wb/sources/(\d+)$", parsed.path)
        if m:
            st = load_state()
            idx = int(m.group(1))
            srcs = st.get("sources", [])
            if idx < 0 or idx >= len(srcs):
                return self._json({"ok": False, "error": "no such source"},
                                  404)
            srcs.pop(idx)
            save_state(st)
            return self._json(wb_state_view())
        if parsed.path == "/api/wb/sources":
            st = load_state()
            st["sources"] = []
            save_state(st)
            return self._json(wb_state_view())
        self.send_error(404)

    def _app_update(self, body):
        """Check for / install packmerge app updates (never touches packs)."""
        try:
            import packmerge_update
        except ImportError:
            return self._json({"ok": False,
                               "error": "packmerge_update.py is missing."})
        if not packmerge_update.configured():
            return self._json({
                "ok": False, "error": "not configured",
                "hint": "Set \"repo\" in packmerge-update.json next to the "
                        "script (\"username/reponame\")."})
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        action = payload.get("action", "check")
        overwrite = bool(payload.get("overwrite", False))
        if action == "install":
            res = packmerge_update.check()
            if not res.get("ok"):
                return self._json({"ok": False, "error": res.get("error")})
            ok, msg = packmerge_update.install(
                res, overwrite_modified=overwrite)
            restarting = ok and bool(res.get("changed"))
            if restarting:
                # let the response flush, then re-exec into the new code
                threading.Timer(
                    1.5,
                    lambda: packmerge_update.restart_into("packmerge_web.py")
                ).start()
            return self._json({"ok": ok, "message": msg,
                               "restarting": restarting})
        res = packmerge_update.check()
        if not res.get("ok"):
            return self._json({"ok": False, "error": res.get("error")})
        res.pop("_files", None)  # never send internals (or the token) out
        res.pop("_token", None)
        return self._json(res)


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Packmerge</title>
<link rel="icon" type="image/png" href="/icon.png">
<link rel="stylesheet" href="/custom.css">
<script src="/custom.js" defer></script>
<style>
:root{
  --bg:#1e1e1e; --panel:#2d2d30; --raised:#3a3a3d; --fg:#f2f2f2;
  --dim:#9d9da3; --accent:#0a84ff; --accent-hover:#2f9bff;
  --border:#48484a; --logbg:#19191b; --danger:#ff453a;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--bg); color:var(--fg);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
  font-size:15px; line-height:1.45;
}
.wrap{max-width:940px; margin:0 auto; padding:28px 20px 60px}
h1{font-size:26px; margin:0 0 2px}
h1.brand{display:flex; align-items:center; gap:11px}
h1.brand img{width:34px; height:34px; border-radius:8px}
.subtitle{color:var(--dim); margin:0 0 22px}
.card{
  border:1px solid var(--border); border-radius:10px;
  padding:16px 18px; margin-bottom:14px;
}
.card>h2{font-size:13px; color:var(--dim); margin:0 0 12px; letter-spacing:.4px}
label.f{display:block; color:var(--dim); font-size:13px; margin:10px 0 4px}
input[type=text],select{
  width:100%; background:var(--panel); color:var(--fg);
  border:1px solid var(--border); border-radius:7px; padding:9px 11px;
  font-size:15px; font-family:inherit;
}
input[type=text]:focus,select:focus{outline:none; border-color:var(--accent)}
.row{display:flex; gap:10px; align-items:center}
.row .grow{flex:1}
button{
  background:var(--raised); color:var(--fg); border:none; border-radius:7px;
  padding:9px 16px; font-size:14px; font-family:inherit; cursor:pointer;
  white-space:nowrap;
}
button:hover{background:#4b4b4f}
button:disabled{opacity:.45; cursor:default}
button.accent{background:var(--accent); color:#fff; font-weight:600}
button.accent:hover{background:var(--accent-hover)}
button.danger{background:var(--danger); color:#fff; font-weight:600}
button.danger:hover{filter:brightness(1.12)}
button.danger-ghost{background:transparent; color:var(--danger)}
.hint{color:var(--dim); font-size:13px; margin:0 0 12px}
/* sources list */
ul.pick{list-style:none; margin:0 0 12px; padding:0; max-height:260px; overflow:auto;
  border:1px solid var(--border); border-radius:7px; background:var(--panel)}
ul.pick li{
  padding:9px 12px; display:flex; align-items:center; gap:10px; cursor:pointer;
  border-bottom:1px solid var(--border);
}
ul.pick li:last-child{border-bottom:none}
ul.pick li:hover{background:#343437}
ul.pick li.sel{background:var(--accent); color:#fff}
ul.pick li.sel .badge{background:rgba(255,255,255,.22); color:#fff}
.badge{
  font-size:11px; background:var(--raised); color:var(--dim);
  border-radius:20px; padding:2px 9px; margin-left:auto; flex-shrink:0;
}
.src-actions{display:flex; gap:8px; flex-wrap:wrap; align-items:center}
.src-actions .spacer{flex:1}
/* two columns */
.cols{display:grid; grid-template-columns:1fr 1fr; gap:14px}
@media(max-width:700px){.cols{grid-template-columns:1fr}}
/* checkboxes */
.checks{margin-top:12px; display:grid; gap:8px}
.check{display:flex; gap:10px; align-items:flex-start; cursor:pointer}
.check input{width:18px; height:18px; margin-top:2px; accent-color:var(--accent)}
/* tooltip */
[data-tip]{position:relative; cursor:help}
[data-tip]:hover::after{
  content:attr(data-tip); position:absolute; left:0; top:calc(100% + 8px);
  background:#3a3a3c; color:var(--fg); font-size:13px; font-weight:400;
  padding:10px 12px; border-radius:8px; width:320px; z-index:50;
  border:1px solid var(--border); white-space:pre-line; line-height:1.5;
}
/* log */
#logCard{display:none}
#log{
  background:var(--logbg); border-radius:7px; padding:12px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12.5px;
  max-height:320px; overflow:auto; white-space:pre-wrap; color:#d7d7db; margin:0;
}
/* status bar */
.statusbar{display:flex; gap:10px; align-items:center; margin:4px 0 14px}
.statusbar .status{margin-left:auto; color:var(--dim); font-size:13px}
/* modal */
.overlay{
  position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:100;
  display:flex; align-items:center; justify-content:center; padding:20px;
}
.dialog{
  background:var(--panel); border:1px solid var(--border); border-radius:12px;
  padding:22px; width:480px; max-width:100%; max-height:80vh; overflow:auto;
}
.dialog h3{margin:0 0 6px; font-size:17px}
.dialog p{color:var(--dim); font-size:14px; margin:0 0 14px}
.dialog .btns{display:flex; gap:8px; justify-content:flex-end; margin-top:16px}
/* file browser */
.crumb{color:var(--dim); font-size:13px; margin-bottom:10px; word-break:break-all}
.browser{border:1px solid var(--border); border-radius:7px; max-height:300px; overflow:auto}
.browser .brow{
  padding:9px 12px; cursor:pointer; border-bottom:1px solid var(--border);
  display:flex; gap:10px; align-items:center;
}
.browser .brow:last-child{border-bottom:none}
.browser .brow:hover{background:#343437}
.browser .brow.dir::before{content:"\\1F4C1 "}
.browser .brow.zip::before{content:"\\1F4E6 "}
.browser .brow.up::before{content:"\\2B06 "}
.empty{color:var(--dim); text-align:center; padding:18px; font-size:14px}
.kv{font-size:13px; color:var(--dim); margin-top:8px; word-break:break-all}
</style>
</head>
<body>
<div class="wrap">
  <h1 class="brand"><img src="/icon.png" alt="">Packmerge</h1>
  <p class="subtitle">Turn a stack of resource packs into one.</p>

  <div class="card">
    <h2>PACK</h2>
    <label class="f" for="packName">Name</label>
    <input type="text" id="packName" placeholder="My Pack" autocomplete="off">
    <label class="f" for="output">Output file</label>
    <div class="row">
      <div class="grow"><input type="text" id="output" placeholder="MyPack.zip" autocomplete="off"></div>
      <button id="browseOutput">Browse</button>
    </div>
    <label class="f" for="desc">Description</label>
    <input type="text" id="desc" autocomplete="off">
    <div class="checks">
      <label class="check" data-tip="When two packs contain the same JSON file, combine them instead of letting the higher pack replace the whole file.&#10;&#10;For example: two packs that each restyle different GUI screens, or each add custom bow models, will both survive in the one merged file. Conflicts still warn you.&#10;&#10;Turn this off for strict top-pack-wins on every file.">
        <input type="checkbox" id="smartJson"><span>Smart JSON merge</span>
      </label>
      <label class="check" data-tip="If several packs each bring their own settings screen (Respackopts), merge them into a single settings menu with one section per pack.&#10;&#10;Turn this off and only the top pack's settings will work.">
        <input type="checkbox" id="mergeRpo"><span>Merge Respackopts configs</span>
      </label>
    </div>
  </div>

  <div class="card">
    <h2>SOURCES &nbsp;&mdash;&nbsp; bottom of the list wins</h2>
    <ul class="pick" id="srcList"></ul>
    <div class="src-actions">
      <button id="addUrl">Add URL</button>
      <button id="addLocal">Add local file</button>
      <button id="removeSrc">Remove</button>
      <button id="moveUp">Move up</button>
      <button id="moveDown">Move down</button>
      <span class="spacer"></span>
      <button id="buildBtn" class="accent">Merge Packs</button>
    </div>
  </div>

  <div class="cols">
    <div class="card">
      <h2>PER-FILE OVERRIDES</h2>
      <p class="hint">Force a specific file to come from a specific pack, beating the priority order above. Useful when you like one pack overall but prefer another pack's version of a single texture or model.</p>
      <ul class="pick" id="ovList"></ul>
      <div class="src-actions">
        <button id="addOv">Add</button>
        <button id="removeOv">Remove</button>
      </div>
    </div>
    <div class="card">
      <h2>EXCLUDED PATHS</h2>
      <p class="hint">Drop files or whole folders from the merged pack. Useful for stripping parts of a pack you don't want, like its OptiFine extras. End a path with / to drop an entire folder.</p>
      <ul class="pick" id="exList"></ul>
      <div class="src-actions">
        <button id="addEx">Add</button>
        <button id="removeEx">Remove</button>
      </div>
    </div>
  </div>

  <div class="statusbar">
    <button id="checkBtn">Check for updates</button>
    <button id="openFolder">Open output folder</button>
    <button id="logToggle">Log</button>
    <button id="appUpdateBtn">App updates</button>
    <span class="status" id="status">Ready.</span>
  </div>

  <div class="card" id="logCard">
    <h2>LOG</h2>
    <pre id="log"></pre>
  </div>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const MODRINTH_API = "https://api.modrinth.com/v2";
let S = null;               // state
let autoOutput = true;
let saveTimer = null;
let pollTimer = null;
let seenLog = 0;
let currentMode = "build";
let selectedSrc = -1, selectedOv = -1, selectedEx = -1;

async function api(method, path, body){
  const r = await fetch(path, {
    method, headers: {"Content-Type":"application/json"},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if(!r.ok) throw new Error("request failed: " + path);
  return r.json();
}
function scheduleSave(){
  clearTimeout(saveTimer);
  saveTimer = setTimeout(()=> api("POST","/api/state", S).catch(()=>{}), 300);
}
function setStatus(t){ $("status").textContent = t; }

/* ---------------- state <-> ui ---------------- */
function syncFromUI(){
  S.pack_name = $("packName").value.trim();
  S.description = $("desc").value.trim();
  S.output = $("output").value.trim();
  S.smart_json_merge = $("smartJson").checked;
  S.merge_respackopts = $("mergeRpo").checked;
  S.mc_version = "latest";
}
function renderAll(){
  $("packName").value = S.pack_name || "";
  $("desc").value = S.description || "";
  $("output").value = S.output || "";
  $("smartJson").checked = S.smart_json_merge !== false;
  $("mergeRpo").checked = S.merge_respackopts !== false;
  const n = S.pack_name || "";
  autoOutput = !S.output || (n && S.output === n + ".zip");
  renderSources(); renderOverrides(); renderExcludes();
}
$("packName").addEventListener("input", e=>{
  syncFromUI(); scheduleSave();
  if(autoOutput) $("output").value = e.target.value.trim() ? e.target.value.trim()+".zip" : "";
  S.output = $("output").value;
});
$("output").addEventListener("input", ()=>{ autoOutput = false; syncFromUI(); scheduleSave(); });
$("desc").addEventListener("input", ()=>{ syncFromUI(); scheduleSave(); });
$("smartJson").addEventListener("change", ()=>{ syncFromUI(); scheduleSave(); });
$("mergeRpo").addEventListener("change", ()=>{ syncFromUI(); scheduleSave(); });

/* ---------------- sources ---------------- */
function kindOf(src){
  if(src.modrinth) return "Modrinth";
  if(src.url) return "URL";
  return "File";
}
function renderSources(){
  const ul = $("srcList"); ul.innerHTML = "";
  const btn = $("removeSrc");
  const hasSel = selectedSrc >= 0 && selectedSrc < S.sources.length;
  btn.disabled = !S.sources.length;
  btn.textContent = hasSel ? "Remove" : "Clear all";
  if(!S.sources.length){
    ul.innerHTML = '<div class="empty">No packs yet &mdash; add one below.</div>';
    return;
  }
  S.sources.forEach((s,i)=>{
    const li = document.createElement("li");
    li.innerHTML = `<span></span><span class="badge"></span>`;
    li.firstChild.textContent = s.name || ("source-"+i);
    li.lastChild.textContent = kindOf(s);
    if(i === selectedSrc) li.classList.add("sel");
    li.onclick = ()=>{ selectedSrc = (selectedSrc === i) ? -1 : i; renderSources(); };
    li.ondblclick = ()=> moveSrc(-1);
    ul.appendChild(li);
  });
}
function modrinthSlug(v){
  const m = v.match(/modrinth\\.com\\/(?:resourcepack|mod|datapack|plugin)\\/([A-Za-z0-9_-]+)/);
  return m ? m[1] : v.trim();
}
function looksModrinth(v){
  return v.includes("modrinth.com") || /^[A-Za-z0-9_-]+$/.test(v.trim());
}
function nameFromUrl(url){
  let f = url.split("?")[0].replace(/\\/$/,"").split("/").pop() || "";
  if(f.toLowerCase().endsWith(".zip")) f = f.slice(0,-4);
  return (f.replace(/[-_]+/g," ").trim()) || "Downloaded pack";
}
$("addUrl").onclick = ()=>{
  const {overlay, input} = textModal("Add pack",
    "Paste a Modrinth link or a direct zip URL.", "https://modrinth.com/resourcepack/…");
  input.focus();
  overlay.querySelector("[data-ok]").onclick = ()=>{
    const url = input.value.trim();
    if(!url) return;
    closeModal(overlay);
    if(looksModrinth(url)){
      const slug = modrinthSlug(url);
      const src = {name: slug, modrinth: url};
      S.sources.push(src); selectedSrc = S.sources.length-1;
      syncFromUI(); renderSources(); scheduleSave();
      fetch(`${MODRINTH_API}/project/${slug}`).then(r=>r.json()).then(p=>{
        if(p && p.title && S.sources.includes(src)){
          src.name = p.title; renderSources(); scheduleSave();
        }
      }).catch(()=>{});
    } else {
      S.sources.push({name: nameFromUrl(url), url});
      selectedSrc = S.sources.length-1;
      syncFromUI(); renderSources(); scheduleSave();
    }
  };
};
$("addLocal").onclick = ()=> browseModal("zip");
$("removeSrc").onclick = ()=>{
  if(selectedSrc >= 0 && selectedSrc < S.sources.length){
    S.sources.splice(selectedSrc,1);
    selectedSrc = -1; renderSources(); scheduleSave();
    return;
  }
  if(!S.sources.length) return;
  const n = S.sources.length;
  confirmModal("Clear all sources?",
    `Remove all ${n} pack${n === 1 ? "" : "s"} from the list? ` +
    "Your per-file overrides and excluded paths are kept.",
    "Clear all", ()=>{
      S.sources = []; selectedSrc = -1; renderSources(); scheduleSave();
    });
};
function moveSrc(d){
  const i = selectedSrc, j = i + d;
  if(i < 0 || j < 0 || j >= S.sources.length) return;
  [S.sources[i], S.sources[j]] = [S.sources[j], S.sources[i]];
  selectedSrc = j; renderSources(); scheduleSave();
}
$("moveUp").onclick = ()=> moveSrc(-1);
$("moveDown").onclick = ()=> moveSrc(1);

/* ---------------- overrides / excludes ---------------- */
function renderOverrides(){
  const ul = $("ovList"); ul.innerHTML = "";
  const o = S.overrides || {};
  const keys = Object.keys(o);
  if(!keys.length){ ul.innerHTML = '<div class="empty">None.</div>'; return; }
  keys.forEach((p,i)=>{
    const li = document.createElement("li");
    li.textContent = `${p}  →  ${o[p]}`;
    if(i === selectedOv) li.classList.add("sel");
    li.onclick = ()=>{ selectedOv = i; renderOverrides(); };
    ul.appendChild(li);
  });
}
function renderExcludes(){
  const ul = $("exList"); ul.innerHTML = "";
  const ex = S.exclude || [];
  if(!ex.length){ ul.innerHTML = '<div class="empty">None.</div>'; return; }
  ex.forEach((p,i)=>{
    const li = document.createElement("li");
    li.textContent = p;
    if(i === selectedEx) li.classList.add("sel");
    li.onclick = ()=>{ selectedEx = i; renderExcludes(); };
    ul.appendChild(li);
  });
}
$("addOv").onclick = ()=>{
  if(!S.sources.length){ alert("Add at least one pack first."); return; }
  const {overlay, input, select} = overrideModal();
  input.focus();
  overlay.querySelector("[data-ok]").onclick = ()=>{
    const p = input.value.trim();
    if(!p) return;
    S.overrides = S.overrides || {};
    S.overrides[p] = select.value;
    closeModal(overlay); renderOverrides(); scheduleSave();
  };
};
$("removeOv").onclick = ()=>{
  const keys = Object.keys(S.overrides || {});
  if(selectedOv < 0 || selectedOv >= keys.length) return;
  delete S.overrides[keys[selectedOv]];
  selectedOv = -1; renderOverrides(); scheduleSave();
};
$("addEx").onclick = ()=>{
  const {overlay, input} = textModal("Exclude a path",
    "Path to drop (end with / to drop a whole folder).", "assets/minecraft/optifine/");
  input.focus();
  overlay.querySelector("[data-ok]").onclick = ()=>{
    const p = input.value.trim();
    if(!p) return;
    S.exclude = S.exclude || [];
    S.exclude.push(p);
    closeModal(overlay); renderExcludes(); scheduleSave();
  };
};
$("removeEx").onclick = ()=>{
  if(selectedEx < 0 || selectedEx >= (S.exclude||[]).length) return;
  S.exclude.splice(selectedEx,1);
  selectedEx = -1; renderExcludes(); scheduleSave();
};

/* ---------------- modals ---------------- */
function openModalShell(title){
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.innerHTML = `<div class="dialog"><h3></h3><div class="mbody"></div><div class="btns"></div></div>`;
  overlay.querySelector("h3").textContent = title;
  document.body.appendChild(overlay);
  overlay.addEventListener("mousedown", e=>{ if(e.target === overlay) closeModal(overlay); });
  document.addEventListener("keydown", function esc(e){
    if(e.key === "Escape"){ closeModal(overlay); document.removeEventListener("keydown", esc); }
  });
  return overlay;
}
function closeModal(overlay){ overlay.remove(); }
function addButtons(overlay, buttons){
  const bar = overlay.querySelector(".btns");
  buttons.forEach(([label, cls, fn])=>{
    const b = document.createElement("button");
    b.textContent = label;
    if(cls) b.className = cls;
    if(label === "OK" || cls === "accent") b.setAttribute("data-ok","");
    b.onclick = fn;
    bar.appendChild(b);
  });
}
function textModal(title, hint, placeholder){
  const overlay = openModalShell(title);
  const body = overlay.querySelector(".mbody");
  if(hint){ const p = document.createElement("p"); p.textContent = hint; body.appendChild(p); }
  const input = document.createElement("input");
  input.type = "text"; input.placeholder = placeholder || "";
  body.appendChild(input);
  input.addEventListener("keydown", e=>{
    if(e.key === "Enter") overlay.querySelector("[data-ok]").click();
  });
  addButtons(overlay, [["Cancel","",()=>closeModal(overlay)],["Add","accent",()=>{}]]);
  return {overlay, input};
}
function overrideModal(){
  const overlay = openModalShell("Force a file's source");
  const body = overlay.querySelector(".mbody");
  const p1 = document.createElement("p"); p1.textContent = "File path inside the pack:"; body.appendChild(p1);
  const input = document.createElement("input"); input.type = "text";
  input.placeholder = "assets/minecraft/textures/gui/book.png"; body.appendChild(input);
  const p2 = document.createElement("p"); p2.textContent = "Source that should win this file:";
  p2.style.marginTop = "12px"; body.appendChild(p2);
  const select = document.createElement("select");
  S.sources.forEach((s,i)=>{
    const o = document.createElement("option");
    o.value = s.name || ("source-"+i); o.textContent = o.value;
    select.appendChild(o);
  });
  if(S.sources.length) select.value = S.sources[S.sources.length-1].name;
  body.appendChild(select);
  addButtons(overlay, [["Cancel","",()=>closeModal(overlay)],["Add","accent",()=>{}]]);
  return {overlay, input, select};
}
function errorModal(summary){
  const overlay = openModalShell("Something went wrong");
  const body = overlay.querySelector(".mbody");
  const p = document.createElement("p");
  p.textContent = summary; p.style.wordBreak = "break-word";
  body.appendChild(p);
  addButtons(overlay, [
    ["Read Log","accent",()=>{ showLog(true); closeModal(overlay); }],
    ["OK","",()=>closeModal(overlay)],
  ]);
}
function confirmModal(title, message, okLabel, onOk){
  const overlay = openModalShell(title);
  const body = overlay.querySelector(".mbody");
  const p = document.createElement("p");
  p.textContent = message;
  body.appendChild(p);
  addButtons(overlay, [
    ["Cancel","",()=>closeModal(overlay)],
    [okLabel,"danger",()=>{ closeModal(overlay); onOk(); }],
  ]);
}

/* ---------------- server-side file browser ---------------- */
function browseModal(mode){ // mode: "zip" | "dir"
  const overlay = openModalShell(mode === "zip" ? "Choose a resource pack" : "Choose a folder");
  const body = overlay.querySelector(".mbody");
  const crumb = document.createElement("div"); crumb.className = "crumb"; body.appendChild(crumb);
  const list = document.createElement("div"); list.className = "browser"; body.appendChild(list);
  const kv = document.createElement("div"); kv.className = "kv"; body.appendChild(kv);
  let current = null, pickedDir = null;

  async function go(path){
    const d = await api("GET", "/api/browse?path=" + encodeURIComponent(path || ""));
    current = d.path; pickedDir = d.path;
    crumb.textContent = d.path;
    list.innerHTML = "";
    if(d.parent){
      const up = document.createElement("div");
      up.className = "brow up"; up.textContent = "Up";
      up.onclick = ()=> go(d.parent);
      list.appendChild(up);
    }
    d.dirs.forEach(n=>{
      const el = document.createElement("div");
      el.className = "brow dir"; el.textContent = n;
      el.onclick = ()=> go(d.path + "/" + n);
      list.appendChild(el);
    });
    if(mode === "zip"){
      if(!d.zips.length && !d.dirs.length)
        list.innerHTML += '<div class="empty">No zip files here.</div>';
      d.zips.forEach(n=>{
        const el = document.createElement("div");
        el.className = "brow zip"; el.textContent = n;
        el.onclick = ()=> pickZip(d.path + "/" + n);
        list.appendChild(el);
      });
    } else {
      kv.textContent = "Pick the current folder with the button below.";
    }
  }
  async function pickZip(full){
    const n = await api("GET", "/api/normalize-path?path=" + encodeURIComponent(full));
    const stem = full.split("/").pop().replace(/\\.zip$/i,"") || "Local pack";
    S.sources.push({name: stem, path: n.path});
    selectedSrc = S.sources.length - 1;
    closeModal(overlay); renderSources(); scheduleSave();
  }
  const btns = [["Cancel","",()=>closeModal(overlay)]];
  if(mode === "dir"){
    btns.push(["Use this folder","accent", async ()=>{
      const n = await api("GET", "/api/normalize-path?path=" + encodeURIComponent(pickedDir));
      const cur = $("output").value.trim() || "Pack.zip";
      const base = cur.split("/").pop();
      $("output").value = n.path.replace(/\\\\/g,"/") + "/" + base;
      autoOutput = false; syncFromUI(); scheduleSave();
      closeModal(overlay);
    }]);
  }
  addButtons(overlay, btns);
  go("");
}
$("browseOutput").onclick = ()=> browseModal("dir");

/* ---------------- log ---------------- */
function showLog(show){
  $("logCard").style.display = show ? "block" : "none";
  $("logToggle").textContent = show ? "Hide log" : "Log";
}
$("logToggle").onclick = ()=> showLog($("logCard").style.display !== "block");
function appendLog(text){
  const el = $("log");
  el.textContent += text;
  el.scrollTop = el.scrollHeight;
}

/* ---------------- run ---------------- */
$("checkBtn").onclick = ()=> run("check");
$("buildBtn").onclick = ()=> run("build");
$("openFolder").onclick = ()=> api("GET","/api/open-output").catch(()=>{});
$("appUpdateBtn").onclick = checkAppUpdates;

async function checkAppUpdates(){
  setStatus("Checking for app updates…");
  let d;
  try{ d = await api("POST","/api/app-update",{action:"check"}); }
  catch(e){ setStatus("App update check failed."); return; }
  if(!d.ok){
    setStatus("Ready.");
    errorModal("App update check failed. " +
      (d.error === "not configured" ? d.hint : d.error));
    return;
  }
  const changed = d.changed || [], modified = d.modified || [];
  if(!changed.length){
    setStatus(modified.length
      ? `App is up to date (v${d.current}); your edits to ${modified.join(", ")} are untouched.`
      : `App is up to date (v${d.current}).`);
    return;
  }
  setStatus("Ready.");
  const overlay = openModalShell("App update available");
  const body = overlay.querySelector(".mbody");
  const p = document.createElement("p");
  p.textContent = `Version ${d.current} → ${d.remote}. These files will be replaced:`;
  body.appendChild(p);
  const ul = document.createElement("ul");
  ul.style.margin = "0 0 6px 18px";
  changed.forEach(f=>{
    const li = document.createElement("li");
    li.textContent = f; li.style.fontFamily = "monospace";
    ul.appendChild(li);
  });
  body.appendChild(ul);
  if(modified.length){
    const pm = document.createElement("p");
    pm.textContent = "These have your own edits and will be kept as-is " +
      "unless you overwrite them:";
    body.appendChild(pm);
    const ulm = document.createElement("ul");
    ulm.style.margin = "0 0 6px 18px";
    modified.forEach(f=>{
      const li = document.createElement("li");
      li.textContent = f; li.style.fontFamily = "monospace";
      ulm.appendChild(li);
    });
    body.appendChild(ulm);
  }
  const buttons = [["Cancel","",()=>closeModal(overlay)]];
  if(modified.length)
    buttons.push(["Overwrite my edits","danger",()=>doAppInstall(true, overlay)]);
  buttons.push(["Install & restart","accent",()=>doAppInstall(false, overlay)]);
  addButtons(overlay, buttons);
}

async function doAppInstall(overwrite, overlay){
  closeModal(overlay);
  setStatus("Installing app update…");
  let r;
  try{ r = await api("POST","/api/app-update",{action:"install", overwrite}); }
  catch(e){
    setStatus("Ready.");
    errorModal("Install failed: lost connection to the server.");
    return;
  }
  if(!r.ok){
    setStatus("Ready.");
    errorModal("Install failed: " + (r.error || r.message || "unknown error"));
    return;
  }
  if(r.restarting){
    const ov2 = openModalShell("Update installed");
    const b2 = ov2.querySelector(".mbody");
    const p2 = document.createElement("p");
    p2.textContent = r.message + " The server is restarting — this page will reload in a moment.";
    b2.appendChild(p2);
    setStatus("Restarting…");
    setTimeout(()=> location.reload(), 4500);
  } else {
    setStatus(r.message);
  }
}

async function run(mode){
  syncFromUI();
  if(!S.sources.length){ alert("Add at least one pack first."); return; }
  if(!$("packName").value.trim()){ alert("Give your pack a name first."); return; }
  await api("POST","/api/state", S).catch(()=>{});
  const r = await api("POST","/api/run", {mode});
  if(!r.ok) return;
  currentMode = mode;
  setStatus(mode === "check" ? "Checking for updates…" : "Building…");
  setRunning(true);
  seenLog = 0;
  $("log").textContent = "";
  clearInterval(pollTimer);
  pollTimer = setInterval(pollLog, 400);
  pollLog();
}
function setRunning(on){
  ["checkBtn","buildBtn"].forEach(id=> $(id).disabled = on);
}
async function pollLog(){
  let d;
  try{ d = await api("GET","/api/log"); }
  catch(e){ return; }
  if(d.lines.length > seenLog){
    appendLog(d.lines.slice(seenLog).join(""));
    seenLog = d.lines.length;
  }
  if(d.done){
    clearInterval(pollTimer);
    setRunning(false);
    finishRun(d.rc, d.lines.join(""));
  }
}
function finishRun(rc, text){
  const failed = rc !== 0 || /Traceback/.test(text);
  if(failed){
    let summary = "packmerge exited with code " + rc + ".";
    const lines = text.split("\\n").reverse();
    for(const l of lines){
      const s = l.trim();
      if(!s || /downloading/i.test(s)) continue;
      if(/error|traceback|failed|mismatch|not a valid/i.test(s)){ summary = s.slice(0,300); break; }
    }
    errorModal(summary);
    return;
  }
  if(/updates available/i.test(text)){
    showLog(true);
    setStatus("Updates available — see the log.");
  } else if(currentMode === "check"){
    setStatus("Everything is up to date.");
  } else if(/WARNING/.test(text)){
    setStatus("Done — with warnings. Open the log for details.");
  } else {
    setStatus("Done.");
  }
}

/* ---------------- init ---------------- */
(async function init(){
  try{
    S = await api("GET","/api/state");
  }catch(e){
    document.body.innerHTML = '<div class="wrap"><h1>packmerge</h1><p class="subtitle">Could not reach the local server. Is packmerge_web.py running?</p></div>';
    return;
  }
  renderAll();
  // show the last run's log (if any) so the log isn't blank after reload
  try{
    const d = await api("GET","/api/log");
    if(d.lines && d.lines.length){
      seenLog = d.lines.length;
      $("log").textContent = d.lines.join("");
      $("log").scrollTop = $("log").scrollHeight;
    } else {
      $("log").textContent = "No log output yet — run a check or build and the output will appear here.";
    }
  }catch(e){}
})();
</script>
</body>
</html>
"""
# ------------------------------------------------------------------ main

def _auto_update_at_startup(args):
    """Check for and install app updates at startup (e.g. at login).

    Runs before the server starts; if updates were installed, re-execs
    into the new code. Never breaks startup: any failure just starts
    the server as-is.
    """
    if args.no_auto_update:
        return
    try:
        import packmerge_update
    except ImportError:
        return
    if not packmerge_update.configured():
        return
    try:
        res = packmerge_update.check(timeout=10, retries=1)
    except Exception:
        return
    if not res.get("ok") or not res.get("changed"):
        return
    if res.get("modified"):
        # unattended startup must never destroy local edits; the user can
        # resolve this from the App updates button
        print("[packmerge-web] skipping auto-update; you have local edits "
              "to: " + ", ".join(res["modified"]))
        return
    try:
        ok, _msg = packmerge_update.install(res)
    except Exception:
        return
    if ok:
        print(f"[packmerge-web] updated to {res['remote']}, restarting…")
        # guard flag keeps the new process from checking again (no loops)
        sys.argv = ([sys.argv[0]]
                    + [a for a in sys.argv[1:] if a != "--no-auto-update"]
                    + ["--no-auto-update"])
        packmerge_update.restart_into("packmerge_web.py")


def main():
    ap = argparse.ArgumentParser(description="Browser UI for packmerge, served locally.")
    ap.add_argument("--port", type=int, default=8471)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-auto-update", action="store_true",
                    help="skip the automatic update check at startup")
    args = ap.parse_args()

    if not os.path.exists(PACKMERGE_PY):
        sys.exit(f"packmerge.py not found next to this script: {PACKMERGE_PY}")

    if sys.stdout is None:
        # windowed launch (pyw/pythonw has no console): printing would crash
        # the server instantly, so send output to a log file instead
        logf = open(os.path.join(HERE, "packmerge-web.log"), "a", buffering=1)
        sys.stdout = sys.stderr = logf

    _auto_update_at_startup(args)

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}"
    print(f"[packmerge-web] serving at {url}  (Ctrl+C to stop)")
    # Watch mode (slice 5): a daemon mtime poller for local path
    # sources, active only while Auto Rebuild is ON. Daemon so it
    # never blocks shutdown; fully guarded so it cannot crash the
    # server.
    threading.Thread(target=wb_watch_loop, daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[packmerge-web] stopped")


if __name__ == "__main__":
    main()
