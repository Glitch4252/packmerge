#!/usr/bin/env python3
"""
packmerge_gui -- dark desktop front-end for packmerge (stdlib tkinter only).

Apple-HIG-inspired and dark by design (no theme switching). The app keeps
its own state file next to the script and autosaves every change, so there
is deliberately no save/open/new workflow: what you see is what will build.
"""

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
import urllib.request
from tkinter import filedialog, messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
PACKMERGE_PY = os.path.join(HERE, "packmerge.py")
STATE_PATH = os.path.join(HERE, "packmerge-gui-state.json")
MODRINTH_API = "https://api.modrinth.com/v2"

# ---------------------------------------------------------------- palette
BG = "#1e1e1e"        # window
PANEL = "#2d2d30"     # fields, lists
RAISED = "#3a3a3d"    # buttons
FG = "#f2f2f2"        # primary text
DIM = "#9d9da3"       # secondary text
ACCENT = "#0a84ff"    # macOS blue
ACCENT_HOVER = "#2f9bff"
BORDER = "#48484a"
LOG_BG = "#19191b"
TOOLTIP_BG = "#3a3a3c"


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


def source_label(src):
    name = src.get("name", "?")
    if "modrinth" in src:
        return f"{name}"
    if "url" in src:
        return f"{name}"
    if "path" in src:
        return f"{name}"
    return f"{name}  ?"


def modrinth_slug(value):
    m = re.search(r"modrinth\.com/(?:resourcepack|mod|datapack|plugin)/([A-Za-z0-9_-]+)",
                  value)
    return m.group(1) if m else value.strip()


def looks_like_modrinth(value):
    return "modrinth.com" in value or re.fullmatch(r"[A-Za-z0-9_-]+", value.strip()) is not None


def name_from_url(url):
    fname = url.split("?")[0].rstrip("/").split("/")[-1]
    if fname.lower().endswith(".zip"):
        fname = fname[:-4]
    # turn "my-cool-pack-v3" into "My Cool Pack v3"
    name = re.sub(r"[-_]+", " ", fname).strip()
    return name or "Downloaded pack"


def fetch_modrinth_title(slug):
    try:
        req = urllib.request.Request(
            f"{MODRINTH_API}/project/{slug}",
            headers={"User-Agent": "packmerge-gui/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp).get("title")
    except Exception:
        return None
# ---------------------------------------------------------------- tooltip

class ToolTip:
    """Small dark hover tooltip for a widget."""

    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.tip = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _event):
        if self.tip is not None:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 10
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", background=TOOLTIP_BG,
                 foreground=FG, relief="solid", borderwidth=1,
                 wraplength=340, padx=10, pady=8,
                 font=("TkDefaultFont", 9)).pack()

    def hide(self, _event):
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
# ---------------------------------------------------------------- dialogs

class PromptDialog(tk.Toplevel):
    """Single-field dark input dialog."""

    def __init__(self, parent, title, prompt):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=BG)
        self.result = None
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text=prompt,
                  style="Dim.TLabel").pack(padx=20, pady=(18, 8))
        self.entry = ttk.Entry(self, width=52)
        self.entry.pack(padx=20, pady=(0, 6))
        self.entry.focus_set()
        self.entry.bind("<Return>", lambda _e: self.ok())
        self.entry.bind("<Escape>", lambda _e: self.cancel())

        btns = ttk.Frame(self)
        btns.pack(pady=(0, 18))
        ttk.Button(btns, text="Cancel", command=self.cancel).pack(side="left", padx=4)
        ttk.Button(btns, text="Add", style="Accent.TButton",
                   command=self.ok).pack(side="left", padx=4)

        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
        self.wait_window(self)

    def ok(self):
        value = self.entry.get().strip()
        if value:
            self.result = value
        self.destroy()

    def cancel(self):
        self.destroy()


