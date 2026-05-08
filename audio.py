import json
from pathlib import Path

from ffmpeg_utils import _run, _run_ffmpeg


def get_audio_info(path):
    """Return dict with duration or None."""
    try:
        r = _run([
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", str(path),
        ])
        data = json.loads(r.stdout)
        duration = float(data["format"].get("duration", 0))
        return {"duration": duration} if duration > 0 else None
    except Exception:
        return None


def get_music_energy(path):
    """Return RMS energy level (dB) as a float, or None on failure. Higher = more intense."""
    try:
        r = _run([
            "ffmpeg", "-i", str(path), "-af", "astats=metadata=1:reset=1",
            "-f", "null", "-",
        ], check=False)
        for line in r.stderr.splitlines():
            if "RMS level dB" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    val = parts[-1].strip()
                    if val not in ("-inf", "inf"):
                        return float(val)
        return None
    except Exception:
        return None


def detect_beats(path, log_fn=None):
    """
    Return list of beat timestamps (seconds) via librosa.
    Returns None if librosa is unavailable or detection fails.
    """
    try:
        import librosa  # optional dependency
        if log_fn:
            log_fn("  Loading audio for beat detection (may take a moment)…")
        y, sr = librosa.load(str(path), mono=True)
        _, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
        beat_times = librosa.frames_to_time(beat_frames, sr=sr).tolist()
        if log_fn:
            log_fn(f"  Detected {len(beat_times)} beats")
        return beat_times
    except ImportError:
        if log_fn:
            log_fn("  librosa not installed — pip install librosa  (beat sync disabled)")
        return None
    except Exception as e:
        if log_fn:
            log_fn(f"  Beat detection failed: {e}")
        return None


def prepare_music_audio(tracks, total_dur, tmp_dir):
    """
    Concatenate and loop music tracks to cover at least total_dur seconds.
    tracks: list of (path, duration) — caller is responsible for probing.
    Returns a Path to the prepared audio file, or None on failure.
    """
    if not tracks:
        return None

    playlist, acc, idx = [], 0.0, 0
    while acc < total_dur:
        p, d = tracks[idx % len(tracks)]
        playlist.append(p)
        acc += d
        idx += 1
        if idx > 500:
            break

    if len(playlist) == 1:
        return tracks[0][0]  # single file already long enough — use directly

    list_file = Path(tmp_dir) / "_music_list.txt"
    out_file = Path(tmp_dir) / "_music.aac"
    with open(list_file, "w") as f:
        for p in playlist:
            f.write(f"file '{str(p)}'\n")

    _run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(list_file),
        "-vn",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        str(out_file),
    ])
    return out_file


def mix_music(video_path, output_path, music_path, music_vol, log_fn=None, cancel_event=None):
    """Overlay music onto an already-concatenated video (stream-copy video, re-encode audio)."""
    _run_ffmpeg([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-i", str(music_path),
        "-filter_complex",
        f"[0:a]volume=1.0[aclip];"
        f"[1:a]volume={music_vol:.3f}[amus];"
        f"[aclip][amus]amix=inputs=2:duration=first:normalize=0[aout]",
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        str(output_path),
    ], log_fn=log_fn, cancel_event=cancel_event)
