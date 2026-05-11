"""
FastAPI server for Video Mixer.
Run via amyleor.py or directly: uvicorn server:app --reload
"""
import asyncio
import json
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

import pipeline
from constants import SETTINGS_FILE

app = FastAPI()

# ── Shared state ──────────────────────────────────────────────────────────────

_cancel = threading.Event()
_running = False
_job_queues: dict[str, queue.Queue] = {}

_DEFAULT_SETTINGS = {
    "input": "",
    "output": str(Path.home() / "Desktop" / "mixed_video.mp4"),
    "music": "",
    "duration": "60",
    "use_max": True,
    "max_clip": "10",
    "use_fade": True,
    "fade_dur": "0.5",
    "use_seed": False,
    "seed": "42",
    "music_vol": "30",
    "beat_sync": True,
    "beats_per_clip": "8",
    "clip_order": "random",
    "subfolder_split": "equal",
    "use_all": False,
    "tile_portrait": True,
    "use_intro": False,
    "intro_mode": "over_clips",
    "intro_fade_dur": "0.5",
    "intro_lines": "[]",
    "use_outro": False,
    "outro_dur": "3",
    "use_countdown": False,
    "cd_corner": "top-right",
    "cd_dur": "5",
    "cd_ivmin": "50",
    "cd_ivmax": "60",
    "cd_text1": "HOLD",
    "cd_text1_dur": "7",
    "cd_text2": "RELEASE",
    "cd_text2_dur": "4",
    "cd_sync": True,
}

# ── Static / index ─────────────────────────────────────────────────────────────

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/")
async def index():
    return FileResponse(_STATIC_DIR / "index.html")


# ── Settings ──────────────────────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings():
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
        return {**_DEFAULT_SETTINGS, **saved}
    except Exception:
        return _DEFAULT_SETTINGS


@app.post("/api/settings")
async def save_settings(request: Request):
    data = await request.json()
    try:
        SETTINGS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        pass
    return {"ok": True}


# ── Browse dialogs ────────────────────────────────────────────────────────────

def _pick_folder() -> Optional[str]:
    if sys.platform == "darwin":
        r = subprocess.run(
            ["osascript", "-e", "POSIX path of (choose folder)"],
            capture_output=True, text=True, timeout=60,
        )
        path = r.stdout.strip().rstrip("/")
        return path or None
    else:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory()
        root.destroy()
        return path or None


def _pick_savefile() -> Optional[str]:
    if sys.platform == "darwin":
        r = subprocess.run(
            ["osascript", "-e",
             'POSIX path of (choose file name with prompt "Save output video:" '
             'default name "mixed_video.mp4")'],
            capture_output=True, text=True, timeout=60,
        )
        path = r.stdout.strip()
        return path or None
    else:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4"), ("All Files", "*.*")],
        )
        root.destroy()
        return path or None


@app.post("/api/browse/folder")
async def browse_folder():
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, _pick_folder)
    return {"path": path}


@app.post("/api/browse/savefile")
async def browse_savefile():
    loop = asyncio.get_event_loop()
    path = await loop.run_in_executor(None, _pick_savefile)
    return {"path": path}


# ── Thumbnail ────────────────────────────────────────────────────────────────

_thumb_cache: dict[tuple[str, float], bytes] = {}


@app.get("/api/thumbnail")
async def thumbnail(path: str, time: float = 0.0):
    key = (path, round(time, 2))
    if key in _thumb_cache:
        return Response(content=_thumb_cache[key], media_type="image/jpeg",
                        headers={"Cache-Control": "max-age=3600"})

    def _grab() -> Optional[bytes]:
        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", str(time), "-i", path,
             "-vframes", "1", "-vf", "scale=200:-1",
             "-f", "image2", "-vcodec", "mjpeg", "pipe:1"],
            capture_output=True, timeout=15,
        )
        return result.stdout if result.returncode == 0 and result.stdout else None

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _grab)
    if not data:
        return Response(status_code=404)
    _thumb_cache[key] = data
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "max-age=3600"})


