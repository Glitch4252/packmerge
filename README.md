# packmerge

Merge Minecraft: Java Edition resource packs into a single pack, deterministically,
so updating is one command instead of an afternoon of manual frankensteining.

Prefer buttons to JSON? Two frontends, same engine:
- `python3 packmerge_gui.py` — a small desktop app, dark, Apple-HIG-inspired,
  stdlib tkinter only (`sudo pacman -S tk` / `sudo apt install python3-tk`
  if it's missing).
- `python3 packmerge_web.py` — the same UI in your browser, served from your
  own machine (no hosting, no accounts; stdlib only, zero dependencies).
  Opens automatically at http://127.0.0.1:8471. Add `--host 0.0.0.0` to
  reach it from other devices on your network, e.g. your phone on WiFi.

Both edit everything (sources, overrides, excludes), check for updates, and
build with live log output. They share one state file next to the scripts
and autosave every change, so there's no save/open workflow — what you see
is what builds, always targeting the latest release.

## Self-updates

packmerge can update itself from its GitHub repo — no re-downloading zips:

- Web UI / desktop app: hit the **App updates** button.
- CLI: `python3 packmerge.py --self-update`

Setup (one time): create a repo, push these files plus `version.json`,
and put `"repo": "username/reponame"` in `packmerge-update.json` next to
the scripts. (For a private repo, also put a personal access token in the
`"token"` field — it's only ever sent to raw.githubusercontent.com.)
`python3 packmerge_update.py --make-version 2026.10.13.1` regenerates
`version.json` before each push. Only files listed there are ever replaced;
your state, cache, and lockfiles are untouched.

## How it works

You describe your merge in `manifest.json`:

- **sources** — packs in priority order, low to high (exactly like the game's
  pack stack: the top/last pack wins when two packs contain the same file).
  Each source is one of:
  - `{"name": "dark", "modrinth": "default-dark-mode"}` — a Modrinth slug or
    full Modrinth page URL. packmerge picks the newest release for your
    `mc_version` automatically. No API key needed.
  - `{"name": "x", "url": "https://..."}` — a direct zip download link.
    Uses conditional requests, so unchanged files aren't re-downloaded.
  - `{"name": "y", "path": "./local.zip"}` — a pack already on your disk.
- **overrides** — per-file winners that beat priority order, e.g.
  `"assets/minecraft/textures/gui/book.png": "dark"`.
- **exclude** — exact paths to drop, or directory prefixes ending in `/`.

packmerge resolves everything, downloads only what changed since the last run
(tracked in `manifest.json.lock.json`), merges with game-accurate top-wins
semantics, generates `pack.mcmeta` (with the right `pack_format` for your
`mc_version`), and writes the output zip.

## Usage

```bash
# copy the example and edit it
cp manifest.example.json manifest.json

# see what would update, without downloading/building
python3 packmerge.py --check

# build (or rebuild after upstream updates)
python3 packmerge.py

# redownload everything and rebuild
python3 packmerge.py --force

# other options
python3 packmerge.py -m other-manifest.json --mc-version 26.3 -o Out.zip
```

Then drop the output zip into your `resourcepacks` folder like any other pack.

## Respackopts support

If **one** of your sources uses the [Respackopts](https://mods.jfronny.dev/Respackopts/)
config mod, its `respackopts.json5`, `.rpo` files, and translations pass through
untouched — the merged pack stays configurable.

If **several** sources use Respackopts, packmerge merges their configs into one:

- each source's options are nested under a category named after the source
  (so two packs can both define `darkMode` without colliding),
- every `.rpo` condition is rewritten to the new paths
  (`darkMode` → `packA.darkMode`),
- translation keys are remapped to the merged pack id
  (`rpo.packA.darkMode` → `rpo.mergedPack.packA.darkMode`),
- capabilities are unioned and the highest format version is kept.

Tune it with `"respackopts_id"` (default: derived from `pack_name`) and
`"merge_respackopts": false` to disable merging (top-wins then applies, which
will break the losing packs' toggles — not recommended).

## Smart JSON merging

When two packs contain the *same* JSON file, packmerge doesn't just let the
higher-priority pack win — it merges them by type, and warns when the two
packs genuinely conflict:

- **lang files** — keys are unioned (this matches what the game itself does);
  a note is logged when both packs define the same key.
- **block/item models (legacy `overrides`)** — `overrides` arrays are spliced
  together. Per the wiki, when several cases match the *last* one wins, so
  higher-priority entries go last; exact duplicates are dropped and true
  predicate conflicts warn (higher-priority pack's model kept).
- **item definitions (`assets/*/items/*.json`, 1.21.4+)** — the modern
  format that replaced `overrides`. `range_dispatch` entries union by
  threshold (sorted ascending), `select` cases union by `when` value,
  `condition` branches merge recursively, `composite` model lists
  concatenate; type mismatches warn and the higher-priority pack wins.
- **blockstates** — `variants` are unioned; `multipart` lists concatenated.
- **sounds.json** — events are merged per-event, honoring `replace: true`.
- **atlases / fonts / particles** — `sources` / `providers` / `textures`
  lists are concatenated.
- **everything else JSON** — deep-merged; conflicting values warn and the
  higher-priority pack wins.

Version overlays (`overlays` in each source's `pack.mcmeta`) are preserved:
their declarations are carried into the merged `pack.mcmeta` (legacy
`formats` converted to `min_format`/`max_format` on modern targets), and if
two sources declare the same overlay directory name the later one is renamed
with a warning.

Set `"smart_json_merge": false` in the manifest for strict top-wins
everywhere. Your manifest `overrides` still beat all of this per-file.

## credits.txt

Every build writes a `credits.txt` into the output pack listing each source,
its version, and its author (looked up from Modrinth where possible), plus
your overrides and exclusions.

## Vanilla Tweaks and other custom downloads

Packs from builders like [vanillatweaks.net](https://vanillatweaks.net) have no
versioned releases to track — just download your custom zip and reference it
as a local `path` source. packmerge hashes local files, so `--check` will
tell you when you've replaced the download with a fresh one. These rarely
update, so this is a non-issue in practice.

## Requirements

- Python 3.9+ — no pip packages needed (stdlib only).
- Works on Windows, macOS, and Linux.

## Files

- `packmerge.py` — the tool
- `manifest.example.json` — annotated example manifest
- `.packmerge-cache/` — downloaded packs (created on first run)
- `manifest.json.lock.json` — what was used last run (created on first run)

## Notes

- This merges packs for **personal use**. Redistributing a merged pack bundles
  other authors' work — check each pack's license before sharing it anywhere.
- `pack.png` is taken from the highest-priority source that has one.
- Each source's own `pack.mcmeta` is ignored; the output gets a fresh one.
