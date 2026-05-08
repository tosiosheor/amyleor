import subprocess


def find_highlight_start(path, clip_dur, info, log_fn=None):
    """
    Returns the start time (seconds) of the most scene-change-dense window of
    clip_dur length within the video.  Falls back to (duration - clip_dur) —
    i.e. the end of the clip — if analysis finds nothing or fails.
    """
    duration = info["duration"]
    fallback = max(0.0, duration - clip_dur)

    if clip_dur >= duration:
        return 0.0

    try:
        r = subprocess.run(
            [
                "ffmpeg", "-i", str(path),
                "-vf", "scale=160:-2,select=gt(scene\\,0.1),metadata=print:file=-",
                "-an", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=120,
        )
        timestamps = []
        for line in r.stdout.splitlines():
            if line.startswith("frame:") and "pts_time:" in line:
                for token in line.split():
                    if token.startswith("pts_time:"):
                        try:
                            t = float(token[len("pts_time:"):])
                            if 0 <= t < duration:
                                timestamps.append(t)
                        except ValueError:
                            pass

        if not timestamps:
            return fallback

        best_start, best_count = fallback, 0
        for t in timestamps:
            start = max(0.0, min(t - clip_dur * 0.25, duration - clip_dur))
            count = sum(1 for s in timestamps if start <= s <= start + clip_dur)
            if count > best_count:
                best_count, best_start = count, start

        return round(best_start, 3)
    except Exception as e:
        if log_fn:
            log_fn(f"  Highlight detection failed ({e}) — using end of video")
        return fallback


def analyze_portrait_motion(src, start, duration, width, height):
    """
    Sample ~15 grayscale frames from a portrait clip and compute a motion heatmap
    via frame-to-frame absolute differences (pure Python, no extra dependencies).

    Returns (y_frac, x_frac, confidence):
      y_frac     — vertical center of motion, 0.0 = top, 1.0 = bottom
      x_frac     — horizontal center of motion, 0.0 = left, 1.0 = right
      confidence — 0.0–1.0; how concentrated the motion is in one region
    Falls back to (0.5, 0.5, 0.0) on any error.
    """
    SAMPLE_W = 160
    scale_h = int(SAMPLE_W * height / width)
    if scale_h % 2:
        scale_h += 1
    frame_size = SAMPLE_W * scale_h

    target_frames = 15
    fps_rate = max(0.25, min(2.0, target_frames / max(duration, 1.0)))

    cmd = [
        "ffmpeg", "-ss", f"{start:.3f}", "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vf", f"fps={fps_rate:.4f},scale={SAMPLE_W}:{scale_h},format=gray",
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        raw = r.stdout
        n_frames = len(raw) // frame_size
        if n_frames < 2:
            return (0.5, 0.5, 0.0)

        frames = [raw[i * frame_size:(i + 1) * frame_size] for i in range(n_frames)]

        motion = bytearray(frame_size)
        for i in range(1, n_frames):
            a, b = frames[i], frames[i - 1]
            for j in range(frame_size):
                v = motion[j] + abs(a[j] - b[j])
                motion[j] = v if v < 256 else 255

        row_sums = [sum(motion[r * SAMPLE_W:(r + 1) * SAMPLE_W]) for r in range(scale_h)]
        col_sums = [sum(motion[r * SAMPLE_W + c] for r in range(scale_h)) for c in range(SAMPLE_W)]

        total = sum(row_sums)
        if total == 0:
            return (0.5, 0.5, 0.0)

        y_frac = sum(r * row_sums[r] for r in range(scale_h)) / (total * scale_h)
        x_frac = sum(c * col_sums[c] for c in range(SAMPLE_W)) / (total * SAMPLE_W)

        mean_row = total / scale_h
        peak_row = max(row_sums)
        confidence = min(1.0, max(0.0, (peak_row / mean_row - 1.0) / 3.0))

        return (round(y_frac, 4), round(x_frac, 4), round(confidence, 4))
    except Exception:
        return (0.5, 0.5, 0.0)


def detect_change_points(path, log_fn=None):
    """
    Detect musical structure change points (builds, drops, section transitions).
    Returns a sorted list of timestamps in seconds, or None on failure.
    """
    try:
        import librosa
        import numpy as np

        if log_fn:
            log_fn("  Analysing musical structure for sync points…")

        y, sr = librosa.load(str(path), mono=True)
        hop_length = 512
        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)

        bin_sec = 0.5
        bin_frames = max(1, int(bin_sec * sr / hop_length))
        n_bins = len(onset_env) // bin_frames
        bins = np.array([
            onset_env[i * bin_frames:(i + 1) * bin_frames].mean()
            for i in range(n_bins)
        ])
        bin_times = np.arange(n_bins) * bin_sec

        lookback = int(8.0 / bin_sec)   # 8-second context window
        min_gap = 10.0                   # minimum gap between change points (seconds)
        change_times = []
        last_t = -min_gap

        for i in range(lookback, n_bins):
            past = bins[i - lookback:i]
            z = (bins[i] - past.mean()) / (past.std() + 1e-6)
            if abs(z) >= 2.0 and bin_times[i] - last_t >= min_gap:
                change_times.append(float(bin_times[i]))
                last_t = bin_times[i]

        if log_fn:
            log_fn(f"  Found {len(change_times)} musical change point(s)")
        return change_times or None
    except ImportError:
        if log_fn:
            log_fn("  librosa unavailable — countdown music sync skipped")
        return None
    except Exception as e:
        if log_fn:
            log_fn(f"  Change point detection failed: {e}")
        return None


def _compute_section_boundaries(n_sections, total_dur, change_points, buffer=60.0, weights=None):
    """
    Return (section_durs, snapped_boundaries).
    Boundaries are nudged to the nearest music change point within ±buffer seconds when available.
    weights: optional list of fractions (summing to 1.0) for proportional splits; None = equal.
    """
    if weights is None:
        weights = [1.0 / n_sections] * n_sections
    raw_boundaries = []
    acc = 0.0
    for w in weights[:-1]:
        acc += w * total_dur
        raw_boundaries.append(acc)
    snapped = []
    for raw in raw_boundaries:
        best = raw
        if change_points:
            lo, hi = raw - buffer, raw + buffer
            cands = [cp for cp in change_points if lo <= cp <= hi]
            if cands:
                best = min(cands, key=lambda cp: abs(cp - raw))
        snapped.append((raw, best))

    section_durs = []
    acc = 0.0
    for _, t in snapped:
        section_durs.append(t - acc)
        acc = t
    section_durs.append(total_dur - acc)
    return section_durs, snapped
