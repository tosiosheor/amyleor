import os
import subprocess
import threading
from pathlib import Path

from audio import mix_music
from constants import FONT_CANDIDATES, OUT_FPS, OUT_H, OUT_W
from ffmpeg_utils import _run, _run_ffmpeg, get_video_info

_XFADE_CHUNK = 60  # max clips per ffmpeg invocation


def _filter(width, height, has_audio, audio_input_index, motion=None, tile_portrait=True):
    """Return (use_complex, filter_str, extra_map_args) for a single clip encode."""
    portrait = height > width
    if portrait:
        y_frac, x_frac, confidence = motion if motion is not None else (0.5, 0.5, 0.0)

        if confidence < 0.35 and tile_portrait:
            # Tile the portrait video 3 times across the screen.
            tile_w = OUT_W // 3
            if tile_w % 2:
                tile_w -= 1
            tile_scale = (
                f"scale={tile_w}:{OUT_H}:force_original_aspect_ratio=increase,"
                f"crop={tile_w}:{OUT_H}"
            )
            fc = (
                f"[0:v]split=3[f1][f2][f3];"
                f"[f1]{tile_scale}[t1];"
                f"[f2]{tile_scale}[t2];"
                f"[f3]{tile_scale}[t3];"
                f"[t1][t2][t3]hstack=inputs=3,setsar=1,fps={OUT_FPS}[vout]"
            )
            maps = ["-map", "[vout]", "-map", f"{audio_input_index}:a"]
            return True, fc, maps

        bg_scale_h = OUT_W * height // width
        if bg_scale_h % 2:
            bg_scale_h += 1
        bg_y = max(0, min(round((bg_scale_h - OUT_H) * y_frac), bg_scale_h - OUT_H))

        if confidence >= 0.35:
            blur = ""
        elif confidence >= 0.1:
            blur = ",boxblur=luma_radius=14:luma_power=2"
        else:
            blur = ",boxblur=luma_radius=28:luma_power=3"

        bg = (
            f"scale={OUT_W}:{bg_scale_h},"
            f"crop={OUT_W}:{OUT_H}:0:{bg_y}"
            f"{blur}"
        )

        fg = f"scale=-2:{OUT_H}"

        if blur == "":
            # No blur: push the portrait clip to the side so the background fill is visible.
            fg_w = OUT_H * width // height
            if fg_w % 2:
                fg_w += 1
            max_shift = (OUT_W - fg_w) // 2 - 8
            shift = max_shift if x_frac >= 0.5 else -max_shift
        else:
            # Blurred background: minor shift toward motion centre (max ±320 px)
            shift = round((0.5 - x_frac) * (OUT_W // 3))
        fc = (
            f"[0:v]split[raw1][raw2];"
            f"[raw1]{bg}[bg];"
            f"[raw2]{fg}[fg];"
            f"[bg][fg]overlay=(W-w)/2+{shift}:(H-h)/2,setsar=1,fps={OUT_FPS}[vout]"
        )
        maps = ["-map", "[vout]", "-map", f"{audio_input_index}:a"]
        return True, fc, maps
    else:
        vf = (
            f"scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,"
            f"crop={OUT_W}:{OUT_H},setsar=1,fps={OUT_FPS}"
        )
        maps = ["-map", "0:v", "-map", f"{audio_input_index}:a"]
        return False, vf, maps


def process_clip(src, dst, start, duration, info, cancel_event=None, motion=None,
                 tile_portrait=True):
    """Transcode one clip to the standard output format."""
    has_audio = info["has_audio"]
    audio_idx = 0 if has_audio else 1

    use_complex, filter_str, maps = _filter(
        info["width"], info["height"], has_audio, audio_idx, motion=motion,
        tile_portrait=tile_portrait,
    )

    cmd = ["ffmpeg", "-y"]
    if start > 0.01:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
    cmd += ["-t", f"{duration:.3f}"]

    if use_complex:
        cmd += ["-filter_complex", filter_str]
    else:
        cmd += ["-vf", filter_str]

    cmd += maps
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    if not has_audio:
        cmd += ["-shortest"]
    cmd.append(str(dst))

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stderr_chunks: list[bytes] = []

    def _drain():
        assert proc.stderr is not None
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            stderr_chunks.append(chunk)

    drain_thread = threading.Thread(target=_drain, daemon=True)
    drain_thread.start()

    while proc.poll() is None:
        if cancel_event and cancel_event.is_set():
            proc.kill()
            drain_thread.join(timeout=2)
            return False
        try:
            proc.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            pass

    drain_thread.join(timeout=5)

    if proc.returncode != 0:
        raise RuntimeError(b"".join(stderr_chunks).decode(errors="replace")[-800:])
    return True


def concat_clips(clip_paths, output_path):
    """Lossless concat of already-encoded clips via the concat demuxer."""
    list_file = Path(output_path).parent / "_concat_list.txt"
    with open(list_file, "w") as f:
        for p in clip_paths:
            f.write(f"file '{str(p).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n")
    try:
        _run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(output_path),
        ])
    finally:
        list_file.unlink(missing_ok=True)


