import hashlib
import json
import time
from pathlib import Path

from constants import CACHE_FILE, CLIP_CACHE_MAX_AGE_S, OUT_FPS, OUT_H, OUT_W


def _load_cache(folder):
    try:
        return json.loads((Path(folder) / CACHE_FILE).read_text())
    except Exception:
        return {}


def _save_cache(folder, cache):
    try:
        (Path(folder) / CACHE_FILE).write_text(json.dumps(cache, indent=2))
    except Exception:
        pass


def _fingerprint(path):
    st = Path(path).stat()
    return st.st_mtime, st.st_size


def _fp_match(path, entry):
    try:
        mtime, size = _fingerprint(path)
        return mtime == entry.get("mtime") and size == entry.get("size")
    except Exception:
        return False


def _clip_cache_key(vf, start, dur, motion):
    mtime, size = _fingerprint(vf)
    parts = "|".join([
        str(mtime), str(size),
        f"{start:.3f}", f"{dur:.3f}",
        str(motion),
        f"{OUT_W}x{OUT_H}@{OUT_FPS}",
    ])
    return hashlib.sha1(parts.encode()).hexdigest()


def _evict_clip_cache(cache_dir):
    """Delete encoded clips in cache_dir that haven't been accessed in 30 days."""
    try:
        cutoff = time.time() - CLIP_CACHE_MAX_AGE_S
        for p in Path(cache_dir).glob("*.mp4"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except Exception:
        pass
