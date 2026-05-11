import math
import os
import tempfile

from constants import FONT_CANDIDATES
from ffmpeg_utils import _run_ffmpeg, get_video_info


def _find_font(font_size):
    """Return an ImageFont for the given size, falling back to the default."""
    from PIL import ImageFont
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, font_size)
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=font_size)  # Pillow 10+
    except TypeError:
        return ImageFont.load_default()


def _render_overlay_png(text, font_size, path, min_height=0):
    """Render text to a transparent PNG. Returns (width, height)."""
    from PIL import Image, ImageDraw

    border_w = 4
    font = _find_font(font_size)

    dummy = Image.new("RGBA", (1, 1))
    draw  = ImageDraw.Draw(dummy)
    bbox  = draw.textbbox((0, 0), text, font=font, stroke_width=border_w)
    w, h  = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])

    if min_height > 0 and h < min_height:
        font_size = int(font_size * min_height / h * 1.05)
        font = _find_font(font_size)
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=border_w)
        w, h = max(1, bbox[2] - bbox[0]), max(1, bbox[3] - bbox[1])

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).text(
        (-bbox[0], -bbox[1]), text, font=font,
        fill="white", stroke_width=border_w, stroke_fill="black",
    )
    img.save(str(path))
    return w, h


def _corner_pos(corner, vid_w, vid_h, png_w, png_h, margin=60):
    if corner == "top-left":
        return margin, margin
    if corner == "top-right":
        return vid_w - png_w - margin, margin
    if corner == "bottom-left":
        return margin, vid_h - png_h - margin
    return vid_w - png_w - margin, vid_h - png_h - margin


def build_countdown_events(
    total_dur, countdown_dur, interval_min, interval_max,
    text1, text1_dur, text2, text2_dur,
    change_points, rng,
    change_point_scores=None,
):
    """
    Return list of (start_sec, end_sec, text_str) tuples for all overlay segments.
    Countdown numerals are expanded into per-second events.

    Sync logic: if change_points provided, shift each countdown start so that
    the text1→text2 transition (countdown_end + text1_dur) lands on the most
    significant musical change point (by z-score) in range. Falls back to nearest
    when scores are unavailable. Among equally significant candidates, prefers one
    where countdown_end also lands on a change point (secondary goal).
    """
    post_dur = (text1_dur if text1 else 0.0) + (text2_dur if text2 and text1 else 0.0)
    sync_offset = text1_dur if text1 else 0.0

    def _safe(s):
        return s.replace("'", "").replace("\\", "") if s else ""

    events = []
    t = 0.0

    while True:
        t += rng.uniform(interval_min, interval_max)
        if t + countdown_dur + post_dur > total_dur:
            break

        cdown_start = t

        if change_points:
            raw_target = cdown_start + countdown_dur + sync_offset
            search_lo = cdown_start + countdown_dur * 0.5
            search_hi = raw_target + 20.0

            if change_point_scores:
                # Pick the most significant change point in the search window.
                # Secondary preference: countdown_end (= cp - text1_dur) also near a cp.
                indexed = [
                    (change_point_scores[j], cp)
                    for j, cp in enumerate(change_points)
                    if search_lo < cp <= search_hi
                ]
                if indexed:
                    max_score = max(s for s, _ in indexed)
                    top_tier = [(s, cp) for s, cp in indexed if s >= max_score * 0.7]

                    def _secondary_dist(cp_val):
                        # Distance from countdown_end to the nearest OTHER change point
                        countdown_end = cp_val - sync_offset
                        dists = [abs(c - countdown_end) for c in change_points if c != cp_val]
                        return min(dists) if dists else float("inf")

                    # Sort: secondary within 1s first, then highest score
                    top_tier.sort(key=lambda x: (_secondary_dist(x[1]) > 1.0, -x[0]))
                    _, best_cp = top_tier[0]
                    new_start = best_cp - countdown_dur - sync_offset
                    if new_start >= 0 and new_start >= t - (interval_max * 0.5):
                        cdown_start = new_start
            else:
                # No scores: fall back to nearest change point
                candidates = [cp for cp in change_points if search_lo < cp <= search_hi]
                if candidates:
                    best = min(candidates, key=lambda cp: abs(cp - raw_target))
                    new_start = best - countdown_dur - sync_offset
                    if new_start >= 0 and new_start >= t - (interval_max * 0.5):
                        cdown_start = new_start

        cdown_end = cdown_start + countdown_dur
        if cdown_end > total_dur:
            break

        n_secs = int(math.ceil(countdown_dur))
        for i in range(n_secs):
            seg_start = cdown_start + i
            seg_end = min(cdown_start + i + 1, cdown_end - 0.04)
            if seg_end <= seg_start:
                break
            events.append((seg_start, seg_end, str(int(math.ceil(countdown_dur - i)))))

        t = cdown_end

        if text1 and t + text1_dur <= total_dur:
            t1_end = t + text1_dur
            events.append((t, t1_end, _safe(text1)))
            t = t1_end

        if text2 and text1 and t + text2_dur <= total_dur:
            t2_end = t + text2_dur
            events.append((t, t2_end, _safe(text2)))
            t = t2_end

    return events


def apply_countdown_overlay(input_path, output_path, events, corner, cancel_event=None, log_fn=None):
    """Re-encode video with countdown/text overlays using Pillow-rendered PNGs."""
    if not events:
        return True

    try:
        from PIL import Image  # noqa: F401 — verify Pillow is available
    except ImportError:
        raise RuntimeError(
            "Pillow is required for text overlay. Install with:  pip install pillow"
        )

    info = get_video_info(str(input_path))
    if not info:
        raise RuntimeError("Could not read video dimensions for overlay")
    vid_w, vid_h = info["width"], info["height"]

    with tempfile.TemporaryDirectory() as tmpdir:
        text_to_entry = {}  # text -> (ffmpeg_input_idx, png_w, png_h)
        png_paths = []

        min_h = int(vid_h * 0.07)
        for _, _, text in events:
            if text not in text_to_entry:
                idx = len(png_paths)
                p = os.path.join(tmpdir, f"ovl_{idx:04d}.png")
                pw, ph = _render_overlay_png(text, max(90, min_h), p, min_height=min_h)
                png_paths.append(p)
                text_to_entry[text] = (idx + 1, pw, ph)  # +1: input 0 is video

        cmd = ["ffmpeg", "-y", "-i", str(input_path)]
        for p in png_paths:
            cmd += ["-i", p]

        fc_parts = []
        prev = "[0:v]"
        for i, (start, end, text) in enumerate(events):
            ffmpeg_idx, pw, ph = text_to_entry[text]
            x, y = _corner_pos(corner, vid_w, vid_h, pw, ph)
            out_label = f"[v{i}]" if i < len(events) - 1 else "[vout]"
            fc_parts.append(
                f"{prev}[{ffmpeg_idx}:v]overlay={x}:{y}"
                f":enable='between(t,{start:.3f},{end:.3f})'{out_label}"
            )
            prev = out_label

        cmd += ["-filter_complex", ";".join(fc_parts)]
        cmd += ["-map", "[vout]"]
        if info["has_audio"]:
            cmd += ["-map", "0:a"]
        cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "copy"]
        cmd.append(str(output_path))

        try:
            _run_ffmpeg(cmd, log_fn=log_fn, cancel_event=cancel_event)
        except RuntimeError as exc:
            if str(exc) == "Cancelled":
                return False
            raise
        return True