def _xfade_chunk(clip_paths, output_path, fade_dur, log_fn=None, cancel_event=None,
                 muted_flags=None):
    """Concatenate a single batch of clips (<=_XFADE_CHUNK) with xfade/acrossfade."""
    n = len(clip_paths)
    assert 1 <= n <= _XFADE_CHUNK

    durations = []
    for p in clip_paths:
        info = get_video_info(str(p))
        durations.append(info["duration"] if info else 5.0)

    min_dur = min(durations)
    fd = min(fade_dur, min_dur * 0.45)
    fd = max(fd, 0.05)

    cmd = ["ffmpeg", "-y"]
    for p in clip_paths:
        cmd += ["-i", str(p)]

    if n == 1:
        if muted_flags and muted_flags[0]:
            fc = "[0:a]volume=0[aout]"
            cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]"]
            cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
            cmd.append(str(output_path))
            _run(cmd)
        else:
            cmd += ["-map", "0:v", "-map", "0:a", "-c", "copy", str(output_path)]
            _run(cmd)
        return

    fc_parts = []
    prev_v = "[0:v]"
    for i in range(n - 1):
        out_v = "vout" if i == n - 2 else f"vc{i}"
        offset = max(0.01, sum(durations[:i + 1]) - (i + 1) * fd)
        fc_parts.append(
            f"{prev_v}[{i+1}:v]xfade=transition=fade"
            f":duration={fd:.3f}:offset={offset:.3f}[{out_v}]"
        )
        prev_v = f"[{out_v}]"

    clip_audio = []
    for i in range(n):
        if muted_flags and i < len(muted_flags) and muted_flags[i]:
            fc_parts.append(f"[{i}:a]volume=0[am{i}]")
            clip_audio.append(f"[am{i}]")
        else:
            clip_audio.append(f"[{i}:a]")

    prev_a = clip_audio[0]
    for i in range(n - 1):
        out_a = "aclip" if i == n - 2 else f"ac{i}"
        fc_parts.append(
            f"{prev_a}{clip_audio[i + 1]}acrossfade=d={fd:.3f}[{out_a}]"
        )
        prev_a = f"[{out_a}]"

    cmd += ["-filter_complex", ";".join(fc_parts)]
    cmd += ["-map", "[vout]", "-map", "[aclip]"]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    cmd.append(str(output_path))
    _run_ffmpeg(cmd, log_fn=log_fn, cancel_event=cancel_event)


