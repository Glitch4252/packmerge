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
    python3 packmerge_update.py --install --force  # also overwrite files
                                            # you edited locally
    python3 packmerge_update.py --publish 2026.10.14.7  # regen version.json,
                                            # commit, push (one step)
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
    "packmerge_web.py",
    "packmerge_update.py",
    "packmerge-web-start.bat",
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


def _fetch(url, req, timeout, retries=2):
    """Fetch with one retry; the egress proxy occasionally hiccups."""
    last = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:
            last = e
    raise last


def fetch_json(url, token, timeout=20, retries=2):
    data = _fetch(url, _request(url, token), timeout, retries)
    return json.loads(data.decode("utf-8"))


def fetch_bytes(url, token, timeout=60, retries=2):
    req = urllib.request.Request(url, headers={"User-Agent": "packmerge-updater"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return _fetch(url, req, timeout, retries)


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


def remote_version_info(timeout=20, retries=2):
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
    info = fetch_json(url, token, timeout, retries)
    if not isinstance(info, dict) or "files" not in info:
        raise RuntimeError("version.json in the repo is malformed.")
    return info, repo, branch, token


def check(timeout=20, retries=2):
    """Compare local files to the repo. Returns a result dict.

    changed: files that differ from the repo but match what was last
        installed (clean updates).
    modified: files that differ from BOTH the repo and the last-installed
        version (the user edited them locally). These are never touched
        without explicit consent.
    """
    try:
        info, repo, branch, token = remote_version_info(timeout, retries)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    local = local_version()
    installed_files = local.get("files", {})
    has_record = bool(installed_files)
    remote_files = info.get("files", {})
    changed, modified = [], []
    for name, digest in remote_files.items():
        if "/" in name or "\\" in name or name not in MANAGED_FILES:
            continue  # never write outside the script folder
        p = os.path.join(HERE, name)
        if not os.path.exists(p):
            changed.append(name)  # missing entirely: install it
            continue
        local_digest = sha256_file(p)
        if local_digest == digest:
            continue  # up to date
        if not has_record or installed_files.get(name) == local_digest:
            changed.append(name)  # clean update (or no install record yet)
        else:
            modified.append(name)  # user edited it locally
    return {
        "ok": True,
        "repo": repo,
        "branch": branch,
        "current": local.get("version", "0"),
        "remote": info.get("version", "?"),
        "changed": sorted(changed),
        "modified": sorted(modified),
        "_files": remote_files,
        "_token": token,
    }


def install(result, overwrite_modified=False):
    """Download changed files and swap them in. Returns (ok, message).

    Files the user edited locally (result["modified"]) are kept as-is
    unless overwrite_modified is True; they are reported either way.
    """
    if not result.get("ok"):
        return False, result.get("error", "check failed")
    changed = list(result.get("changed", []))
    modified = list(result.get("modified", []))
    skipped = []
    if modified and not overwrite_modified:
        skipped = modified
    else:
        changed = sorted(set(changed) | set(modified))
    if not changed:
        if skipped:
            return True, ("Already up to date. Kept your edits to: "
                          + ", ".join(skipped) + ".")
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
    msg = (f"Updated {len(changed)} file(s) to version "
           f"{result['remote']}: {', '.join(changed)}")
    if skipped:
        msg += f". Kept your edits to: {', '.join(skipped)}"
    return True, msg


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


def publish(version):
    """One-step publish: regen version.json, commit, push.

    Eliminates the classic failure modes (forgetting version.json,
    committing it separately from the files, hashing before editing).
    """
    import subprocess as sp
    doc = make_version_file(version)
    files = sorted(doc["files"].keys()) + ["version.json"]
    files = [f for f in files if os.path.exists(os.path.join(HERE, f))]
    try:
        sp.run(["git", "add"] + files, cwd=HERE, check=True,
               capture_output=True, text=True)
    except FileNotFoundError:
        sys.exit("[packmerge-update] git not found on PATH. Install Git for "
                 "Windows or push via the GitHub website.")
    except sp.CalledProcessError as e:
        sys.exit(f"[packmerge-update] git add failed: {e.stderr.strip()}")
    # anything actually staged? (untracked side files don't count)
    r = sp.run(["git", "diff", "--cached", "--quiet"], cwd=HERE)
    if r.returncode == 0:
        print("[packmerge-update] nothing changed.")
        return
    r = sp.run(["git", "commit", "-m", version], cwd=HERE,
               capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("[packmerge-update] git commit failed:\n"
                 + r.stderr.strip()
                 + "\n(hint: git needs user.name and user.email set once: "
                 'git config --global user.name "you" && '
                 'git config --global user.email "you@example.com")')
    print(f"[packmerge-update] committed {version}, pushing…")
    r = sp.run(["git", "push"], cwd=HERE, capture_output=True, text=True)
    if r.returncode != 0 and "no upstream branch" in r.stderr:
        branch = sp.run(["git", "branch", "--show-current"], cwd=HERE,
                        capture_output=True, text=True).stdout.strip() or "main"
        r = sp.run(["git", "push", "--set-upstream", "origin", branch],
                   cwd=HERE, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("[packmerge-update] git push failed:\n" + r.stderr.strip())
    print(f"[packmerge-update] pushed {version}.")


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
    if "--publish" in argv:
        i = argv.index("--publish")
        try:
            version = argv[i + 1]
        except IndexError:
            sys.exit("usage: packmerge_update.py --publish VERSION")
        publish(version)
        return
    if "--install" in argv:
        force = "--force" in argv
        res = check()
        if not res.get("ok"):
            sys.exit(f"[packmerge-update] {res.get('error')}")
        if res.get("modified") and not force:
            print(f"[packmerge-update] keeping your edits to: "
                  f"{', '.join(res['modified'])}")
            print("[packmerge-update] add --force to overwrite them with "
                  "the repo version")
        if not res["changed"] and not (res.get("modified") and force):
            print(f"[packmerge-update] already up to date "
                  f"(version {res['current']}).")
            return
        if res["changed"] or force:
            show = sorted(set(res["changed"])
                          | (set(res["modified"]) if force else set()))
            print(f"[packmerge-update] {res['current']} -> {res['remote']}: "
                  f"{', '.join(show)}")
        ok, msg = install(res, overwrite_modified=force)
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
