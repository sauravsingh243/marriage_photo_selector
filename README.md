# Marriage Photo Selector

A local photo-culling tool for your wedding photos. Everything — the photos, the database, the face recognition — stays on your Mac. Nothing is uploaded anywhere.

## Setup (one time)

Requires Python 3.10+ (`python3 --version` to check; install from python.org if needed).

```bash
cd marriage-photo-selector
chmod +x run.sh
./run.sh
```

The first run creates a virtual environment and installs dependencies (the face-recognition model, ~300 MB, downloads automatically the first time you index). Then it opens http://127.0.0.1:8756 in your browser.

Every later launch is just `./run.sh` again.

## How it works

**First launch** takes you straight to Settings and asks for two paths: the folder containing all your photos (subfolders included), and a folder where selected photos should be copied. If a folder later becomes unreachable (external drive unplugged, folder moved), a warning banner appears at the top.

**Photos tab** shows everything as a grid. Each photo carries one of three states: **Selected** (gold), **Not selected**, or **No action** (untagged). Hover a thumbnail to tag it, or click to open it full-screen and cull fast with the keyboard: `S` select, `X` not selected, `U` clear, arrow keys to move. Filter chips at the top show only Selected / Not selected / Untagged. **Copy selected → folder** copies every selected photo into your selected-photos folder (safe to run repeatedly — it skips ones already copied).

**Find a person tab**: first click **Start indexing** — it scans every photo for faces once, in the background (roughly 1–3 photos/second on an M1; you can pause and resume, and re-runs only touch new photos). Then drop in one clear photo of a person and you get every photo they appear in, best match first, with the same Select / Reject / Clear tags. The strictness slider trades precision for recall.

## Efficiency notes (M1, 16 GB)

- The browser only ever loads small cached thumbnails; full images are served downscaled to 2048 px. The grid loads in pages as you scroll, so 5,000+ photos are fine.
- Face indexing processes one photo at a time, capped at 1600 px, so memory stays flat (~1–1.5 GB while indexing, near zero otherwise).
- All state lives in SQLite at `~/.marriage-photo-selector/` — your original photos are never modified or moved. Delete that folder to reset the tool completely.

## Supported formats

JPG, PNG, WebP, and HEIC/HEIF (via pillow-heif). RAW files (.CR3/.NEF/.ARW) are not supported — point the tool at the JPEG exports from your photographer.
