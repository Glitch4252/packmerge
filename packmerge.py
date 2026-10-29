#!/usr/bin/env python3
"""
packmerge - deterministically merge Minecraft resource packs into one.

You describe your frankenpack in a manifest (JSON): a list of sources in
priority order (low -> high, exactly like the game's pack stack, top wins),
optional per-file overrides, and files to exclude. packmerge resolves each
source (Modrinth page/slug, direct zip URL, or local path), downloads what
changed since the last run, and rebuilds the merged pack zip.

Usage:
    python3 packmerge.py [-m manifest.json] [--check] [--force] [--mc-version 26.3]

Manifest example:
    {
        "mc_version": "26.3",
        "pack_name": "MyFrankenpack",
        "description": "My personal merge",
        "output": "MyFrankenpack.zip",
        "sources": [
            {"name": "dark",   "modrinth": "default-dark-mode"},
            {"name": "tweaks", "modrinth": "https://modrinth.com/resourcepack/some-pack"},
            {"name": "extra",  "url": "https://example.com/extra.zip"},
            {"name": "local",  "path": "./my-tweaks.zip"}
        ],
        "overrides": {
            "assets/minecraft/textures/gui/book.png": "dark"
        },
        "exclude": [
            "assets/minecraft/optifine/color.properties",
            "assets/some-mod/"
        ]
    }

Notes:
- "overrides" picks the winner for a file by source name (or 0-based index),
  regardless of priority order. "exclude" drops exact paths, or whole
  directory prefixes ending in "/".
- A lockfile (<manifest>.lock.json) records what was used last run, so
  re-running only downloads what actually changed.
- Stdlib only: no pip packages needed. Python 3.9+.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
import urllib.error
import zipfile

MODRINTH_API = "https://api.modrinth.com/v2"
USER_AGENT = "packmerge/1.0 (personal resource pack merger)"
HERE = os.path.dirname(os.path.abspath(__file__))

# Minecraft version -> resource pack format (major). Extend as needed,
# or set "pack_format" directly in the manifest.
PACK_FORMATS = {
    "1.21.9": 69, "1.21.10": 69,
    "1.21.11": 75,
    "26.1": 84, "26.2": 88, "26.3": 97,
}


def build_pack_mcmeta(manifest, pack_format, description, overlays=None):
    """Build a spec-correct pack.mcmeta.

    Since 1.21.9 (format 65+) the required fields are min_format/max_format
    and the legacy pack_format must be absent unless the pack also supports
    old versions; below format 65 it's the reverse (pack_format required).
    """
    if "min_format" in manifest or "max_format" in manifest:
        pack = {"description": description}
        if "min_format" in manifest:
            pack["min_format"] = manifest["min_format"]
        if "max_format" in manifest:
            pack["max_format"] = manifest["max_format"]
        if pack_format < 65:
            pack["pack_format"] = pack_format
    elif pack_format < 65:
        pack = {"pack_format": pack_format, "description": description}
    else:
        pack = {"min_format": pack_format, "max_format": pack_format,
                "description": description}
    if overlays:
        pack["overlays"] = {"entries": overlays}
    return {"pack": pack}


def collect_overlay_entries(pack_files, modern):
    """Gather overlay declarations from each source's pack.mcmeta.

    The overlay directories themselves merge as normal files, but their
    declarations live in pack.mcmeta, which packmerge regenerates -- without
    this the overlays would silently stop working. Returns (entries, renames)
    where renames maps (source_name, old_dir) -> new_dir for directory name
    collisions between sources.
    """
    entries = []
    renames = {}
    used_dirs = set()
    for name, files in pack_files:
        raw = files.get("pack.mcmeta")
        if not raw:
            continue
        try:
            mcmeta = json.loads(raw.decode("utf-8"))
        except Exception:
            try:
                mcmeta = json5_loads(raw.decode("utf-8"))
            except Exception as e:
                warn(f"'{name}': could not parse pack.mcmeta ({e}); "
                     f"its overlay declarations are skipped")
                continue
        for entry in (mcmeta.get("overlays") or {}).get("entries", []):
            e = dict(entry)
            directory = e.get("directory")
            if not directory:
                continue
            if modern:
                # legacy 'formats' is not allowed on modern packs; derive
                # min/max from it when the entry only has the old field
                fmts = e.pop("formats", None)
                if "min_format" not in e and isinstance(fmts, dict):
                    if "min_inclusive" in fmts:
                        e["min_format"] = fmts["min_inclusive"]
                    if "max_inclusive" in fmts:
                        e["max_format"] = fmts["max_inclusive"]
            else:
                if "formats" not in e:
                    def major(v):
                        return v[0] if isinstance(v, list) else v
                    e["formats"] = {
                        "min_inclusive": major(e.get("min_format", 1)),
                        "max_inclusive": major(e.get("max_format", 999))}
            new_dir = directory
            if new_dir in used_dirs:
                base = re.sub(r"[^a-z0-9_.-]", "_", name.lower()) or "pack"
                cand, k = f"{base}-{directory}", 0
                while cand in used_dirs:
                    k += 1
                    cand = f"{base}-{directory}-{k}"
                new_dir = cand
                renames[(name, directory)] = new_dir
                e["directory"] = new_dir
                warn(f"'{name}': overlay directory '{directory}' is also "
                     f"declared by another source; renamed to '{new_dir}'")
            used_dirs.add(new_dir)
            entries.append(e)
    return entries, renames


def log(msg):
    print(f"[packmerge] {msg}", flush=True)


def warn(msg):
    print(f"[packmerge] WARNING: {msg}", flush=True)


# ---------------------------------------------------------------- HTTP/API

def http_get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    return urllib.request.urlopen(req, timeout=60)


def api_get(url):
    with http_get(url) as resp:
        return json.load(resp)


def modrinth_slug(value):
    """Accept a slug or a full Modrinth project URL, return the slug."""
    m = re.search(r"modrinth\.com/(?:resourcepack|mod|datapack|plugin)/([A-Za-z0-9_-]+)", value)
    return m.group(1) if m else value.strip()


def resolve_modrinth(value, mc_version, loader=None, version_id=None):
    """Resolve a Modrinth pack to a concrete downloadable version.

    When loader is given, only versions carrying that loader are
    considered (e.g. "datapack" for dual-published projects whose
    newest version is a mod). Without it, behavior is unchanged.
    When version_id is given, that exact version wins (the workbench
    update queue pins a queued update so a build uses it even if a
    newer version has since appeared); without it, newest logic is
    unchanged."""
    slug = modrinth_slug(value)
    project = api_get(f"{MODRINTH_API}/project/{slug}")
    title = project.get("title", slug)
    versions = api_get(f"{MODRINTH_API}/project/{slug}/version")
    if not versions:
        raise RuntimeError(f"Modrinth: no versions found for '{slug}'")
    # Newest first -- don't rely on the API's ordering.
    versions.sort(key=lambda v: v.get("date_published", ""), reverse=True)

    if loader is not None:
        versions = [v for v in versions
                    if loader in (v.get("loaders") or [])]
        if not versions:
            raise RuntimeError(
                f"Modrinth: no version of '{slug}' uses loader '{loader}'")

    if version_id is not None:
        versions = [v for v in versions if v.get("id") == version_id]
        if not versions:
            raise RuntimeError(
                f"Modrinth: version '{version_id}' of '{slug}' not found")

    chosen = None
    if mc_version == "latest" or mc_version is None:
        chosen = versions[0]
    else:
        for v in versions:
            if mc_version in v.get("game_versions", []):
                chosen = v
                break
    if chosen is None:
        chosen = versions[0]
        warn(f"'{title}': no release for MC {mc_version}; using newest "
             f"({chosen['version_number']}, supports {chosen.get('game_versions')})")

    files = chosen.get("files") or []
    if not files:
        raise RuntimeError(f"Modrinth: version {chosen['version_number']} of '{slug}' has no files")
    f = next((x for x in files if x.get("primary")), files[0])
    return {
        "kind": "modrinth",
        "slug": slug,
        "title": title,
        "version_id": chosen["id"],
        "version_number": chosen["version_number"],
        "filename": f["filename"],
        "url": f["url"],
        "sha512": (f.get("hashes") or {}).get("sha512"),
    }


# ------------------------------------------------------------- downloading

def download_file(url, dest_path, expected_sha512=None, extra_headers=None):
    """Download url -> dest_path. Returns (etag, last_modified) for caching."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(extra_headers or {})})
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return "not-modified", None, None
        raise
    etag = resp.headers.get("ETag")
    last_mod = resp.headers.get("Last-Modified")
    h = hashlib.sha512()
    total = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            h.update(chunk)
            total += len(chunk)
    digest = h.hexdigest()
    if expected_sha512 and digest != expected_sha512:
        os.remove(dest_path)
        raise RuntimeError(f"hash mismatch downloading {url}")
    log(f"downloaded {total / 1048576:.1f} MB")
    return "ok", etag, last_mod