def xfade_concat(clip_paths, output_path, fade_dur, music_path=None, music_vol=0.3,
                 log_fn=None, cancel_event=None, muted_flags=None):
    """
    Concatenate clips with xfade video transitions and acrossfade audio crossfades.
    Optionally mixes in a music track at music_vol (0.0–1.0) relative level.
    muted_flags: optional list[bool] parallel to clip_paths; True silences that clip's audio.
    Large clip sets are processed in chunks to stay within ffmpeg's limits.
    """
    n = len(clip_paths)
    if n == 0:
        raise RuntimeError("No clips to concatenate.")

    tmp_dir = Path(output_path).parent

    if n > _XFADE_CHUNK:
        chunks = [clip_paths[i:i + _XFADE_CHUNK]
                  for i in range(0, n, _XFADE_CHUNK)]
        n_chunks = len(chunks)
        if log_fn:
            log_fn(f"  {n} clips — splitting into {n_chunks} batches of up to {_XFADE_CHUNK}…")
        chunk_outputs = []
        for idx, chunk in enumerate(chunks):
            if log_fn:
                log_fn(f"  Batch {idx+1}/{n_chunks} ({len(chunk)} clips)…")
            chunk_out = tmp_dir / f"_chunk_{idx:04d}.mp4"
            chunk_start = idx * _XFADE_CHUNK
            chunk_flags = (muted_flags[chunk_start:chunk_start + len(chunk)]
                           if muted_flags else None)
            _xfade_chunk(chunk, chunk_out, fade_dur, log_fn=log_fn, cancel_event=cancel_event,
                         muted_flags=chunk_flags)
            chunk_outputs.append(chunk_out)

        if log_fn:
            log_fn(f"  Merging {n_chunks} batches…")
        no_music_out = tmp_dir / "_merged_no_music.mp4"
        xfade_concat(chunk_outputs, no_music_out, fade_dur, music_path=None,
                     log_fn=log_fn, cancel_event=cancel_event)

        for p in chunk_outputs:
            try:
                p.unlink()
            except OSError:
                pass

        if music_path:
            mix_music(str(no_music_out), str(output_path), str(music_path), music_vol,
                      log_fn=log_fn, cancel_event=cancel_event)
            try:
                no_music_out.unlink()
            except OSError:
                pass
        else:
            no_music_out.rename(output_path)
        return

    durations = []
    for p in clip_paths:
        info = get_video_info(str(p))
        durations.append(info["duration"] if info else 5.0)

    min_dur = min(durations)
    fade_dur = min(fade_dur, min_dur * 0.45)
    fade_dur = max(fade_dur, 0.05)

    cmd = ["ffmpeg", "-y"]
    for p in clip_paths:
        cmd += ["-i", str(p)]
    music_idx = n
    if music_path:
        cmd += ["-i", str(music_path)]

    clip0_vol = "0.0" if (muted_flags and muted_flags[0]) else "1.0"

    if n == 1:
        if music_path:
            fc = (
                f"[0:a]volume={clip0_vol}[aclip];"
                f"[{music_idx}:a]volume={music_vol:.3f}[amus];"
                f"[aclip][amus]amix=inputs=2:duration=first:normalize=0[aout]"
            )
            cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]"]
            cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
            cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
        elif muted_flags and muted_flags[0]:
            fc = "[0:a]volume=0[aout]"
            cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[aout]"]
            cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
        else:
            cmd += ["-map", "0:v", "-map", "0:a", "-c", "copy"]
        cmd.append(str(output_path))
        _run_ffmpeg(cmd, log_fn=log_fn, cancel_event=cancel_event)
        return

    fc_parts = []

    prev_v = "[0:v]"
    for i in range(n - 1):
        out_v = "vout" if i == n - 2 else f"vc{i}"
        offset = max(0.01, sum(durations[:i + 1]) - (i + 1) * fade_dur)
        fc_parts.append(
            f"{prev_v}[{i+1}:v]xfade=transition=fade"
            f":duration={fade_dur:.3f}:offset={offset:.3f}[{out_v}]"
        )
        prev_v = f"[{out_v}]"

    clip_audio = []
    for i in range(n):
        if muted_flags and i < len(muted_flags) and muted_flags[i]:
            fc_parts.append(f"[{i}:a]volume=0[am{i}]")
            clip_audio.append(f"[am{i}]")
        else:
            clip_audio.append(f"[{i}:a]")

    prev_a = clip_audio[0]
    for i in range(n - 1):
        out_a = "aclip" if i == n - 2 else f"ac{i}"
        fc_parts.append(
            f"{prev_a}{clip_audio[i + 1]}acrossfade=d={fade_dur:.3f}[{out_a}]"
        )
        prev_a = f"[{out_a}]"

    if music_path:
        fc_parts.append(f"[{music_idx}:a]volume={music_vol:.3f}[amus]")
        fc_parts.append(f"[aclip][amus]amix=inputs=2:duration=first:normalize=0[aout]")
        audio_map = "[aout]"
    else:
        audio_map = "[aclip]"

    cmd += ["-filter_complex", ";".join(fc_parts)]
    cmd += ["-map", "[vout]", "-map", audio_map]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    cmd.append(str(output_path))

    _run_ffmpeg(cmd, log_fn=log_fn, cancel_event=cancel_event)


