"""
packmerge_web -- a browser UI for packmerge, served from your own machine.

    python3 packmerge_web.py [--port 8471] [--host 127.0.0.1] [--no-browser]

Then open the printed address in your browser. Same engine (packmerge.py)
and same state file as the desktop GUI -- this page is just another face.
Stdlib only: no pip packages, no JS frameworks.

Binds to 127.0.0.1 by default (this machine only). Pass --host 0.0.0.0 to
reach it from other devices on your network (e.g. your phone on WiFi).
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PACKMERGE_PY = os.path.join(HERE, "packmerge.py")
STATE_PATH = os.path.join(HERE, "packmerge-gui-state.json")
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


def start_run(mode):
    with run_lock:
        if run_info["running"]:
            return False
        run_info.update(running=True, lines=[], done=False, rc=0)
    threading.Thread(target=_run_worker, args=(mode,), daemon=True).start()
    return True


def _run_worker(mode):
    cmd = [sys.executable, PACKMERGE_PY, "-m", STATE_PATH]
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
            with run_lock:
                run_info["lines"].append(line)
        proc.wait()
        rc = proc.returncode
    except Exception as e:
        msg = f"[packmerge] failed to start: {e}\n"
        lines.append(msg)
        with run_lock:
            run_info["lines"].append(msg)
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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path == "/":
            return self._html()
        if parsed.path in ("/icon.png", "/favicon.ico"):
            return self._icon()
        if parsed.path == "/custom.css":
            return self._custom("custom.css", "text/css; charset=utf-8")
        if parsed.path == "/custom.js":
            return self._custom("custom.js", "text/javascript; charset=utf-8")
        if parsed.path == "/api/state":
            return self._json(load_state())
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
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[packmerge-web] stopped")


if __name__ == "__main__":
    main()