# ------------------------------------------------------------------ zips

def read_pack_zip(zip_path):
    """Return {arcname: bytes} for a resource pack zip, normalizing layout."""
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist()
                 if not n.endswith("/")
                 and not n.startswith("__MACOSX/")
                 and os.path.basename(n) != ".DS_Store"]
        # Some packs zip a single top-level folder; unwrap it.
        if "pack.mcmeta" not in names:
            tops = {n.split("/")[0] for n in names if "/" in n}
            if len(tops) == 1:
                top = next(iter(tops))
                if f"{top}/pack.mcmeta" in names:
                    log(f"unwrapping top-level folder '{top}/'")
                    return {n[len(top) + 1:]: z.read(n) for n in names
                            if n.startswith(top + "/") and len(n) > len(top) + 1}
            warn(f"{os.path.basename(zip_path)}: no pack.mcmeta at root; using as-is")
        return {n: z.read(n) for n in names}


# ------------------------------------------------------- Respackopts merge

def json5_loads(text):
    """Parse the JSON5 subset used by respackopts configs (stdlib only).

    Supports: // and /* */ comments, unquoted keys, single-quoted strings,
    trailing commas. Not supported: hex numbers, Infinity/NaN, multiline
    strings, regex literals.
    """
    import ast
    strings = []
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ('"', "'"):
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c:
                    break
                j += 1
            if j >= n:
                raise ValueError("unterminated string in JSON5")
            raw = text[i:j + 1]
            norm = json.dumps(ast.literal_eval(raw)) if c == "'" else raw
            strings.append(norm)
            out.append("\x00%d\x00" % (len(strings) - 1))
            i = j + 1
        else:
            out.append(c)
            i += 1
    text = "".join(out)
    # No string contents left to confuse these:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    text = re.sub(r'([{,]\s*)([A-Za-z_$][A-Za-z0-9_$]*)(\s*:)', r'\1"\2"\3', text)
    text = re.sub(r",(\s*[}\]])", r"\1", text)

    def _restore(m):
        return strings[int(m.group(1))]
    text = re.sub(r"\x00(\d+)\x00", _restore, text)
    return json.loads(text)


def sanitize_rpo_id(name):
    """Respackopts ids/entry names must be alphabetic; make camelCase."""
    parts = [p for p in re.split(r"[^A-Za-z]+", str(name)) if p]
    if not parts:
        return "pack"
    return parts[0][:1].lower() + parts[0][1:] + "".join(
        p[:1].upper() + p[1:] for p in parts[1:])


def normalize_entry(value):
    """Expand shorthand entries (darkMode: false) to full entry objects."""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, bool):
        return {"type": "boolean", "default": value}
    if isinstance(value, int):
        return {"type": "integer", "default": value}
    if isinstance(value, float):
        return {"type": "number", "default": value}
    if isinstance(value, str):
        return {"type": "string", "default": value}
    return {"type": "string", "default": str(value)}


