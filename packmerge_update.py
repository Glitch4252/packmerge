#!/usr/bin/env python3
"""
packmerge_update -- self-update packmerge from its GitHub repo.

The repo holds the canonical files plus a version.json manifest:
    {"version": "2026.10.13.1",
     "files": {"packmerge.py": "<sha256>", ...}}

This module compares the local files against version.json, downloads only
what changed, and swaps the files in atomically. Stdlib only.

Configuration lives in packmerge-update.json next to this script:
    {"repo": "username/packmerge", "branch": "main", "token": ""}
The token is only needed for private repos (it is sent as a Bearer token
to raw.githubusercontent.com). It is never required for public repos.

Usage:
    python3 packmerge_update.py --check     # report what would change
    python3 packmerge_update.py --install   # download and apply the update
    python3 packmerge_update.py --make-version  # (maintainer) regenerate
                                            # version.json before pushing
"""

import hashlib
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "packmerge-update.json")
LOCAL_VERSION_PATH = os.path.join(HERE, "version.json")

# files managed by the updater (never touches state, cache, or lockfiles)
MANAGED_FILES = [
    "packmerge.py",
    "packmerge_gui.py",
    "packmerge_web.py",
    "packmerge_update.py",
    "README.md",
    "manifest.example.json",
    "version.json",
]


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            return cfg
    except Exception:
        pass
    return {}


def configured():
    cfg = load_config()
    return bool(cfg.get("repo"))


def _request(url, token):
    req = urllib.request.Request(
        url, headers={"User-Agent": "packmerge-updater",
                      "Accept": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return req


def fetch_json(url, token, timeout=20):
    with urllib.request.urlopen(_request(url, token),
                                 timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_bytes(url, token, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "packmerge-updater"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def local_version():
    try:
        with open(LOCAL_VERSION_PATH) as f:
            return json.load(f)
    except Exception:
        return {"version": "0", "files": {}}


def _base_url(repo, branch):
    override = os.environ.get("PACKMERGE_UPDATE_BASE")
    if override:
        return override.rstrip("/")
    return f"https://raw.githubusercontent.com/{repo}/{branch}"


def remote_version_info():
    """Fetch version.json from the repo. Raises on network/config errors."""
    cfg = load_config()
    repo = cfg.get("repo", "").strip().strip("/")
    branch = cfg.get("branch", "main").strip() or "main"
    token = cfg.get("token", "").strip()
    if not repo or "/" not in repo:
        raise RuntimeError(
            "packmerge-update.json is missing or has no \"repo\" "
            "(expected \"username/reponame\").")
    url = f"{_base_url(repo, branch)}/version.json"
    info = fetch_json(url, token)
    if not isinstance(info, dict) or "files" not in info:
        raise RuntimeError("version.json in the repo is malformed.")
    return info, repo, branch, token


def check():
    """Compare local files to the repo. Returns a result dict."""
    try:
        info, repo, branch, token = remote_version_info()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    local = local_version()
    remote_files = info.get("files", {})
    changed = []
    for name, digest in remote_files.items():
        if "/" in name or "\\" in name or name not in MANAGED_FILES:
            continue  # never write outside the script folder
        p = os.path.join(HERE, name)
        if not os.path.exists(p) or sha256_file(p) != digest:
            changed.append(name)
    return {
        "ok": True,
        "repo": repo,
        "branch": branch,
        "current": local.get("version", "0"),
        "remote": info.get("version", "?"),
        "changed": sorted(changed),
        "_files": remote_files,
        "_token": token,
    }


def install(result):
    """Download changed files and swap them in. Returns (ok, message)."""
    if not result.get("ok"):
        return False, result.get("error", "check failed")
    changed = result.get("changed", [])
    if not changed:
        return True, "Already up to date."
    repo = result["repo"]
    branch = result["branch"]
    token = result["_token"]
    remote_files = result["_files"]
    staged = []
    try:
        for name in changed:
            url = f"{_base_url(repo, branch)}/{name}"
            data = fetch_bytes(url, token)
            if hashlib.sha256(data).hexdigest() != remote_files[name]:
                raise RuntimeError(f"hash mismatch downloading {name}")
            tmp = os.path.join(HERE, name + ".update-tmp")
            with open(tmp, "wb") as f:
                f.write(data)
            staged.append((tmp, os.path.join(HERE, name)))
        for tmp, dest in staged:
            os.replace(tmp, dest)
        # record the new version locally so future checks compare correctly
        with open(LOCAL_VERSION_PATH, "w") as f:
            json.dump({"version": result["remote"],
                       "files": result["_files"]}, f, indent=2)
            f.write("\n")
    except Exception as e:
        for tmp, _dest in staged:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return False, f"Update failed: {e}"
    return True, (f"Updated {len(changed)} file(s) to version "
                  f"{result['remote']}: {', '.join(changed)}")


def restart_into(script):
    """Re-execute this Python with the given script (applies the update)."""
    path = os.path.join(HERE, script)
    os.execv(sys.executable, [sys.executable, path] + sys.argv[1:])


def make_version_file(version):
    """(Maintainer) regenerate version.json from the local files."""
    files = {}
    for name in MANAGED_FILES:
        if name == "version.json":
            continue
        p = os.path.join(HERE, name)
        if os.path.exists(p):
            files[name] = sha256_file(p)
    doc = {"version": version, "files": files}
    with open(LOCAL_VERSION_PATH, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    return doc


def main(argv):
    if "--make-version" in argv:
        i = argv.index("--make-version")
        try:
            version = argv[i + 1]
        except IndexError:
            sys.exit("usage: packmerge_update.py --make-version VERSION")
        doc = make_version_file(version)
        print(f"[packmerge-update] wrote version.json "
              f"({doc['version']}, {len(doc['files'])} files)")
        return
    if "--install" in argv:
        res = check()
        if not res.get("ok"):
            sys.exit(f"[packmerge-update] {res.get('error')}")
        if not res["changed"]:
            print(f"[packmerge-update] already up to date "
                  f"(version {res['current']}).")
            return
        print(f"[packmerge-update] {res['current']} -> {res['remote']}: "
              f"{', '.join(res['changed'])}")
        ok, msg = install(res)
        print(f"[packmerge-update] {msg}")
        sys.exit(0 if ok else 1)
    # default: --check
    res = check()
    if not res.get("ok"):
        sys.exit(f"[packmerge-update] {res.get('error')}")
    if res["changed"]:
        print(f"[packmerge-update] update available: {res['current']} -> "
              f"{res['remote']} ({', '.join(res['changed'])})")
    else:
        print(f"[packmerge-update] already up to date "
              f"(version {res['current']}).")


if __name__ == "__main__":
    main(sys.argv[1:])