@app.get("/api/clip_preview")
async def clip_preview(path: str, start: float = 0.0, duration: float = 5.0):
    src = Path(path).expanduser()
    if not src.is_file():
        return JSONResponse({"error": "file not found"}, status_code=404)

    safe_start = max(0.0, start)
    safe_duration = max(0.1, min(duration, 300.0))
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{safe_start:.3f}",
        "-i", str(src),
        "-t", f"{safe_duration:.3f}",
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "28",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )

    async def body():
        loop = asyncio.get_event_loop()
        try:
            assert proc.stdout is not None
            while True:
                chunk = await loop.run_in_executor(None, proc.stdout.read, 65536)
                if not chunk:
                    break
                yield chunk
            await loop.run_in_executor(None, proc.wait)
        finally:
            if proc.poll() is None:
                proc.kill()
            if proc.stdout is not None:
                proc.stdout.close()

    return StreamingResponse(
        body(),
        media_type="video/mp4",
        headers={"Cache-Control": "no-store"},
    )


# ── Open file ─────────────────────────────────────────────────────────────────

@app.post("/api/open")
async def open_path(request: Request):
    data = await request.json()
    path = data.get("path", "")
    if path:
        if sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        elif sys.platform == "win32":
            subprocess.run(["start", "", path], shell=True, check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    return {"ok": True}


@app.post("/api/reveal")
async def reveal_path(request: Request):
    data = await request.json()
    path = data.get("path", "")
    if not path:
        return {"ok": False, "error": "missing path"}

    p = Path(path).expanduser()
    if not p.exists():
        return {"ok": False, "error": "path not found"}

    if sys.platform == "darwin":
        subprocess.run(["open", "-R", str(p)], check=False)
    elif sys.platform == "win32":
        subprocess.run(["explorer", "/select,", str(p)], check=False)
    else:
        target = p.parent if p.is_file() else p
        subprocess.run(["xdg-open", str(target)], check=False)

    return {"ok": True}


# ── SSE streaming ─────────────────────────────────────────────────────────────

@app.get("/api/stream/{job_id}")
async def stream_job(job_id: str):
    q = _job_queues.get(job_id)
    if q is None:
        return JSONResponse({"error": "unknown job"}, status_code=404)

    async def event_gen():
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            yield f"data: {json.dumps(item)}\n\n"
            if item.get("type") in ("done", "error"):
                break

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Settings → pipeline args ──────────────────────────────────────────────────

def _parse_settings(s: dict) -> dict:
    target_dur = float(s.get("duration", 60))
    max_clip = float(s["max_clip"]) if s.get("use_max") else None
    fade_dur = float(s["fade_dur"]) if s.get("use_fade") else 0.0
    seed = int(s["seed"]) if s.get("use_seed") else None
    music_vol = float(s.get("music_vol", 30)) / 100.0
    beats_per_clip = int(s.get("beats_per_clip", 8))
    beat_sync = bool(s.get("beat_sync", True))

    clip_order = s.get("clip_order", "random")
    subfolder_split = s.get("subfolder_split", "equal")
    use_all = bool(s.get("use_all", False))
    tile_portrait = bool(s.get("tile_portrait", True))

    countdown_cfg = None
    if s.get("use_countdown"):
        text1 = s.get("cd_text1", "").strip()
        text2 = s.get("cd_text2", "").strip()
        countdown_cfg = {
            "dur": float(s.get("cd_dur", 5)),
            "iv_min": float(s.get("cd_ivmin", 50)),
            "iv_max": float(s.get("cd_ivmax", 60)),
            "corner": s.get("cd_corner", "top-right"),
            "text1": text1,
            "text1_dur": float(s.get("cd_text1_dur", 7)) if text1 else 0.0,
            "text2": text2,
            "text2_dur": float(s.get("cd_text2_dur", 4)) if text2 else 0.0,
            "sync": bool(s.get("cd_sync", True)),
        }

    auto_mute = bool(s.get("auto_mute", False))
    outro_dur = float(s["outro_dur"]) if s.get("use_outro") else 0.0

    intro_cfg = None
    if s.get("use_intro"):
        import json as _json
        try:
            lines = _json.loads(s.get("intro_lines", "[]"))
        except Exception:
            lines = []
        intro_cfg = {
            "lines": lines,
            "mode": s.get("intro_mode", "over_clips"),
            "fade_dur": float(s.get("intro_fade_dur", 0.5)),
        }

    return dict(
        folder=s.get("input", ""),
        output=s.get("output", ""),
        target_dur=target_dur,
        max_clip=max_clip,
        seed=seed,
        music_folder=s.get("music", ""),
        music_vol=music_vol,
        fade_dur=fade_dur,
        beat_sync=beat_sync,
        beats_per_clip=beats_per_clip,
        countdown_cfg=countdown_cfg,
        clip_order=clip_order,
        subfolder_split=subfolder_split,
        use_all=use_all,
        tile_portrait=tile_portrait,
        auto_mute=auto_mute,
        outro_dur=outro_dur,
        intro_cfg=intro_cfg,
    )


# ── Job runner ────────────────────────────────────────────────────────────────

def _new_job() -> tuple[str, queue.Queue]:
    global _running
    job_id = str(uuid.uuid4())
    q: queue.Queue = queue.Queue()
    _job_queues[job_id] = q
    _cancel.clear()
    _running = True
    return job_id, q


def _make_callbacks(q: queue.Queue):
    def log_fn(msg, color=None):
        q.put({"type": "log", "msg": msg, "color": color})

    def status_fn(msg):
        q.put({"type": "status", "msg": msg})

    def prog_fn(val):
        q.put({"type": "progress", "value": val})

    return log_fn, status_fn, prog_fn


# ── Manual mute override ──────────────────────────────────────────────────────

@app.post("/api/mute_override")
async def save_mute_override(request: Request):
    from cache import _load_cache, _save_cache
    data = await request.json()
    path = data.get("path", "")
    start = data.get("start")
    dur = data.get("dur")
    muted = data.get("muted")
    if not path or start is None or dur is None or muted is None:
        return {"ok": False}
    folder = str(Path(path).parent)
    filename = Path(path).name
    cache_key = f"{filename}:{float(start):.3f}:{float(dur):.3f}"
    cache_data = _load_cache(folder)
    cache_data.setdefault("manual_mute", {})[cache_key] = {"muted": bool(muted)}
    _save_cache(folder, cache_data)
    return {"ok": True}


# ── Plan endpoint ─────────────────────────────────────────────────────────────

@app.post("/api/plan")
async def start_plan(request: Request):
    global _running
    if _running:
        return JSONResponse({"error": "already running"}, status_code=409)

    settings = await request.json()
    job_id, q = _new_job()

    def run():
        global _running
        try:
            log_fn, status_fn, prog_fn = _make_callbacks(q)
            kwargs = _parse_settings(settings)
            plan = pipeline.do_analyse(
                **kwargs,
                log_fn=log_fn, status_fn=status_fn, prog_fn=prog_fn,
                cancel_event=_cancel,
            )
            if plan is not None:
                q.put({"type": "plan", "plan": plan})
            q.put({"type": "done"})
        except Exception as exc:
            q.put({"type": "error", "msg": str(exc)})
        finally:
            _running = False

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


# ── Generate endpoint ─────────────────────────────────────────────────────────

@app.post("/api/generate")
async def start_generate(request: Request):
    global _running
    if _running:
        return JSONResponse({"error": "already running"}, status_code=409)

    plan = await request.json()
    job_id, q = _new_job()

    def run():
        global _running
        try:
            log_fn, status_fn, prog_fn = _make_callbacks(q)
            pipeline.do_generate(
                plan,
                log_fn=log_fn, status_fn=status_fn, prog_fn=prog_fn,
                cancel_event=_cancel,
            )
            q.put({"type": "done"})
        except Exception as exc:
            q.put({"type": "error", "msg": str(exc)})
        finally:
            _running = False

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id}


# ── Cancel ─────────────────────────────────────────────────────────────────────

@app.post("/api/cancel")
async def cancel_job():
    if _running:
        _cancel.set()
    return {"ok": True}