def normalize_conf(node):
    new = {}
    for key, val in node.items():
        if isinstance(val, dict) and "type" not in val:
            new[key] = normalize_conf(val)  # category
        else:
            new[key] = normalize_entry(val)
    return new


def apply_rpo_default(conf, dotted, value):
    """Set one config entry's default (dotted path) in place. Returns bool.

    Only existing entries are touched; categories and unknown paths are
    refused so a bad value can never rewrite a config's shape. Shorthand
    entries (a bare bool/int/str) are replaced by the new value itself.
    """
    parts = dotted.split(".")
    if not parts or any(not p for p in parts):
        return False
    node = conf
    for part in parts[:-1]:
        if not isinstance(node, dict):
            return False
        node = node.get(part)
        if node is None:
            return False
        if isinstance(node, dict) and "type" in node:
            return False  # an entry, not a category
    if not isinstance(node, dict):
        return False
    entry = node.get(parts[-1])
    if entry is None:
        return False
    if isinstance(entry, dict):
        if "type" not in entry:
            return False  # a category, not an entry
        entry["default"] = value
        return True
    node[parts[-1]] = value
    return True


def collect_entry_paths(conf, prefix=()):
    """All condition-referencable entry paths, e.g. ('ui', 'darkMode')."""
    paths = []
    for key, val in conf.items():
        if isinstance(val, dict) and "type" not in val:
            paths.extend(collect_entry_paths(val, prefix + (key,)))
        else:
            paths.append(prefix + (key,))
    return paths


def rewrite_condition(condition, path_map):
    """Prefix a .rpo condition's entry references. Longest paths first."""
    for old in sorted(path_map, key=len, reverse=True):
        condition = re.sub(
            r"(?<![A-Za-z0-9_.])" + re.escape(old) + r"(?![A-Za-z0-9_])",
            path_map[old], condition)
    return condition


def merge_respackopts(merged, origin, rpo_sources, manifest):
    """Merge several respackopts.json5 configs into one. Mutates merged.

    Each source's entries are nested under a category named after the source,
    .rpo conditions are rewritten to the new paths, and lang keys are remapped
    to the merged pack id.
    """
    mid = sanitize_rpo_id(manifest.get("respackopts_id")
                          or manifest.get("pack_name", "merged"))
    lang_ns = mid.lower()  # asset namespaces must be lowercase

    # Unique alphabetic category per source. A workbench namespace
    # rename (manifest "rpo_namespace_overrides") replaces the derived
    # category; a colliding override is refused server-side, and the
    # engine keeps the derived name with a warning rather than ever
    # silently overwriting another source's category.
    cats = {}
    ns_overrides = manifest.get("rpo_namespace_overrides") or {}
    for name, _cfg in rpo_sources:
        base = sanitize_rpo_id(name)
        cat, k = base, 0
        while cat in cats.values():
            k += 1
            cat = base + chr(ord("A") + k - 1)
        if name in ns_overrides and isinstance(ns_overrides[name], str) \
                and ns_overrides[name]:
            want = ns_overrides[name]
            if want not in cats.values():
                cat = want
            else:
                warn(f"'{name}': namespace override '{want}' is already "
                     f"used; keeping '{cat}'")
        cats[name] = cat

    meta = {}  # name -> {"cat": ..., "path_map": {...}}
    merged_conf = {}
    capabilities = set()
    version = 0
    base_cfg = {}
    for name, cfg in rpo_sources:
        cat = cats[name]
        if not re.fullmatch(r"[A-Za-z]+", cfg.get("id", "x")):
            warn(f"'{name}': respackopts id '{cfg.get('id')}' is not alphabetic; "
                 f"keys are remapped anyway")
        capabilities.update(cfg.get("capabilities", []))
        v = cfg.get("version", 0) or 0
        if v >= version:
            version, base_cfg = v, cfg
        if "presets" in cfg:
            warn(f"'{name}': config has presets; they are carried over but may "
                 f"reference renamed entries")
        conf = cfg.get("conf", {})
        path_map = {}
        for p in collect_entry_paths(conf):
            path_map[".".join(p)] = cat + "." + ".".join(p)
        meta[name] = {"cat": cat, "path_map": path_map,
                      "src_id": cfg.get("id", "")}
        merged_conf[cat] = normalize_conf(conf)

    def src_of(path):
        return origin.get(path, "").split(" (")[0]

    # Rewrite surviving .rpo conditions per their origin source.
    for path in [p for p in merged if p.endswith(".rpo")]:
        src_name = src_of(path)
        if src_name not in meta:
            continue
        try:
            rpo = json5_loads(merged[path].decode("utf-8"))
        except Exception as e:
            warn(f"{path}: could not parse .rpo ({e}); leaving as-is")
            continue
        cond = rpo.get("condition")
        if isinstance(cond, str):
            new_cond = rewrite_condition(cond, meta[src_name]["path_map"])
            if new_cond != cond:
                rpo["condition"] = new_cond
                merged[path] = json.dumps(rpo, indent=4).encode()
        elif cond is not None:
            warn(f"{path}: non-string condition left as-is; renamed entries "
                 f"may not match it")
        # Coherence check: the governed file should come from the same source.
        if os.path.basename(path) == ".rpo":
            d = os.path.dirname(path)
            others = {src_of(p) for p in merged
                      if p.startswith(d + "/") and not p.endswith(".rpo")}
            others.discard(src_name)
            if others:
                warn(f"{path}: directory mixes files from {sorted(others)}; "
                     f"one condition now governs them all")
        else:
            governed = path[:-4]  # foo.png.rpo -> foo.png
            g_src = src_of(governed)
            if governed in merged and g_src != src_name:
                warn(f"{path}: governs '{governed}' from '{g_src}', but the "
                     f"condition belongs to '{src_name}'")

    # Remap lang keys to the merged id, under each source's category.
    # The wiki convention places them at assets/<lowercased id>/lang/,
    # but we check the raw id too.
    lang_out = {}
    for name, cfg in rpo_sources:
        sid = meta[name]["src_id"]
        cat = meta[name]["cat"]
        if not sid:
            continue
        lang_dirs = []
        for cand in (f"assets/{sid}/lang/", f"assets/{sid.lower()}/lang/"):
            if cand not in lang_dirs:
                lang_dirs.append(cand)
        for path in [p for p in merged if p.endswith(".json")
                     and any(p.startswith(d) for d in lang_dirs)]:
            if src_of(path) != name:
                warn(f"{path}: lang file not from '{name}'; skipping key rewrite")
                continue
            try:
                lang = json.loads(merged[path].decode("utf-8"))
            except Exception as e:
                warn(f"{path}: could not parse lang json ({e})")
                continue
            new_lang = {}
            for key, val in lang.items():
                if key == f"rpo.{sid}":
                    new_lang[f"rpo.{mid}.{cat}"] = val
                elif key.startswith(f"rpo.{sid}."):
                    new_lang[f"rpo.{mid}.{cat}." + key[len(f"rpo.{sid}."):]] = val
                else:
                    new_lang[key] = val
            new_lang.setdefault(f"rpo.{mid}.{cat}",
                                lang.get(f"rpo.{sid}", name))
            dest = f"assets/{lang_ns}/lang/" + os.path.basename(path)
            lang_out.setdefault(dest, {}).update(new_lang)
    for dest, data in lang_out.items():
        data.setdefault(f"rpo.{mid}", manifest.get("pack_name", "Merged pack"))
        merged[dest] = json.dumps(data, indent=4, ensure_ascii=False).encode()
        origin[dest] = "packmerge"

    new_cfg = {k: v for k, v in base_cfg.items()
               if k not in ("id", "conf", "capabilities", "version")}
    new_cfg["id"] = mid
    new_cfg["conf"] = merged_conf
    new_cfg["capabilities"] = sorted(capabilities) or ["FileFilter"]
    new_cfg["version"] = version or 13
    text = ("// Merged by packmerge from: "
            + ", ".join(n for n, _ in rpo_sources) + "\n"
            + json.dumps(new_cfg, indent=4) + "\n")
    merged["respackopts.json5"] = text.encode()
    origin["respackopts.json5"] = "packmerge"
    log(f"merged {len(rpo_sources)} Respackopts configs into id '{mid}'")


