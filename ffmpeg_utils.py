import json
import subprocess
import sys
import time


def _run(cmd, check=True):
    return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", check=check)


def _run_ffmpeg(cmd, log_fn=None, cancel_event=None):
    """Run an ffmpeg command, streaming stderr progress lines to log_fn."""
    import re
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stderr_buf = b""
    time_re = re.compile(rb"time=(\d+:\d+:\d+\.\d+)")
    while proc.poll() is None:
        if cancel_event and cancel_event.is_set():
            proc.kill()
            proc.wait()
            raise RuntimeError("Cancelled")
        try:
            chunk = proc.stderr.read1(4096)  # type: ignore[attr-defined]
        except Exception:
            chunk = b""
        if chunk:
            stderr_buf += chunk
            m = time_re.search(chunk)
            if m and log_fn:
                log_fn(f"  mixing… {m.group(1).decode()}")
        else:
            time.sleep(0.1)
    remaining = proc.stderr.read()
    stderr_buf += remaining
    if proc.returncode != 0:
        raise RuntimeError(stderr_buf.decode(errors="replace")[-800:])


def check_ffmpeg():
    try:
        _run(["ffmpeg", "-version"])
        _run(["ffprobe", "-version"])
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def get_video_info(path):
    """Return dict with duration, width, height, has_audio or None on failure."""
    try:
        r = _run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ])
        data = json.loads(r.stdout)
        duration = float(data["format"].get("duration", 0))
        if duration <= 0:
            return None

        vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
        if not vstream:
            return None

        w, h = int(vstream.get("width", 0)), int(vstream.get("height", 0))

        rotation = 0
        for sd in vstream.get("side_data_list", []):
            if sd.get("side_data_type") == "Display Matrix":
                rotation = abs(int(sd.get("rotation", 0)))
        try:
            rotation = rotation or abs(int(vstream.get("tags", {}).get("rotate", 0)))
        except (ValueError, TypeError):
            pass
        if rotation in (90, 270):
            w, h = h, w

        has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
        return {"duration": duration, "width": w, "height": h, "has_audio": has_audio}
    except Exception as e:
        print(f"ffprobe error for {path}: {e}", file=sys.stderr)
        return None