class OverrideDialog(tk.Toplevel):
    """Pick a file path and which source wins it."""

    def __init__(self, parent, sources):
        super().__init__(parent)
        self.title("Force a file's source")
        self.configure(bg=BG)
        self.result = None
        self.transient(parent)
        self.grab_set()

        ttk.Label(self, text="File path inside the pack:",
                  style="Dim.TLabel").grid(row=0, column=0, sticky="w",
                                           padx=20, pady=(18, 4))
        self.path_entry = ttk.Entry(self, width=48)
        self.path_entry.grid(row=1, column=0, padx=20, pady=(0, 8),
                             sticky="ew")
        self.path_entry.focus_set()
        ttk.Label(self, text="Source that should win this file:",
                  style="Dim.TLabel").grid(row=2, column=0, sticky="w",
                                           padx=20, pady=(0, 4))
        names = [s.get("name", f"source-{i}") for i, s in enumerate(sources)]
        self.src_var = tk.StringVar(value=names[-1] if names else "")
        ttk.OptionMenu(self, self.src_var, self.src_var.get(),
                       *names).grid(row=3, column=0, padx=20, pady=(0, 8),
                                    sticky="ew")

        btns = ttk.Frame(self)
        btns.grid(row=4, column=0, pady=(0, 18))
        ttk.Button(btns, text="Cancel",
                   command=self.destroy).pack(side="left", padx=4)
        ttk.Button(btns, text="Add", style="Accent.TButton",
                   command=self.ok).pack(side="left", padx=4)

        self.columnconfigure(0, weight=1)
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")
        self.wait_window(self)

    def ok(self):
        path = self.path_entry.get().strip()
        if not path:
            return
        self.result = (path, self.src_var.get())
        self.destroy()
# --------------------------------------------------------------------- app