# ------------------------------------------------------- smart JSON merge

merge_stats = {"json_merged": 0, "warnings": 0}


def _mwarn(msg):
    merge_stats["warnings"] += 1
    warn(msg)


def _mnote(msg):
    print(f"[packmerge] (merge) {msg}", flush=True)


def _canon(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def merge_lang(lower, higher, ctx):
    out = dict(lower)
    for k, v in higher.items():
        if k in out and _canon(out[k]) != _canon(v):
            _mnote(f"{ctx['path']}: key '{k}' set by both "
                   f"'{ctx['lower']}' and '{ctx['higher']}'; kept '{ctx['higher']}'s")
        out[k] = v
    return out


def merge_model(lower, higher, ctx):
    lo = {k: v for k, v in lower.items() if k != "overrides"}
    hi = {k: v for k, v in higher.items() if k != "overrides"}
    out = merge_deep(lo, hi, ctx)
    # Splice override lists. Per minecraft.wiki, when several cases match,
    # the LAST case in the list wins -- so lower-priority entries go first
    # and higher-priority entries last. Exact duplicates are dropped; a
    # predicate claimed by both packs with different models warns and keeps
    # the higher-priority pack's model.
    lo_ov = lower.get("overrides") or []
    hi_ov = higher.get("overrides") or []
    spliced, pred_index = [], {}
    for entry in list(lo_ov) + list(hi_ov):
        pred = _canon(entry.get("predicate", {}))
        model = _canon(entry.get("model"))
        if pred in pred_index:
            i = pred_index[pred]
            if _canon(spliced[i].get("model")) == model:
                continue  # exact duplicate
            _mwarn(f"{ctx['path']}: both packs override {entry.get('predicate')}; "
                   f"kept '{ctx['higher']}'s model")
            spliced[i] = entry
        else:
            pred_index[pred] = len(spliced)
            spliced.append(entry)
    if lo_ov or hi_ov:
        out["overrides"] = spliced
    return out


def merge_item_definition(lower, higher, ctx):
    """Merge 1.21.4+ item definitions (assets/<ns>/items/*.json).

    These are typed model selectors (range_dispatch, select, condition,
    composite, model). Entries are keyed by threshold/when value; conflicts
    keep the higher-priority pack's entry with a warning.
    """
    def merge_def(lo, hi, path):
        t_lo, t_hi = lo.get("type"), hi.get("type")
        if t_lo != t_hi:
            _mwarn(f"{ctx['path']}: conflicting model types '{t_lo}' vs "
                   f"'{t_hi}' at '{path}'; kept '{ctx['higher']}'s")
            return hi
        if t_lo == "minecraft:range_dispatch":
            if lo.get("property") != hi.get("property"):
                _mwarn(f"{ctx['path']}: range_dispatch on different properties "
                       f"at '{path}'; kept '{ctx['higher']}'s")
                return hi
            out = dict(lo)
            seen = {}
            for e in list(lo.get("entries", [])) + list(hi.get("entries", [])):
                th = e.get("threshold")
                if th in seen:
                    if _canon(seen[th]) != _canon(e):
                        _mwarn(f"{ctx['path']}: both packs define threshold "
                               f"{th} at '{path}'; kept '{ctx['higher']}'s")
                    seen[th] = e
                else:
                    seen[th] = e
            # entries keyed by threshold (higher wins conflicts), sorted
            # ascending as vanilla expects
            out["entries"] = [seen[th]
                              for th in sorted(seen, key=lambda x: (x is None, x))]
            if "fallback" in lo or "fallback" in hi:
                lo_f, hi_f = lo.get("fallback"), hi.get("fallback")
                if isinstance(lo_f, dict) and isinstance(hi_f, dict):
                    out["fallback"] = merge_def(lo_f, hi_f, path + ".fallback")
                else:
                    out["fallback"] = hi_f if "fallback" in hi else lo_f
            for k in ("property", "scale"):
                if k in hi:
                    out[k] = hi[k]
            return out
        if t_lo == "minecraft:select":
            if lo.get("property") != hi.get("property"):
                _mwarn(f"{ctx['path']}: select on different properties at "
                       f"'{path}'; kept '{ctx['higher']}'s")
                return hi
            out = dict(lo)
            seen = {}
            for c in list(lo.get("cases", [])) + list(hi.get("cases", [])):
                w = _canon(c.get("when"))
                if w in seen:
                    if _canon(seen[w]) != _canon(c):
                        _mwarn(f"{ctx['path']}: both packs define select case "
                               f"{c.get('when')} at '{path}'; kept "
                               f"'{ctx['higher']}'s")
                    seen[w] = c
                else:
                    seen[w] = c
            out["cases"] = list(seen.values())
            if "fallback" in hi:
                out["fallback"] = hi["fallback"]
            elif "fallback" in lo:
                out["fallback"] = lo["fallback"]
            return out
        if t_lo == "minecraft:condition":
            out = dict(lo)
            for k in ("on_true", "on_false"):
                if isinstance(lo.get(k), dict) and isinstance(hi.get(k), dict):
                    out[k] = merge_def(lo[k], hi[k], path + "." + k)
                elif k in hi:
                    out[k] = hi[k]
            return out
        if t_lo == "minecraft:composite":
            out = dict(lo)
            models, seen = [], set()
            for m in list(lo.get("models", [])) + list(hi.get("models", [])):
                c = _canon(m)
                if c not in seen:
                    seen.add(c)
                    models.append(m)
            out["models"] = models
            return out
        # leaf types (minecraft:model, minecraft:empty, ...) or unknown:
        # higher-priority wins
        if _canon(lo) != _canon(hi):
            _mnote(f"{ctx['path']}: model definition at '{path}' replaced by "
                   f"'{ctx['higher']}'s")
        return hi

    out = dict(lower)
    if "model" in lower or "model" in higher:
        lo_m, hi_m = lower.get("model"), higher.get("model")
        if isinstance(lo_m, dict) and isinstance(hi_m, dict):
            out["model"] = merge_def(lo_m, hi_m, "model")
        elif "model" in higher:
            out["model"] = hi_m
    for k, v in higher.items():
        if k != "model":
            out[k] = v
    return out


def merge_blockstate(lower, higher, ctx):
    rest_lo = {k: v for k, v in lower.items() if k not in ("variants", "multipart")}
    rest_hi = {k: v for k, v in higher.items() if k not in ("variants", "multipart")}
    out = merge_deep(rest_lo, rest_hi, ctx)
    lv, hv = lower.get("variants"), higher.get("variants")
    if isinstance(lv, dict) or isinstance(hv, dict):
        variants = dict(lv) if isinstance(lv, dict) else {}
        for k, v in (hv.items() if isinstance(hv, dict) else []):
            if k in variants and _canon(variants[k]) != _canon(v):
                _mwarn(f"{ctx['path']}: variant '{k}' defined by both; "
                       f"kept '{ctx['higher']}'s")
            variants[k] = v
        out["variants"] = variants
    lm, hm = lower.get("multipart"), higher.get("multipart")
    if isinstance(lm, list) or isinstance(hm, list):
        parts, seen = [], set()
        for part in list(lm or []) + list(hm or []):
            c = _canon(part)
            if c not in seen:
                seen.add(c)
                parts.append(part)
        out["multipart"] = parts
        _mnote(f"{ctx['path']}: concatenated multipart entries from both packs")
    return out


def merge_sounds(lower, higher, ctx):
    out = dict(lower)
    for event, hdef in higher.items():
        ldef = out.get(event)
        if event not in out:
            out[event] = hdef
            continue
        if not isinstance(ldef, dict) or not isinstance(hdef, dict):
            _mwarn(f"{ctx['path']}: event '{event}' conflict; kept '{ctx['higher']}'s")
            out[event] = hdef
            continue
        if hdef.get("replace"):
            out[event] = hdef
            _mnote(f"{ctx['path']}: event '{event}' replaced by '{ctx['higher']}' "
                   f"(replace: true)")
            continue
        merged = dict(ldef)
        for k, v in hdef.items():
            if k == "sounds" and isinstance(v, list):
                sounds = list(merged.get("sounds") or [])
                seen = {_canon(s) for s in sounds}
                for s in v:
                    if _canon(s) not in seen:
                        seen.add(_canon(s))
                        sounds.append(s)
                merged["sounds"] = sounds
            else:
                if k in merged and _canon(merged[k]) != _canon(v):
                    _mwarn(f"{ctx['path']}: event '{event}' key '{k}' differs; "
                           f"kept '{ctx['higher']}'s")
                merged[k] = v
        out[event] = merged
    return out


def merge_concat_key(lower, higher, ctx, key):
    rest_lo = {k: v for k, v in lower.items() if k != key}
    rest_hi = {k: v for k, v in higher.items() if k != key}
    out = merge_deep(rest_lo, rest_hi, ctx)
    items, seen = [], set()
    for lst in (lower.get(key), higher.get(key)):
        for it in lst or []:
            c = _canon(it)
            if c not in seen:
                seen.add(c)
                items.append(it)
    if key in lower or key in higher:
        out[key] = items
    return out


def merge_deep(lower, higher, ctx, _path="root"):
    """Generic recursive merge. higher wins scalar conflicts (with warning)."""
    if isinstance(lower, dict) and isinstance(higher, dict):
        out = dict(lower)
        for k, v in higher.items():
            out[k] = merge_deep(out[k], v, ctx, f"{_path}.{k}") if k in out else v
        return out
    if isinstance(lower, list) and isinstance(higher, list):
        seen = {_canon(x) for x in lower}
        out = list(lower)
        for x in higher:
            if _canon(x) not in seen:
                seen.add(_canon(x))
                out.append(x)
        return out
    if _canon(lower) != _canon(higher):
        _mwarn(f"{ctx['path']}: conflicting value at '{_path}'; "
               f"kept '{ctx['higher']}'s")
    return higher


def json_merge_dispatch(path):
    """Pick a merge strategy by path, or None for plain top-wins."""
    if os.path.basename(path) == "respackopts.json5":
        return None  # handled by the Respackopts merger
    p = path.lower()
    base = os.path.basename(p)
    if p.endswith(".json") and "/lang/" in p:
        return merge_lang
    if p.endswith(".json") and "/items/" in p:
        return merge_item_definition  # 1.21.4+ item definitions
    if p.endswith(".json") and "/models/" in p:
        return merge_model
    if p.endswith(".json") and "/blockstates/" in p:
        return merge_blockstate
    if base == "sounds.json":
        return merge_sounds
    if p.endswith(".json") and "/atlases/" in p:
        return lambda a, b, ctx: merge_concat_key(a, b, ctx, "sources")
    if p.endswith(".json") and "/font/" in p:
        return lambda a, b, ctx: merge_concat_key(a, b, ctx, "providers")
    if p.endswith(".json") and "/particles/" in p:
        return lambda a, b, ctx: merge_concat_key(a, b, ctx, "textures")
    if p.endswith(".mcmeta") or p.endswith(".json"):
        return merge_deep
    return None


def modrinth_owners(slug):
    """Best-effort author lookup for credits.txt."""
    try:
        members = api_get(f"{MODRINTH_API}/project/{slug}/members")
        owners = [m["user"]["username"] for m in members
                  if "owner" in str(m.get("role", "")).lower()]
        names = owners or [m["user"]["username"] for m in members]
        return ", ".join(names) if names else "unknown"
    except Exception:
        return "unknown"


def build_credits(manifest, source_credits, mc_version):
    from datetime import datetime, timezone
    L = []
    L.append(manifest.get("pack_name", "MergedPack"))
    L.append(f"Merged with packmerge on "
             f"{datetime.now(timezone.utc):%Y-%m-%d} for Minecraft {mc_version}.")
    L.append("")
    L.append("Sources, low to high priority (later packs win plain-file conflicts;")
    L.append("JSON files were smart-merged where possible):")
    L.append("")
    for i, c in enumerate(source_credits, 1):
        if c["kind"] == "modrinth":
            L.append(f"{i}. {c['title']} ({c['version']}) by "
                     f"{modrinth_owners(c['slug'])}")
            L.append(f"   {c['page']}")
        elif c["kind"] == "url":
            L.append(f"{i}. {c['name']} — direct download")
            L.append(f"   {c['url']}")
        else:
            L.append(f"{i}. {c['name']} — local file")
            L.append(f"   {c['path']}")
    L.append("")
    if manifest.get("overrides"):
        L.append("Per-file overrides applied:")
        for path, ref in manifest["overrides"].items():
            L.append(f"  {path}  <-  {ref}")
        L.append("")
    if manifest.get("exclude"):
        L.append("Excluded:")
        for rule in manifest["exclude"]:
            L.append(f"  {rule}")
        L.append("")
    L.append("Note: this merged pack bundles other authors' work. Personal use is")
    L.append("fine; check each pack's license before sharing it anywhere.")
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------- merge

def main():
    ap = argparse.ArgumentParser(description="Merge resource packs from URLs into one pack.")
    ap.add_argument("-m", "--manifest", default="manifest.json", help="path to manifest JSON")
    ap.add_argument("--check", action="store_true", help="only report which sources have updates")
    ap.add_argument("--force", action="store_true", help="redownload everything, ignore cache/lock")
    ap.add_argument("--mc-version", default=None, help="override manifest mc_version")
    ap.add_argument("-o", "--output", default=None, help="override manifest output path")
    ap.add_argument("--self-update", action="store_true",
                    help="update packmerge itself from its GitHub repo and exit")
    args = ap.parse_args()

    if args.self_update:
        import packmerge_update
        res = packmerge_update.check()
        if not res.get("ok"):
            sys.exit(f"[packmerge] self-update: {res.get('error')}")
        if not res["changed"]:
            print(f"[packmerge] already up to date (version {res['current']}).")
            return
        print(f"[packmerge] updating {res['current']} -> {res['remote']}: "
              f"{', '.join(res['changed'])}")
        ok, msg = packmerge_update.install(res)
        print(f"[packmerge] {msg}")
        sys.exit(0 if ok else 1)

    manifest_path = os.path.abspath(args.manifest)
    if args.manifest == "manifest.json" and not os.path.exists(manifest_path):
        # Be helpful when launched from another working directory (e.g. an
        # editor's terminal): fall back to a manifest.json next to the script.
        sibling = os.path.join(HERE, "manifest.json")
        if os.path.exists(sibling):
            log(f"using manifest next to the script: {sibling}")
            manifest_path = sibling
    base_dir = os.path.dirname(manifest_path)
    with open(manifest_path) as f:
        manifest = json.load(f)

    mc_version = args.mc_version or manifest.get("mc_version", "26.3")
    if mc_version == "latest":
        # Always target the newest release: newest Modrinth version regardless
        # of game version, and the newest known pack format for the output.
        pack_format = manifest.get("pack_format") or max(PACK_FORMATS.values())
    else:
        pack_format = manifest.get("pack_format") or PACK_FORMATS.get(mc_version)
        if pack_format is None:
            sys.exit(f"unknown pack_format for MC {mc_version}; set \"pack_format\" in the manifest "
                     f"(known: {', '.join(sorted(PACK_FORMATS))})")

    cache_dir = os.path.join(base_dir, ".packmerge-cache")
    os.makedirs(cache_dir, exist_ok=True)
    lock_path = manifest_path + ".lock.json"
    lock = {}
    if os.path.exists(lock_path) and not args.force:
        with open(lock_path) as f:
            lock = json.load(f)

    sources = manifest.get("sources", [])
    if not sources:
        sys.exit("manifest has no sources")

    seen_names = set()
    for i, src in enumerate(sources):
        name = src.get("name", f"source-{i}")
        if name in seen_names:
            warn(f"duplicate source name '{name}': overrides by name will "
                 f"use the last one")
        seen_names.add(name)

    # ---- resolve + fetch each source -> local zip path
    fetched = []  # (name, zip_path, identity_dict)
    source_credits = []
    new_lock = {}
    updates = []
    for i, src in enumerate(sources):
        name = src.get("name", f"source-{i}")
        lock_key = f"{i}:{name}"
        old = lock.get(lock_key, {})

        if "modrinth" in src:
            info = resolve_modrinth(src["modrinth"], mc_version,
                                    loader=("datapack" if src.get("pack_type") == "datapack" else None),
                                    version_id=src.get("modrinth_version_id"))
            ident = {"kind": "modrinth", "version_id": info["version_id"],
                     "version_number": info["version_number"]}
            changed = old.get("version_id") != info["version_id"]
            dest = os.path.join(cache_dir, f"{i}-{info['filename']}")
            if args.check:
                if changed:
                    updates.append(f"{name}: {old.get('version_number', 'none')} -> {info['version_number']}")
                fetched.append((name, None, ident))
                new_lock[lock_key] = ident
                source_credits.append({
                    "name": name, "kind": "modrinth", "title": info["title"],
                    "version": info["version_number"],
                    "page": f"https://modrinth.com/resourcepack/{info['slug']}",
                    "slug": info["slug"]})
                continue
            if changed or args.force or not os.path.exists(dest):
                log(f"{name}: fetching '{info['title']}' {info['version_number']}")
                for stale in os.listdir(cache_dir):
                    if stale.startswith(f"{i}-"):
                        os.remove(os.path.join(cache_dir, stale))
                download_file(info["url"], dest, info["sha512"])
            else:
                log(f"{name}: up to date ({info['version_number']}), using cache")
            new_lock[lock_key] = ident
            fetched.append((name, dest, ident))
            source_credits.append({
                "name": name, "kind": "modrinth", "title": info["title"],
                "version": info["version_number"],
                "page": f"https://modrinth.com/resourcepack/{info['slug']}",
                "slug": info["slug"]})

        elif "url" in src:
            url = src["url"]
            ident = {"kind": "url", "url": url}
            fname = url.split("?")[0].rstrip("/").split("/")[-1] or "pack.zip"
            dest = os.path.join(cache_dir, f"{i}-{fname}")
            if args.check:
                updates.append(f"{name}: cannot check plain URLs without downloading; use --force to refresh")
                fetched.append((name, None, ident))
                new_lock[lock_key] = {**ident, "etag": old.get("etag"), "last_modified": old.get("last_modified")}
                continue
            headers = {}
            if old.get("etag") and not args.force:
                headers["If-None-Match"] = old["etag"]
            elif old.get("last_modified") and not args.force:
                headers["If-Modified-Since"] = old["last_modified"]
            if not os.path.exists(dest) or args.force:
                headers = {}
            log(f"{name}: checking {url}")
            status, etag, last_mod = download_file(url, dest + ".tmp", extra_headers=headers or None)
            if status == "not-modified":
                log(f"{name}: not modified, using cache")
                if os.path.exists(dest + ".tmp"):
                    os.remove(dest + ".tmp")
            else:
                os.replace(dest + ".tmp", dest)
            new_lock[lock_key] = {**ident, "etag": etag, "last_modified": last_mod}
            fetched.append((name, dest, ident))
            source_credits.append({"name": name, "kind": "url", "url": url})

        elif "path" in src:
            p = os.path.join(base_dir, os.path.expanduser(src["path"]))
            if not os.path.exists(p):
                sys.exit(f"source '{name}': local path not found: {p}")
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 256), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            ident = {"kind": "path", "path": os.path.abspath(p),
                     "sha256": digest}
            if args.check and old.get("sha256") and old["sha256"] != digest:
                updates.append(f"{name}: local file changed since last run")
            new_lock[lock_key] = ident
            fetched.append((name, p, ident))
            source_credits.append({"name": name, "kind": "path",
                                   "path": os.path.abspath(p)})
            log(f"{name}: using local {p}")
        else:
            sys.exit(f"source '{name}': needs one of 'modrinth', 'url', or 'path'")

    if args.check:
        if updates:
            print("\nUpdates available:")
            for u in updates:
                print(f"  - {u}")
        else:
            print("\nEverything is up to date.")
        with open(lock_path, "w") as f:
            json.dump(new_lock, f, indent=2)
        return

    # ---- read packs
    pack_files = []  # list of (name, {path: bytes})
    for name, zpath, _ident in fetched:
        log(f"{name}: reading {os.path.basename(zpath)}")
        try:
            pack_files.append((name, read_pack_zip(zpath)))
        except zipfile.BadZipFile:
            sys.exit(f"source '{name}': not a valid zip file: {zpath}")

    by_name = {n: files for n, files in pack_files}
    by_index = {str(i): files for i, (n, files) in enumerate(pack_files)}

    def source_files(ref):
        key = str(ref)
        if key in by_name:
            return by_name[key], key
        if key in by_index:
            files = by_index[key]
            return files, pack_files[int(key)][0]
        sys.exit(f"override references unknown source '{ref}'")

    # ---- overlays: keep each source's version-overlay declarations alive.
    # The overlay directories merge as normal files below; their declarations
    # live in pack.mcmeta, which we regenerate, so collect them here.
    overlay_entries, overlay_renames = collect_overlay_entries(
        pack_files, pack_format >= 65)
    if overlay_entries:
        log(f"keeping {len(overlay_entries)} overlay declaration(s) "
            f"from source packs")
    if overlay_renames:
        renamed = []
        for name, files in pack_files:
            new_files = {}
            for path, data in files.items():
                for (sname, old_dir), new_dir in overlay_renames.items():
                    if sname == name and (path == old_dir
                                          or path.startswith(old_dir + "/")):
                        path = new_dir + path[len(old_dir):]
                        break
                new_files[path] = data
            renamed.append((name, new_files))
        pack_files = renamed

    # ---- merge: later sources win (game stack order: top wins),
    # except JSON files which are smart-merged when they collide
    smart_json = manifest.get("smart_json_merge", True)
    merged = {}
    origin = {}
    for name, files in pack_files:
        for path, data in files.items():
            if path == "pack.mcmeta":
                continue  # we generate our own
            if path in merged and smart_json:
                fn = json_merge_dispatch(path)
                if fn is not None:
                    try:
                        lo = json.loads(merged[path].decode("utf-8"))
                        hi = json.loads(data.decode("utf-8"))
                    except Exception as e:
                        _mwarn(f"{path}: not valid JSON ({e}); kept '{name}'s "
                               f"version (top-wins)")
                    else:
                        prev = origin[path].split(" (")[0]
                        ctx = {"path": path, "lower": prev, "higher": name}
                        merged[path] = json.dumps(
                            fn(lo, hi, ctx), indent=2,
                            ensure_ascii=False).encode()
                        origin[path] = (prev if name in prev.split("+")
                                        else f"{prev}+{name}")
                        merge_stats["json_merged"] += 1
                        continue
            merged[path] = data
            origin[path] = name

    # explicit per-file overrides (beat priority order)
    for path, ref in (manifest.get("overrides") or {}).items():
        files, real_name = source_files(ref)
        if path not in files:
            warn(f"override: '{path}' not found in source '{real_name}'; skipping")
            continue
        merged[path] = files[path]
        origin[path] = f"{real_name} (override)"

    # exclusions: exact paths, or directory prefixes ending in "/"
    for rule in manifest.get("exclude") or []:
        if rule.endswith("/"):
            doomed = [p for p in merged if p.startswith(rule)]
        else:
            doomed = [rule] if rule in merged else []
        for p in doomed:
            del merged[p]
            del origin[p]
        if doomed:
            log(f"excluded {len(doomed)} file(s) via '{rule}'")

    # ---- Respackopts: merge configs when several sources bring their own
    rpo_sources = []
    for name, files in pack_files:
        if "respackopts.json5" in files:
            try:
                cfg = json5_loads(files["respackopts.json5"].decode("utf-8"))
            except Exception as e:
                warn(f"'{name}': could not parse respackopts.json5 ({e}); "
                     f"config left as-is")
                continue
            rpo_sources.append((name, cfg))
    # Bake workbench-pending RPO values as defaults before merging: the
    # merged config then carries them, and they travel with the output.
    # Pending values for entries that do not exist are ignored loudly.
    rpo_defaults = manifest.get("rpo_defaults") or {}
    for name, cfg in rpo_sources:
        for path, val in (rpo_defaults.get(name) or {}).items():
            if not apply_rpo_default(cfg.get("conf", {}), path, val):
                warn(f"'{name}': pending RPO value for '{path}' does not "
                     f"match a config entry; ignoring it")
    rpo_ns = manifest.get("rpo_namespace_overrides") or {}
    merge_on = manifest.get("merge_respackopts", True)
    if len(rpo_sources) >= 2 and merge_on:
        merge_respackopts(merged, origin, rpo_sources, manifest)
    elif len(rpo_sources) == 1 and merge_on and rpo_ns.get(rpo_sources[0][0]):
        # A renamed single config runs the namespaced merge so the
        # rename lands; otherwise a single config passes through.
        merge_respackopts(merged, origin, rpo_sources, manifest)
    elif len(rpo_sources) == 1:
        log(f"Respackopts: single config from '{rpo_sources[0][0]}' "
            f"passed through unchanged")
        # Bake pending defaults into the passed-through config itself.
        defaults = rpo_defaults.get(rpo_sources[0][0]) or {}
        if defaults and "respackopts.json5" in merged:
            src_name = origin.get("respackopts.json5", "").split(" (")[0]
            if src_name == rpo_sources[0][0]:
                try:
                    cfg = json5_loads(
                        merged["respackopts.json5"].decode("utf-8"))
                except Exception:
                    cfg = None
                if cfg is not None:
                    changed = False
                    for path, val in defaults.items():
                        changed = apply_rpo_default(
                            cfg.get("conf", {}), path, val) or changed
                    if changed:
                        merged["respackopts.json5"] = (
                            json.dumps(cfg, indent=4) + "\n").encode()
    elif len(rpo_sources) >= 2:
        warn("multiple respackopts.json5 found but merge_respackopts is off; "
             "top-wins applies and some toggles may break")

    # ---- credits.txt
    merged["credits.txt"] = build_credits(
        manifest, source_credits, mc_version).encode()
    origin["credits.txt"] = "packmerge"

    # ---- write output
    pack_name = manifest.get("pack_name", "MergedPack")
    output = args.output or manifest.get("output", f"{pack_name}.zip")
    output = os.path.join(base_dir, output)
    out_dir = os.path.dirname(output)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    description = manifest.get("description", f"Merged with packmerge: {', '.join(n for n, _ in pack_files)}")

    # pack.png from the highest-priority source that has one
    icon = None
    for name, files in reversed(pack_files):
        if "pack.png" in files:
            icon = files["pack.png"]
            break

    tmp = output + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        mcmeta = build_pack_mcmeta(manifest, pack_format, description,
                                   overlay_entries or None)
        z.writestr("pack.mcmeta", json.dumps(mcmeta, indent=4))
        if icon:
            z.writestr("pack.png", icon)
        for path in sorted(merged):
            if path == "pack.png":
                continue  # already written from chosen source
            z.writestr(path, merged[path])

    os.replace(tmp, output)
    with open(lock_path, "w") as f:
        json.dump(new_lock, f, indent=2)

    log(f"done: {output} ({len(merged)} files, pack_format {pack_format})")
    if merge_stats["json_merged"]:
        log(f"smart-merged {merge_stats['json_merged']} JSON files "
            f"({merge_stats['warnings']} warnings above)")
    log("merge complete")


if __name__ == "__main__":
    main()
