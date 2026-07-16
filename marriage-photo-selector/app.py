"""
Marriage Photo Selector — local photo culling tool with face search.
Runs entirely on your machine. Optimized for Apple Silicon / 16 GB RAM:
  - photos are never all loaded in memory; grids use small cached thumbnails
  - face indexing streams one photo at a time in a background thread
  - embeddings live in SQLite; search is a single numpy dot product
"""

import io
import os
import json
import shutil
import sqlite3
import hashlib
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, Response, HTMLResponse
from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIC_OK = True
except Exception:
    HEIC_OK = False

APP_DIR = Path.home() / ".marriage-photo-selector"
THUMB_DIR = APP_DIR / "thumbs"
DB_PATH = APP_DIR / "app.db"
APP_DIR.mkdir(exist_ok=True)
THUMB_DIR.mkdir(exist_ok=True)

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp"} | ({".heic", ".heif"} if HEIC_OK else set())
THUMB_SIZE = 480
VALID_STATUS = {"selected", "rejected", "none"}

app = FastAPI(title="Marriage Photo Selector")

from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

# ---------------------------------------------------------------- database

from contextlib import contextmanager

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'none',
            face_indexed INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_photos_status ON photos(status);
        CREATE TABLE IF NOT EXISTS faces (
            id INTEGER PRIMARY KEY,
            photo_id INTEGER NOT NULL REFERENCES photos(id) ON DELETE CASCADE,
            embedding BLOB NOT NULL,
            bbox TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_faces_photo ON faces(photo_id);
        """)

init_db()

def get_setting(key):
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

def set_setting(key, value):
    with db() as c:
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

# ---------------------------------------------------------------- settings

def paths_state():
    photos_path = get_setting("photos_path")
    selected_path = get_setting("selected_path")
    return {
        "configured": bool(photos_path and selected_path),
        "photos_path": photos_path,
        "selected_path": selected_path,
        "photos_path_ok": bool(photos_path) and Path(photos_path).is_dir(),
        "selected_path_ok": bool(selected_path) and Path(selected_path).is_dir(),
        "heic_supported": HEIC_OK,
    }

@app.get("/api/settings")
def api_get_settings():
    return paths_state()

@app.post("/api/settings")
def api_save_settings(payload: dict):
    photos_path = os.path.expanduser((payload.get("photos_path") or "").strip())
    selected_path = os.path.expanduser((payload.get("selected_path") or "").strip())
    errors = {}
    if not photos_path or not Path(photos_path).is_dir():
        errors["photos_path"] = "This folder doesn't exist. Check the path and try again."
    if not selected_path:
        errors["selected_path"] = "Enter a folder for your selected photos."
    else:
        try:
            Path(selected_path).mkdir(parents=True, exist_ok=True)
        except Exception:
            errors["selected_path"] = "Couldn't create this folder. Check the path and permissions."
    if photos_path and selected_path and Path(photos_path) == Path(selected_path):
        errors["selected_path"] = "Use a different folder than the photos folder."
    if errors:
        return {"ok": False, "errors": errors}
    set_setting("photos_path", photos_path)
    set_setting("selected_path", selected_path)
    return {"ok": True, **paths_state()}

# ---------------------------------------------------------------- scanning

@app.post("/api/scan")
def api_scan():
    state = paths_state()
    if not state["photos_path_ok"]:
        raise HTTPException(409, "Photos folder is not available.")
    root = Path(state["photos_path"])
    found = set()
    with db() as c:
        for p in root.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in PHOTO_EXTS or p.name.startswith("."):
                continue
            st = p.stat()
            found.add(str(p))
            c.execute("""INSERT INTO photos(path, filename, mtime, size)
                         VALUES(?,?,?,?)
                         ON CONFLICT(path) DO UPDATE SET
                           mtime=excluded.mtime, size=excluded.size,
                           face_indexed=CASE WHEN photos.mtime!=excluded.mtime
                                             THEN 0 ELSE photos.face_indexed END
                      """, (str(p), p.name, st.st_mtime, st.st_size))
        # drop DB rows for files that no longer exist on disk
        for row in c.execute("SELECT id, path FROM photos").fetchall():
            if row["path"] not in found:
                c.execute("DELETE FROM faces WHERE photo_id=?", (row["id"],))
                c.execute("DELETE FROM photos WHERE id=?", (row["id"],))
    return api_stats()

@app.get("/api/stats")
def api_stats():
    with db() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM photos GROUP BY status").fetchall()
    counts = {"selected": 0, "rejected": 0, "none": 0}
    for r in rows:
        counts[r["status"]] = r["n"]
    counts["total"] = sum(counts.values())
    return counts

@app.get("/api/photos")
def api_photos(status: str = "all", page: int = 1, per_page: int = 120):
    per_page = max(1, min(per_page, 240))
    q, args = "SELECT id, filename, status FROM photos", []
    if status in VALID_STATUS:
        q += " WHERE status=?"
        args.append(status)
    q += " ORDER BY mtime ASC, id ASC LIMIT ? OFFSET ?"
    args += [per_page, (page - 1) * per_page]
    with db() as c:
        rows = [dict(r) for r in c.execute(q, args).fetchall()]
    return {"photos": rows, "page": page, "has_more": len(rows) == per_page}

@app.post("/api/photos/{photo_id}/status")
def api_set_status(photo_id: int, payload: dict):
    status = payload.get("status")
    if status not in VALID_STATUS:
        raise HTTPException(400, "Invalid status.")
    with db() as c:
        cur = c.execute("UPDATE photos SET status=? WHERE id=?", (status, photo_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "Photo not found.")
    return {"ok": True, "id": photo_id, "status": status}

# ---------------------------------------------------------------- images

def photo_row(photo_id: int):
    with db() as c:
        row = c.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Photo not found.")
    if not Path(row["path"]).is_file():
        raise HTTPException(410, "File is missing on disk. Re-scan your photos folder.")
    return row

@app.get("/api/thumb/{photo_id}")
def api_thumb(photo_id: int):
    row = photo_row(photo_id)
    key = hashlib.sha1(f"{row['path']}|{row['mtime']}|{THUMB_SIZE}".encode()).hexdigest()
    cached = THUMB_DIR / f"{key}.jpg"
    if not cached.exists():
        with Image.open(row["path"]) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((THUMB_SIZE, THUMB_SIZE))
            im.convert("RGB").save(cached, "JPEG", quality=82)
    return FileResponse(cached, media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=86400"})

@app.get("/api/image/{photo_id}")
def api_image(photo_id: int):
    """Full-view image, downscaled to 2048px so the browser stays light."""
    row = photo_row(photo_id)
    with Image.open(row["path"]) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((2048, 2048))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=88)
    return Response(buf.getvalue(), media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=3600"})

# ---------------------------------------------------------------- export

@app.post("/api/export")
def api_export():
    state = paths_state()
    if not state["selected_path_ok"]:
        raise HTTPException(409, "Selected-photos folder is not available.")
    dest = Path(state["selected_path"])
    with db() as c:
        rows = c.execute("SELECT path, filename FROM photos WHERE status='selected'").fetchall()
    copied, skipped, missing = 0, 0, 0
    for r in rows:
        src = Path(r["path"])
        if not src.is_file():
            missing += 1
            continue
        target = dest / r["filename"]
        n = 1
        while target.exists():
            if target.stat().st_size == src.stat().st_size:
                break  # same file already exported
            target = dest / f"{src.stem}_{n}{src.suffix}"
            n += 1
        if target.exists():
            skipped += 1
        else:
            shutil.copy2(src, target)
            copied += 1
    return {"copied": copied, "already_there": skipped, "missing": missing, "total": len(rows)}

# ---------------------------------------------------------------- faces

_face_lock = threading.Lock()
_face_model = None
_index_progress = {"running": False, "done": 0, "total": 0, "error": None}

def face_model():
    """Lazy-load InsightFace so the app starts instantly and still works
    (minus face search) if the optional dependencies aren't installed."""
    global _face_model
    if _face_model is None:
        try:
            from insightface.app import FaceAnalysis
        except ImportError:
            raise HTTPException(
                501, "Face search needs extra packages. Run: pip install insightface onnxruntime")
        m = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        m.prepare(ctx_id=-1, det_size=(640, 640))
        _face_model = m
    return _face_model

def load_rgb_capped(path, cap=1600):
    """Open an image capped to `cap` px on the long edge -> BGR numpy array.
    Keeps peak memory low even for 50 MP originals."""
    import numpy as np
    with Image.open(path) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((cap, cap))
        return np.asarray(im.convert("RGB"))[:, :, ::-1].copy()

def _index_worker():
    import numpy as np
    try:
        model = face_model()
        with db() as c:
            todo = c.execute(
                "SELECT id, path FROM photos WHERE face_indexed=0 ORDER BY id").fetchall()
        _index_progress.update(done=0, total=len(todo), error=None)
        for row in todo:
            if not _index_progress["running"]:
                break
            try:
                if Path(row["path"]).is_file():
                    faces = model.get(load_rgb_capped(row["path"]))
                else:
                    faces = []
                with db() as c:
                    c.execute("DELETE FROM faces WHERE photo_id=?", (row["id"],))
                    for f in faces:
                        emb = f.normed_embedding.astype(np.float32)
                        c.execute("INSERT INTO faces(photo_id, embedding, bbox) VALUES(?,?,?)",
                                  (row["id"], emb.tobytes(), json.dumps(f.bbox.tolist())))
                    c.execute("UPDATE photos SET face_indexed=1 WHERE id=?", (row["id"],))
            except Exception:
                with db() as c:  # unreadable file: mark done so we don't loop on it
                    c.execute("UPDATE photos SET face_indexed=1 WHERE id=?", (row["id"],))
            _index_progress["done"] += 1
    except HTTPException as e:
        _index_progress["error"] = e.detail
    except Exception as e:
        _index_progress["error"] = str(e)
    finally:
        _index_progress["running"] = False

@app.post("/api/faces/index/start")
def api_index_start():
    with _face_lock:
        if _index_progress["running"]:
            return _index_progress
        _index_progress.update(running=True, done=0, total=0, error=None)
        threading.Thread(target=_index_worker, daemon=True).start()
    return _index_progress

@app.post("/api/faces/index/stop")
def api_index_stop():
    _index_progress["running"] = False
    return _index_progress

@app.get("/api/faces/index/status")
def api_index_status():
    with db() as c:
        indexed = c.execute("SELECT COUNT(*) n FROM photos WHERE face_indexed=1").fetchone()["n"]
        total = c.execute("SELECT COUNT(*) n FROM photos").fetchone()["n"]
        n_faces = c.execute("SELECT COUNT(*) n FROM faces").fetchone()["n"]
    return {**_index_progress, "indexed_photos": indexed, "total_photos": total, "faces": n_faces}

@app.post("/api/faces/search")
async def api_faces_search(file: UploadFile = File(...), threshold: float = 0.35):
    import numpy as np
    model = face_model()
    data = await file.read()
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((1600, 1600))
            img = np.asarray(im.convert("RGB"))[:, :, ::-1].copy()
    except Exception:
        raise HTTPException(400, "Couldn't read that image. Use a JPG, PNG or HEIC file.")
    ref_faces = model.get(img)
    if not ref_faces:
        raise HTTPException(422, "No face found in the reference photo. Try a clearer, front-facing shot.")
    # largest face in the reference photo is the person we search for
    ref = max(ref_faces, key=lambda f: (f.bbox[2]-f.bbox[0]) * (f.bbox[3]-f.bbox[1]))
    query = ref.normed_embedding.astype(np.float32)

    with db() as c:
        rows = c.execute("""SELECT f.photo_id, f.embedding, p.filename, p.status
                            FROM faces f JOIN photos p ON p.id = f.photo_id""").fetchall()
    if not rows:
        return {"matches": [], "note": "No faces indexed yet. Run indexing first."}
    embs = np.frombuffer(b"".join(r["embedding"] for r in rows),
                         dtype=np.float32).reshape(len(rows), -1)
    sims = embs @ query  # normed embeddings -> cosine similarity
    best = {}  # one entry per photo, keep its best face score
    for r, s in zip(rows, sims):
        if s >= threshold and s > best.get(r["photo_id"], (None, -1))[1]:
            best[r["photo_id"]] = ((r["filename"], r["status"]), float(s))
    matches = [{"id": pid, "filename": v[0][0], "status": v[0][1], "score": round(v[1], 3)}
               for pid, v in best.items()]
    matches.sort(key=lambda m: -m["score"])
    return {"matches": matches[:600]}

# ---------------------------------------------------------------- app shell

@app.get("/")
def index():
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text())

if __name__ == "__main__":
    print("\n  Marriage Photo Selector  ->  http://127.0.0.1:8756\n")
    uvicorn.run(app, host="127.0.0.1", port=8756, log_level="warning")