class PackmergeGui(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("packmerge")
        self.geometry("880x760")
        self.configure(bg=BG)
        self._apply_theme()

        self.state = self._load_state()
        self.log_queue = queue.Queue()
        self.running = False
        self.log_visible = False
        self._last_lines = []
        self._last_rc = 0
        self._suspend_traces = False
        self._auto_output = True

        self._build_ui()
        self._state_to_ui()
        self._poll_log()

        if not os.path.exists(PACKMERGE_PY):
            messagebox.showerror(
                "packmerge.py not found",
                f"Couldn't find packmerge.py next to this app:\n{PACKMERGE_PY}\n\n"
                "Keep both files in the same folder.", parent=self)

    # ------------------------------------------------------------- theme
    def _apply_theme(self):
        if sys.platform == "win32":
            fam, size = "Segoe UI", 10
        elif sys.platform == "darwin":
            fam, size = "TkDefaultFont", 12
        else:
            fam, size = "TkDefaultFont", 10
        self._font = (fam, size)

        s = ttk.Style(self)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=BG, foreground=FG, font=self._font)
        s.configure("TFrame", background=BG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Dim.TLabel", background=BG, foreground=DIM)
        s.configure("Title.TLabel", background=BG, foreground=FG,
                    font=(fam, 16, "bold"))
        s.configure("Subtitle.TLabel", background=BG, foreground=DIM,
                    font=(fam, size))
        s.configure("TButton", background=RAISED, foreground=FG, borderwidth=0,
                    padding=(14, 7), focuscolor=BG)
        s.map("TButton",
              background=[("active", "#4b4b4f"), ("disabled", PANEL)],
              foreground=[("disabled", DIM)])
        s.configure("Accent.TButton", background=ACCENT, foreground="white",
                    borderwidth=0, padding=(14, 7), focuscolor=BG,
                    font=(fam, size, "bold"))
        s.map("Accent.TButton",
              background=[("active", ACCENT_HOVER), ("disabled", "#3a3a3c")],
              foreground=[("disabled", DIM)])
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TEntry", fieldbackground=PANEL, foreground=FG,
                    bordercolor=BORDER, insertcolor=FG, padding=7)
        s.map("TEntry", bordercolor=[("focus", ACCENT)])
        s.configure("TLabelFrame", background=BG, foreground=FG,
                    bordercolor=BORDER, relief="flat", borderwidth=1)
        s.configure("TLabelFrame.Label", background=BG, foreground=DIM,
                    font=(fam, size, "bold"))
        s.configure("TScrollbar", background=BG, troughcolor=PANEL,
                    bordercolor=BG, arrowcolor=DIM)
        s.configure("TOptionMenu", background=RAISED, foreground=FG,
                    arrowcolor=FG)
        self.option_add("*TCombobox*Listbox.background", PANEL)
        self.option_add("*TCombobox*Listbox.foreground", FG)

    # ------------------------------------------------------------ layout
    def _build_ui(self):
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)

        # header
        ttk.Label(root, text="packmerge", style="Title.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(root, text="Turn a stack of resource packs into one.",
                  style="Subtitle.TLabel").grid(row=1, column=0, sticky="w",
                                                pady=(0, 12))

        # pack details
        ps = ttk.LabelFrame(root, text="Pack", padding=12)
        ps.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ps.columnconfigure(1, weight=1)
        ttk.Label(ps, text="Name").grid(row=0, column=0, sticky="w", pady=3)
        self.name_var = tk.StringVar()
        ttk.Entry(ps, textvariable=self.name_var).grid(
            row=0, column=1, sticky="ew", padx=(10, 0), pady=3)
        ttk.Label(ps, text="Output file").grid(row=1, column=0, sticky="w", pady=3)
        of = ttk.Frame(ps)
        of.grid(row=1, column=1, sticky="ew", padx=(10, 0), pady=3)
        of.columnconfigure(0, weight=1)
        self.output_var = tk.StringVar()
        ttk.Entry(of, textvariable=self.output_var).grid(
            row=0, column=0, sticky="ew")
        ttk.Button(of, text="Browse…",
                   command=self.browse_output).grid(row=0, column=1, padx=(8, 0))
        ttk.Label(ps, text="Description").grid(row=2, column=0, sticky="w", pady=3)
        self.desc_var = tk.StringVar()
        ttk.Entry(ps, textvariable=self.desc_var).grid(
            row=2, column=1, sticky="ew", padx=(10, 0), pady=3)

        self.smart_var = tk.BooleanVar(value=True)
        self.rpo_var = tk.BooleanVar(value=True)
        smart_cb = ttk.Checkbutton(ps, text="Smart JSON merge",
                                   variable=self.smart_var)
        smart_cb.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ToolTip(smart_cb,
                "When two packs contain the same JSON file, combine them "
                "instead of letting the higher pack replace the whole file.\n\n"
                "For example: two packs that each restyle different GUI screens, "
                "or each add custom bow models, will both survive in the one "
                "merged file. Conflicts still warn you.\n\n"
                "Turn this off for strict top-pack-wins on every file.")
        rpo_cb = ttk.Checkbutton(ps, text="Merge Respackopts configs",
                                 variable=self.rpo_var)
        rpo_cb.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))
        ToolTip(rpo_cb,
                "If several packs each bring their own settings screen "
                "(Respackopts), merge them into a single settings menu with "
                "one section per pack.\n\n"
                "Turn this off and only the top pack's settings will work.")

        # sources
        sf = ttk.LabelFrame(root, text="Sources  —  bottom of the list wins",
                            padding=12)
        sf.grid(row=3, column=0, sticky="nsew", pady=(0, 10))
        sf.columnconfigure(0, weight=1)
        self.src_list = tk.Listbox(sf, height=7, selectmode="browse",
                                   exportselection=False, bg=PANEL, fg=FG,
                                   selectbackground=ACCENT,
                                   selectforeground="white",
                                   highlightthickness=0, borderwidth=0,
                                   activestyle="none")
        self.src_list.grid(row=0, column=0, rowspan=3, sticky="nsew",
                           padx=(0, 10))
        sb = ttk.Scrollbar(sf, orient="vertical",
                           command=self.src_list.yview)
        sb.grid(row=0, column=0, rowspan=3, sticky="nse", padx=(0, 10))
        self.src_list.configure(yscrollcommand=sb.set)
        self.src_list.bind("<<ListboxSelect>>",
                           lambda _e: self._update_remove_src_btn())

        ttk.Button(sf, text="Add URL…", width=16,
                   command=self.add_url).grid(row=0, column=1, sticky="new",
                                              pady=2)
        ttk.Button(sf, text="Add local file…", width=16,
                   command=self.add_local).grid(row=1, column=1, sticky="new",
                                                pady=2)
        self.remove_src_btn = ttk.Button(sf, text="Remove", width=16,
                                           command=self.remove_source)
        self.remove_src_btn.grid(row=2, column=1, sticky="new", pady=2)
        ttk.Button(sf, text="Move up", width=16,
                   command=lambda: self.move_source(-1)).grid(
            row=0, column=2, sticky="new", pady=2, padx=(8, 0))
        ttk.Button(sf, text="Move down", width=16,
                   command=lambda: self.move_source(1)).grid(
            row=1, column=2, sticky="new", pady=2, padx=(8, 0))
        self.build_btn = ttk.Button(sf, text="Build merged pack", width=16,
                                    style="Accent.TButton", command=self.build)
        self.build_btn.grid(row=2, column=2, sticky="new", pady=2, padx=(8, 0))

        # overrides + excludes
        oe = ttk.Frame(root)
        oe.grid(row=4, column=0, sticky="nsew", pady=(0, 10))
        oe.columnconfigure(0, weight=1)
        oe.columnconfigure(1, weight=1)

        of = ttk.LabelFrame(oe, text="Per-file overrides", padding=12)
        of.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        of.columnconfigure(0, weight=1)
        ttk.Label(of, style="Dim.TLabel", wraplength=340, justify="left",
                  text="Force a specific file to come from a specific pack, "
                       "beating the priority order above. Useful when you like "
                       "one pack overall but prefer another pack's version of "
                       "a single texture or model.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.ov_list = tk.Listbox(of, height=4, selectmode="browse",
                                  exportselection=False, bg=PANEL, fg=FG,
                                  selectbackground=ACCENT,
                                  selectforeground="white",
                                  highlightthickness=0, borderwidth=0,
                                  activestyle="none")
        self.ov_list.grid(row=1, column=0, columnspan=2, sticky="nsew")
        ttk.Button(of, text="Add…",
                   command=self.add_override).grid(row=2, column=0,
                                                   sticky="ew", pady=(8, 0))
        ttk.Button(of, text="Remove",
                   command=self.remove_override).grid(row=2, column=1,
                                                      sticky="ew", pady=(8, 0),
                                                      padx=(8, 0))

        ef = ttk.LabelFrame(oe, text="Excluded paths", padding=12)
        ef.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        ef.columnconfigure(0, weight=1)
        ttk.Label(ef, style="Dim.TLabel", wraplength=340, justify="left",
                  text="Drop files or whole folders from the merged pack. "
                       "Useful for stripping parts of a pack you don't want, "
                       "like its OptiFine extras. End a path with / to drop "
                       "an entire folder.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.ex_list = tk.Listbox(ef, height=4, selectmode="browse",
                                  exportselection=False, bg=PANEL, fg=FG,
                                  selectbackground=ACCENT,
                                  selectforeground="white",
                                  highlightthickness=0, borderwidth=0,
                                  activestyle="none")
        self.ex_list.grid(row=1, column=0, columnspan=2, sticky="nsew")
        ttk.Button(ef, text="Add…",
                   command=self.add_exclude).grid(row=2, column=0,
                                                  sticky="ew", pady=(8, 0))
        ttk.Button(ef, text="Remove",
                   command=self.remove_exclude).grid(row=2, column=1,
                                                     sticky="ew", pady=(8, 0),
                                                     padx=(8, 0))

        # actions
        af = ttk.Frame(root)
        af.grid(row=5, column=0, sticky="ew", pady=(0, 4))
        self.check_btn = ttk.Button(af, text="Check for updates",
                                    command=self.check_updates)
        self.check_btn.pack(side="left")
        ttk.Button(af, text="Open output folder",
                   command=self.open_output_folder).pack(side="left", padx=8)
        self.log_btn = ttk.Button(af, text="Log", command=self.toggle_log)
        self.log_btn.pack(side="left")
        ttk.Button(af, text="App updates",
                   command=self.check_app_updates).pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(af, textvariable=self.status_var,
                  style="Dim.TLabel").pack(side="right")

        # log (hidden by default)
        self.log_frame = ttk.LabelFrame(root, text="Log", padding=12)
        self.log_frame.grid(row=6, column=0, sticky="nsew", pady=(6, 0))
        self.log_frame.columnconfigure(0, weight=1)
        self.log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(self.log_frame, height=10, state="disabled",
                                wrap="word", bg=LOG_BG, fg="#d7d7db",
                                insertbackground=FG, highlightthickness=0,
                                borderwidth=0, font=("TkFixedFont", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(self.log_frame, orient="vertical",
                            command=self.log_text.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=lsb.set)
        self.log_frame.grid_remove()

        root.rowconfigure(3, weight=1)
        root.rowconfigure(4, weight=1)
        root.rowconfigure(6, weight=2)

    # ------------------------------------------------------- state (auto)
    def _load_state(self):
        try:
            with open(STATE_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sources"), list):
                base = default_state()
                base.update(data)
                return base
        except Exception:
            pass
        return default_state()

    def _save_state(self):
        try:
            with open(STATE_PATH, "w") as f:
                json.dump(self.state, f, indent=2)
                f.write("\n")
        except Exception:
            pass  # autosave is best-effort; build surfaces real errors

    def _sync_state_from_ui(self):
        self.state["pack_name"] = self.name_var.get().strip()
        self.state["description"] = self.desc_var.get().strip()
        self.state["output"] = self.output_var.get().strip()
        self.state["smart_json_merge"] = bool(self.smart_var.get())
        self.state["merge_respackopts"] = bool(self.rpo_var.get())
        # mc_version is always latest; the GUI doesn't expose it
        self.state["mc_version"] = "latest"

    def _state_to_ui(self):
        st = self.state
        self._suspend_traces = True
        try:
            self.name_var.set(st.get("pack_name", ""))
            self.output_var.set(st.get("output", ""))
            self.desc_var.set(st.get("description", ""))
            self.smart_var.set(bool(st.get("smart_json_merge", True)))
            self.rpo_var.set(bool(st.get("merge_respackopts", True)))
        finally:
            self._suspend_traces = False
        name = st.get("pack_name", "")
        out = st.get("output", "")
        self._auto_output = (not out) or (bool(name) and out == name + ".zip")
        self.refresh_lists()

        self.name_var.trace_add("write", self._on_name_change)
        self.output_var.trace_add("write", self._on_output_change)
        self.desc_var.trace_add("write", lambda *_: self._setting_changed())
        self.smart_var.trace_add("write", lambda *_: self._setting_changed())
        self.rpo_var.trace_add("write", lambda *_: self._setting_changed())

    def _setting_changed(self):
        if self._suspend_traces:
            return
        self._sync_state_from_ui()
        self._save_state()

    def _on_name_change(self, *_):
        if self._suspend_traces:
            return
        name = self.name_var.get().strip()
        if self._auto_output:
            self._suspend_traces = True
            try:
                self.output_var.set(f"{name}.zip" if name else "")
            finally:
                self._suspend_traces = False
        self._setting_changed()

    def _on_output_change(self, *_):
        if self._suspend_traces:
            return
        # the user took manual control of the output name; stop mimicking
        self._auto_output = False
        self._setting_changed()

    def refresh_lists(self):
        self.src_list.delete(0, "end")
        for src in self.state.get("sources", []):
            kind = ("Modrinth" if "modrinth" in src
                    else "URL" if "url" in src else "File")
            self.src_list.insert("end", f"{src.get('name', '?')}   ·   {kind}")
        self.ov_list.delete(0, "end")
        for path, ref in (self.state.get("overrides") or {}).items():
            self.ov_list.insert("end", f"{path}  →  {ref}")
        self.ex_list.delete(0, "end")
        for rule in self.state.get("exclude") or []:
            self.ex_list.insert("end", rule)
        self._update_remove_src_btn()

    def _update_remove_src_btn(self):
        """Remove removes the selection; with none selected it clears all."""
        srcs = self.state.get("sources", [])
        sel = self.src_list.curselection()
        if not srcs:
            self.remove_src_btn.configure(text="Remove", state="disabled")
        elif sel:
            self.remove_src_btn.configure(text="Remove", state="normal")
        else:
            self.remove_src_btn.configure(text="Clear all", state="normal")

    # ---------------------------------------------------------- sources
    def _add_source(self, src):
        self.state.setdefault("sources", []).append(src)
        self._save_state()
        self.refresh_lists()
        self.src_list.selection_clear(0, "end")
        self.src_list.selection_set("end")
        self.src_list.see("end")
        self._update_remove_src_btn()

    def add_url(self):
        dlg = PromptDialog(self, "Add pack",
                           "Paste a Modrinth link or a direct zip URL.")
        if not dlg.result:
            return
        url = dlg.result
        if looks_like_modrinth(url):
            slug = modrinth_slug(url)
            src = {"name": slug, "modrinth": url}
            self._add_source(src)
            # fill in the real pack title in the background
            threading.Thread(target=self._resolve_title, args=(src, slug),
                             daemon=True).start()
        else:
            src = {"name": name_from_url(url), "url": url}
            self._add_source(src)

    def _resolve_title(self, src, slug):
        title = fetch_modrinth_title(slug)
        if title and src in self.state.get("sources", []):
            src["name"] = title
            # hand back to the main thread via the log queue (thread-safe)
            self.log_queue.put(("title-resolved",))

    def _title_resolved(self):
        self._save_state()
        self.refresh_lists()

    def add_local(self):
        path = filedialog.askopenfilename(
            title="Choose a resource pack",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")],
            parent=self)
        if not path:
            return
        try:
            rel = os.path.relpath(path, HERE)
        except ValueError:
            rel = path
        if not rel.startswith(".."):
            path = rel
        name = os.path.splitext(os.path.basename(path))[0]
        self._add_source({"name": name or "Local pack", "path": path})

    def remove_source(self):
        sel = self.src_list.curselection()
        if sel:
            del self.state["sources"][sel[0]]
            self._save_state()
            self.refresh_lists()
            return
        srcs = self.state.get("sources", [])
        if not srcs:
            return
        n = len(srcs)
        if messagebox.askyesno(
                "Clear all sources",
                f"Remove all {n} pack{'s' if n != 1 else ''} from the list?\n\n"
                "Your per-file overrides and excluded paths are kept.",
                parent=self):
            self.state["sources"] = []
            self._save_state()
            self.refresh_lists()

    def move_source(self, delta):
        sel = self.src_list.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + delta
        srcs = self.state["sources"]
        if 0 <= j < len(srcs):
            srcs[i], srcs[j] = srcs[j], srcs[i]
            self._save_state()
            self.refresh_lists()
            self.src_list.selection_set(j)
            self._update_remove_src_btn()

    # ------------------------------------------------- overrides/excludes
    def add_override(self):
        if not self.state.get("sources"):
            messagebox.showinfo("No sources", "Add at least one pack first.",
                                parent=self)
            return
        dlg = OverrideDialog(self, self.state["sources"])
        if dlg.result:
            path, ref = dlg.result
            self.state.setdefault("overrides", {})[path] = ref
            self._save_state()
            self.refresh_lists()

    def remove_override(self):
        sel = self.ov_list.curselection()
        if not sel:
            return
        path = list(self.state.get("overrides", {}))[sel[0]]
        del self.state["overrides"][path]
        self._save_state()
        self.refresh_lists()

    def add_exclude(self):
        dlg = PromptDialog(self, "Exclude a path",
                           "Path to drop (end with / to drop a whole folder).")
        if dlg.result:
            self.state.setdefault("exclude", []).append(dlg.result)
            self._save_state()
            self.refresh_lists()

    def remove_exclude(self):
        sel = self.ex_list.curselection()
        if not sel:
            return
        del self.state["exclude"][sel[0]]
        self._save_state()
        self.refresh_lists()

    # ------------------------------------------------------ file actions
    def browse_output(self):
        path = filedialog.asksaveasfilename(
            title="Output file",
            defaultextension=".zip",
            initialfile=self.output_var.get() or "MergedPack.zip",
            filetypes=[("Zip files", "*.zip"), ("All files", "*.*")],
            parent=self)
        if path:
            try:
                rel = os.path.relpath(path, HERE)
                if not rel.startswith(".."):
                    path = rel
            except ValueError:
                pass
            self.output_var.set(path)  # trace marks output as manual

    def open_output_folder(self):
        self._sync_state_from_ui()
        out = self.state.get("output") or (
            (self.state.get("pack_name") or "MergedPack") + ".zip")
        folder = os.path.dirname(os.path.abspath(os.path.join(HERE, out)))
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Couldn't open folder", str(e), parent=self)

    # ------------------------------------------------------------- log
    def toggle_log(self):
        self._show_log(not self.log_visible)

    def _show_log(self, show):
        self.log_visible = show
        if show:
            self.log_frame.grid()
            self.log_btn.configure(text="Hide log")
        else:
            self.log_frame.grid_remove()
            self.log_btn.configure(text="Log")

    def _append_log_line(self, line):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_log(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if item is None:
                    self._on_run_finished()
                elif isinstance(item, tuple) and item[0] == "title-resolved":
                    self._title_resolved()
                else:
                    self._append_log_line(item)
        except queue.Empty:
            pass
        self.after(100, self._poll_log)

    # ------------------------------------------------------------- runs
    def set_running(self, running, status=None):
        self.running = running
        state = "disabled" if running else "normal"
        self.check_btn.configure(state=state)
        self.build_btn.configure(state=state)
        if status:
            self.status_var.set(status)

    def _worker(self, cmd):
        lines = []
        rc = 1
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
            for line in proc.stdout:
                lines.append(line)
                self.log_queue.put(line)
            proc.wait()
            rc = proc.returncode
        except Exception as e:
            lines.append(f"[packmerge] failed to start: {e}\n")
            self.log_queue.put(lines[-1])
        finally:
            self._last_lines = lines
            self._last_rc = rc
            self.log_queue.put(None)

    def _run(self, args, working_label, is_check=False):
        if self.running:
            return
        # validation errors happen before any merging: plain alert, no log
        if not self.state.get("sources"):
            messagebox.showerror("Nothing to build",
                                 "Add at least one pack first.", parent=self)
            return
        if not self.name_var.get().strip():
            messagebox.showerror("Name your pack",
                                 "Give your pack a name first.", parent=self)
            return
        self._sync_state_from_ui()
        if not self.state["output"]:
            self.state["output"] = (self.state["pack_name"] or "MergedPack") + ".zip"
        self._save_state()
        self._is_check = is_check
        cmd = [sys.executable, PACKMERGE_PY, "-m", STATE_PATH] + args
        self.set_running(True, working_label)
        threading.Thread(target=self._worker, args=(cmd,), daemon=True).start()

    def check_updates(self):
        self._run(["--check"], "Checking for updates…", is_check=True)

    def build(self):
        self._run([], "Building…")

    # ------------------------------------------------- app self-updates
    def check_app_updates(self):
        try:
            import packmerge_update
        except ImportError:
            messagebox.showinfo("App updates",
                                "packmerge_update.py is missing from the "
                                "script folder.", parent=self)
            return
        if not packmerge_update.configured():
            messagebox.showinfo(
                "App updates",
                "App updates aren't set up yet.\n\n"
                "Set \"repo\" in packmerge-update.json next to the script\n"
                "(\"username/reponame\").", parent=self)
            return
        self.status_var.set("Checking for app updates…")
        self.update_idletasks()
        threading.Thread(target=self._app_update_check_worker,
                         daemon=True).start()

    def _app_update_check_worker(self):
        import packmerge_update
        res = packmerge_update.check()
        self.after(0, lambda: self._app_update_check_done(res))

    def _app_update_check_done(self, res):
        if not res.get("ok"):
            self.status_var.set("Ready.")
            messagebox.showerror("App updates",
                                 f"Check failed:\n{res.get('error')}",
                                 parent=self)
            return
        if not res["changed"]:
            self.status_var.set(f"App is up to date (v{res['current']}).")
            return
        self.status_var.set("Ready.")
        files = "\n".join(res["changed"])
        if messagebox.askyesno(
                "App update available",
                f"Version {res['current']} → {res['remote']}.\n\n"
                f"Files to replace:\n{files}\n\n"
                "Install now? The app will restart.",
                parent=self):
            self.status_var.set("Installing app update…")
            self.update_idletasks()
            threading.Thread(target=self._app_update_install_worker,
                             args=(res,), daemon=True).start()

    def _app_update_install_worker(self, res):
        import packmerge_update
        ok, msg = packmerge_update.install(res)
        self.after(0,
                   lambda: self._app_update_install_done(ok, msg, res))

    def _app_update_install_done(self, ok, msg, res):
        import packmerge_update
        if not ok or not res.get("changed"):
            self.status_var.set(msg if ok else "Ready.")
            if not ok:
                messagebox.showerror("App updates", msg, parent=self)
            return
        messagebox.showinfo("App updates", msg + "\n\nRestarting now.",
                            parent=self)
        packmerge_update.restart_into("packmerge_gui.py")

    def _summarize_error(self, rc, text):
        for line in reversed(text.splitlines()):
            s = line.strip()
            if not s or s.startswith("[packmerge] downloading"):
                continue
            if re.search(r"(?i)(error|traceback|failed|mismatch|not a valid)", s):
                return s[-300:]
        return f"packmerge exited with code {rc}."

    def _on_run_finished(self):
        rc, text = self._last_rc, "".join(self._last_lines)
        self.set_running(False)
        failed = rc != 0 or "Traceback" in text
        if failed:
            self._error_popup(self._summarize_error(rc, text))
        elif self._is_check and "updates available" in text.lower():
            self._show_log(True)
            self.status_var.set("Updates available — see the log.")
        elif "WARNING" in text:
            self.status_var.set("Done — with warnings. Open the log for details.")
        else:
            self.status_var.set("Done.")

    def _error_popup(self, summary):
        dlg = tk.Toplevel(self)
        dlg.title("Something went wrong")
        dlg.configure(bg=BG)
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(False, False)

        ttk.Label(dlg, text="Something went wrong.",
                  font=(self._font[0], 13, "bold")).pack(
            padx=24, pady=(20, 6))
        ttk.Label(dlg, text=summary, style="Dim.TLabel",
                  wraplength=400, justify="left").pack(padx=24, pady=(0, 16))

        btns = ttk.Frame(dlg)
        btns.pack(pady=(0, 20))
        ttk.Button(btns, text="Read Log", style="Accent.TButton",
                   command=lambda: (self._show_log(True),
                                    dlg.destroy())).pack(side="left", padx=4)
        ttk.Button(btns, text="OK",
                   command=dlg.destroy).pack(side="left", padx=4)
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 2
        dlg.geometry(f"+{x}+{y}")


def main():
    try:
        app = PackmergeGui()
    except tk.TclError as e:
        sys.exit(f"Couldn't open a window ({e}). "
                 "On a headless machine, run packmerge.py directly instead.")
    app.mainloop()


if __name__ == "__main__":
    main()