def apply_intro(input_path, output_path, intro_cfg, cancel_event=None, log_fn=None, font_path=None):
    """Apply intro text overlays to the start of the video.

    Modes:
      'over_clips'  – text fades in/out over actual video content.
      'over_black'  – video is black during intro; clips fade in after the last line.
    """
    lines = intro_cfg.get("lines", [])
    mode = intro_cfg.get("mode", "over_clips")
    fade_dur = max(0.1, float(intro_cfg.get("fade_dur", 0.5)))

    valid_lines = [
        (l["text"].strip(), float(l.get("duration", 5.0)))
        for l in lines
        if l.get("text", "").strip()
    ]
    if not valid_lines:
        return True

    info = get_video_info(str(input_path))
    if not info:
        raise RuntimeError("Could not read video info for intro")
    font_size = max(60, int(info["height"] * 0.09))

    if font_path and os.path.exists(font_path):
        font_file = font_path
    else:
        font_file = next((f for f in FONT_CANDIDATES if os.path.exists(f)), None)

    total_intro_dur = sum(d for _, d in valid_lines)
    vf_parts = []
    af_filter = None

    if mode == "over_black":
        fade_in_start = max(0.0, total_intro_dur - fade_dur)
        vf_parts.append(f"fade=t=in:st={fade_in_start:.3f}:d={fade_dur:.3f}")
        af_filter = f"afade=t=in:st={fade_in_start:.3f}:d={fade_dur:.3f}"

    t = 0.0
    for text, dur in valid_lines:
        end = t + dur
        alpha_expr = (
            f"max(0,min(1,min((t-{t:.3f})/{fade_dur:.3f},({end:.3f}-t)/{fade_dur:.3f})))"
        )
        # Escape special chars for FFmpeg drawtext option values
        safe = text.replace("\\", "\\\\").replace("'", "’").replace(":", "\\:")

        dt_parts = [
            f"text='{safe}'",
            "x=(w-text_w)/2",
            "y=(h-text_h)/2",
            f"fontsize={font_size}",
            "fontcolor=white",
            "borderw=4",
            "bordercolor=black",
            f"alpha='{alpha_expr}'",
            f"enable='between(t,{t:.3f},{end:.3f})'",
        ]
        if font_file:
            ff = font_file.replace("\\", "/")
            # Escape Windows drive-letter colon for FFmpeg filter parser
            if len(ff) > 1 and ff[1] == ":":
                ff = ff[0] + "\\:" + ff[2:]
            dt_parts.insert(0, f"fontfile='{ff}'")

        vf_parts.append("drawtext=" + ":".join(dt_parts))
        t = end

    cmd = ["ffmpeg", "-y", "-i", str(input_path)]
    cmd += ["-vf", ",".join(vf_parts)]
    if af_filter:
        cmd += ["-af", af_filter]
    cmd += ["-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    cmd.append(str(output_path))

    try:
        _run_ffmpeg(cmd, log_fn=log_fn, cancel_event=cancel_event)
    except RuntimeError as exc:
        if str(exc) == "Cancelled":
            return False
        raise
    return True


def apply_outro(input_path, output_path, outro_dur, fade_start=None,
                cancel_event=None, log_fn=None):
    """Re-encode final video with a fade-to-black and audio fade-out at the end.

    fade_start: when to begin the fade; defaults to (duration - outro_dur).
    """
    info = get_video_info(str(input_path))
    if not info:
        raise RuntimeError(f"Could not probe {input_path}")
    total = info["duration"]
    if fade_start is None:
        fade_start = max(0.0, total - outro_dur)
    else:
        fade_start = max(0.0, min(float(fade_start), total - 0.5))

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-vf", f"fade=t=out:st={fade_start:.3f}:d={outro_dur:.3f}",
        "-af", f"afade=t=out:st={fade_start:.3f}:d={outro_dur:.3f}",
        "-c:v", "libx264", "-preset", "fast", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        str(output_path),
    ]
    _run_ffmpeg(cmd, log_fn=log_fn, cancel_event=cancel_event)
