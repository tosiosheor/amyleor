"""
Core plan/generate pipeline, decoupled from any GUI framework.

Callbacks accepted by do_analyse and do_generate:
  log_fn(msg, color=None)  — color: "accent" | "success" | "error" | "subtle" | None
  status_fn(msg)
  prog_fn(value: int)      — 0-100
  cancel_event             — threading.Event (or None for no cancellation)
"""
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from analysis import (
    _compute_section_boundaries,
    analyze_portrait_motion,
    detect_change_points,
    detect_is_music_only,
    find_highlight_start,
)
from audio import (
    detect_beats,
    get_audio_info,
    get_music_energy,
    prepare_music_audio,
)
from cache import (
    _clip_cache_key,
    _evict_clip_cache,
    _fingerprint,
    _fp_match,
    _load_cache,
    _save_cache,
)
from constants import CLIP_CACHE_DIR_NAME, MUSIC_EXTENSIONS, VIDEO_EXTENSIONS
from ffmpeg_utils import get_video_info
from overlay import apply_countdown_overlay, build_countdown_events
from video import apply_outro, concat_clips, process_clip, xfade_concat


def _noop(*a, **kw):
    pass


def _find_outro_start(total_dur, outro_dur, music_tracks, change_points):
    """Return a music-aligned fade-start time near total_dur, or None."""
    window = max(outro_dur * 2.5, 15.0)
    lo = max(0.0, total_dur - window)
    hi = total_dur + window

    candidates = []

    # Natural track-boundary end-points: cumulative sums, repeated across the video
    if music_tracks:
        period = sum(tr[1] for tr in music_tracks)
        if period > 0:
            t = 0.0
            while t < hi + period:
                t += period
                if lo <= t <= hi:
                    candidates.append(t)

    # Musical change-points (build/drop detections)
    if change_points:
        candidates.extend(cp for cp in change_points if lo <= cp <= hi)

    if not candidates:
        return None

    # Prefer a point just before total_dur so the fade overlaps the final clip
    target = total_dur - outro_dur * 0.25
    return min(candidates, key=lambda c: abs(c - target))


def _is_cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def _ensure_energy(pool, cache_data, log_fn):
    """Compute + cache RMS energy for each (vf, info) in pool. Returns whether cache was dirtied."""
    ecache = cache_data.setdefault("video_energy", {})
    dirty = False
    for vf, info in pool:
        key = vf.name
        if key in ecache and _fp_match(vf, ecache[key]):
            info["energy"] = ecache[key]["energy"]
        else:
            log_fn(f"  Measuring intensity: {vf.name}…", color="subtle")
            energy = get_music_energy(vf)
            info["energy"] = energy
            mtime, size = _fingerprint(vf)
            ecache[key] = {"mtime": mtime, "size": size, "energy": energy}
            dirty = True
    return dirty


def _collect_subfolders(root):
    """DFS pre-order walk; returns all dirs under root that directly contain video files."""
    result = []
    for d in sorted(Path(root).iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        if any(f.suffix.lower() in VIDEO_EXTENSIONS for f in d.iterdir() if f.is_file()):
            result.append(d)
        result.extend(_collect_subfolders(d))
    return result


def build_section_clips(
    pool, section_dur, rng, max_clip, beats, beats_per_clip, beat_idx,
    *, log_fn=_noop, cancel_event=None, hcache=None, clip_order="random",
    use_all=False,
):
    """
    Build clips from pool to fill section_dur seconds.
    Returns (clips, new_beat_idx) or (None, beat_idx) if cancelled.
    """
    clips = []
    total = 0.0
    pool_idx = 0

    if beats and len(beats) > beats_per_clip:
        cycle = 0
        while total < section_dur:
            if _is_cancelled(cancel_event):
                return None, beat_idx
            if beat_idx + beats_per_clip >= len(beats):
                beat_idx = 0
                cycle += 1
                if cycle > 20:
                    break

            if pool_idx >= len(pool):
                if use_all:
                    break
                pool_idx = 0
                rng.shuffle(pool)
            vf, info = pool[pool_idx]
            pool_idx += 1

            raw_dur = info["duration"]
            target = min(raw_dur, max_clip) if max_clip else raw_dur
            n_beats = beats_per_clip
            while (beat_idx + n_beats + 1 < len(beats) and
                   beats[beat_idx + n_beats + 1] - beats[beat_idx] <= target):
                n_beats += 1
            beat_dur = beats[beat_idx + n_beats] - beats[beat_idx]
            beat_idx += n_beats

            if raw_dur >= beat_dur:
                if max_clip and raw_dur > beat_dur:
                    window = min(raw_dur, max_clip)
                    cache_key = f"{vf.name}:{window:.3f}"
                    if hcache is not None and cache_key in hcache and _fp_match(vf, hcache[cache_key]):
                        hs = hcache[cache_key]["start"]
                    else:
                        log_fn(f"  Analysing {vf.name} for highlights…", color="subtle")
                        hs = find_highlight_start(
                            vf, window, info,
                            log_fn=lambda m: log_fn(m, color="subtle"),
                        )
                        if hcache is not None:
                            mtime, size = _fingerprint(vf)
                            hcache[cache_key] = {"mtime": mtime, "size": size, "start": hs}
                    he = hs + window
                    start = round(max(0.0, min(he - beat_dur, raw_dur - beat_dur)), 3)
                else:
                    start = round(rng.uniform(0, raw_dur - beat_dur), 3)
                clip_dur = beat_dur
            else:
                start = 0.0
                clip_dur = raw_dur
            clips.append((vf, info, start, clip_dur))
            total += clip_dur
    else:
        cycle = 0
        while total < section_dur:
            if _is_cancelled(cancel_event):
                return None, beat_idx
            if pool_idx >= len(pool):
                if use_all:
                    break
                pool_idx = 0
                cycle += 1
                rng.shuffle(pool)
                if cycle > 50:
                    break
            vf, info = pool[pool_idx]
            pool_idx += 1
            raw_dur = info["duration"]
            if max_clip and raw_dur > max_clip:
                clip_dur = max_clip
                cache_key = f"{vf.name}:{clip_dur}"
                if hcache is not None and cache_key in hcache and _fp_match(vf, hcache[cache_key]):
                    start = hcache[cache_key]["start"]
                else:
                    log_fn(f"  Analysing {vf.name} for highlights…", color="subtle")
                    start = find_highlight_start(
                        vf, clip_dur, info,
                        log_fn=lambda m: log_fn(m, color="subtle"),
                    )
                    if hcache is not None:
                        mtime, size = _fingerprint(vf)
                        hcache[cache_key] = {"mtime": mtime, "size": size, "start": start}
            else:
                start = 0.0
                clip_dur = raw_dur
            clips.append((vf, info, start, clip_dur))
            total += clip_dur

    return clips, beat_idx


def do_analyse(
    folder, output, target_dur, max_clip, seed,
    music_folder, music_vol, fade_dur, beat_sync, beats_per_clip,
    countdown_cfg=None, clip_order="random",
    subfolder_split="equal", use_all=False, tile_portrait=True,
    auto_mute=False, outro_dur=0.0,
    *, log_fn=_noop, status_fn=_noop, prog_fn=_noop, cancel_event=None,
):
    """Analyse sources and return a plan dict, or None if cancelled."""

    # 1 — Collect videos
    log_fn("Scanning folder for videos…", color="accent")
    all_files = [
        f for f in Path(folder).iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS and not f.name.startswith("._")
    ]
    if all_files:
        log_fn(f"  Found {len(all_files)} root-level file(s)")

    # 2 — Probe videos (with folder-level cache)
    log_fn("Reading video metadata…", color="accent")
    vcache_data = _load_cache(folder)
    vcache = vcache_data.setdefault("video_info", {})
    vcache_dirty = False
    pool = []
    for f in all_files:
        key = f.name
        if key in vcache and _fp_match(f, vcache[key]):
            info = vcache[key]["info"]
            cached_tag = " (cached)"
        else:
            info = get_video_info(f)
            if info:
                mtime, size = _fingerprint(f)
                vcache[key] = {"mtime": mtime, "size": size, "info": info}
                vcache_dirty = True
            cached_tag = ""
        if info:
            orient = "portrait" if info["height"] > info["width"] else "landscape"
            log_fn(f"  ✓ {f.name}  {info['width']}×{info['height']} {orient}"
                   f"  {info['duration']:.1f}s{cached_tag}")
            pool.append((f, info))
        else:
            log_fn(f"  ✗ {f.name}  (skipped — unreadable)", color="subtle")
    if clip_order == "intensity" and pool:
        log_fn("Computing clip intensity…", color="accent")
        if _ensure_energy(pool, vcache_data, log_fn):
            vcache_dirty = True

    if vcache_dirty:
        _save_cache(folder, vcache_data)

    _subfolders = _collect_subfolders(folder)
    _subfolder_mode = len(_subfolders) >= 2

    if not pool and not _subfolder_mode:
        raise RuntimeError("No readable video files found.")

    # 3 — Collect & analyse music
    music_files = []
    music_tracks = []
    beats = None
    change_points = None
    if music_folder and os.path.isdir(music_folder):
        log_fn("\nScanning music folder…", color="accent")
        music_files = sorted([
            f for f in Path(music_folder).iterdir()
            if f.is_file() and f.suffix.lower() in MUSIC_EXTENSIONS
        ])
        log_fn(f"  Found {len(music_files)} music file(s)")

        mcache_data = _load_cache(music_folder)
        acache = mcache_data.setdefault("audio_info", {})
        bcache = mcache_data.setdefault("beats", {})
        ecache = mcache_data.setdefault("energy", {})
        mcache_dirty = False

        n_music = len(music_files)
        for i, mf in enumerate(music_files, 1):
            status_fn(f"Analysing music {i}/{n_music}…")
            key = mf.name
            if key in acache and _fp_match(mf, acache[key]):
                ainfo = acache[key]["info"]
            else:
                ainfo = get_audio_info(mf)
                if ainfo:
                    mtime, size = _fingerprint(mf)
                    acache[key] = {"mtime": mtime, "size": size, "info": ainfo}
                    mcache_dirty = True
            if ainfo:
                if key in ecache and _fp_match(mf, ecache[key]):
                    energy = ecache[key]["rms"]
                else:
                    energy = get_music_energy(mf)
                    mtime, size = _fingerprint(mf)
                    ecache[key] = {"mtime": mtime, "size": size, "rms": energy}
                    mcache_dirty = True
                music_tracks.append((mf, ainfo["duration"], energy))

        if music_tracks and beat_sync:
            log_fn("Detecting beats…", color="accent")
            first = music_files[0]
            key = first.name
            if key in bcache and _fp_match(first, bcache[key]):
                beats = bcache[key]["beats"]
                log_fn(f"  {len(beats)} beats (cached)", color="subtle")
            else:
                beats = detect_beats(first, log_fn=lambda m: log_fn(m, color="subtle"))
                if beats is not None:
                    mtime, size = _fingerprint(first)
                    bcache[key] = {"mtime": mtime, "size": size, "beats": beats}
                    mcache_dirty = True
            if beats is None:
                log_fn("  Falling back to regular clip timing.", color="subtle")

        need_change_points = _subfolder_mode or (countdown_cfg and countdown_cfg["sync"]) or outro_dur > 0
        if need_change_points and music_files:
            cpcache = mcache_data.setdefault("change_points", {})
            first = music_files[0]
            key = first.name
            if key in cpcache and _fp_match(first, cpcache[key]):
                change_points = cpcache[key]["points"]
                log_fn(f"  {len(change_points)} change point(s) (cached)", color="subtle")
            else:
                change_points = detect_change_points(
                    first, log_fn=lambda m: log_fn(m, color="subtle")
                )
                if change_points is not None:
                    mtime, size = _fingerprint(first)
                    cpcache[key] = {"mtime": mtime, "size": size, "points": change_points}
                    mcache_dirty = True

        if mcache_dirty:
            _save_cache(music_folder, mcache_data)

    # 4 — Plan clip list
    rng = random.Random(seed)
    if music_tracks:
        rng.shuffle(music_tracks)

    if _subfolder_mode:
        log_fn(f"\nSubfolder mode: {len(_subfolders)} folder(s) detected.", color="accent")

        subfolder_pools = []
        for sf in _subfolders:
            sf_files = sorted([
                f for f in sf.iterdir()
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS and not f.name.startswith("._")
            ])
            sf_cache_data = _load_cache(sf)
            sf_vcache = sf_cache_data.setdefault("video_info", {})
            sf_dirty = False
            sf_pool = []
            for f in sf_files:
                key = f.name
                if key in sf_vcache and _fp_match(f, sf_vcache[key]):
                    info = sf_vcache[key]["info"]
                else:
                    info = get_video_info(f)
                    if info:
                        mtime, size = _fingerprint(f)
                        sf_vcache[key] = {"mtime": mtime, "size": size, "info": info}
                        sf_dirty = True
                if info:
                    sf_pool.append((f, info))
            if clip_order == "intensity" and sf_pool:
                if _ensure_energy(sf_pool, sf_cache_data, log_fn):
                    sf_dirty = True
            if sf_dirty:
                _save_cache(sf, sf_cache_data)
            if sf_pool:
                log_fn(f"  {sf.name}: {len(sf_pool)} video(s)")
                subfolder_pools.append((sf, sf_pool, sf_cache_data))
            else:
                log_fn(f"  {sf.name}: no usable videos — skipped", color="subtle")

        if not subfolder_pools:
            raise RuntimeError("No usable videos found in any subfolder.")

        n_secs = len(subfolder_pools)

        # Compute proportional weights for section boundaries
        if subfolder_split == "by_count":
            total_count = sum(len(sf_pool) for _, sf_pool, _ in subfolder_pools)
            weights = [len(sf_pool) / total_count for _, sf_pool, _ in subfolder_pools]
        elif subfolder_split == "by_duration":
            sf_raw_durs = [
                sum(info["duration"] for _, info in sf_pool)
                for _, sf_pool, _ in subfolder_pools
            ]
            grand_dur = sum(sf_raw_durs)
            weights = [d / grand_dur for d in sf_raw_durs] if grand_dur > 0 else None
        else:
            weights = None

        if use_all:
            # Estimate section durations from actual clip lengths for change-point snapping
            clip_est = lambda info: min(max_clip, info["duration"]) if max_clip else info["duration"]
            sf_est_durs = [
                sum(clip_est(info) for _, info in sf_pool)
                for _, sf_pool, _ in subfolder_pools
            ]
            est_total = sum(sf_est_durs)
            est_weights = [d / est_total for d in sf_est_durs] if est_total > 0 else None
            section_durs, boundary_info = _compute_section_boundaries(
                n_secs, est_total, change_points, weights=est_weights
            )
            build_durs = [float("inf")] * n_secs
        else:
            section_durs, boundary_info = _compute_section_boundaries(
                n_secs, target_dur, change_points, weights=weights
            )
            build_durs = section_durs

        if change_points and boundary_info:
            for i, (raw, snapped) in enumerate(boundary_info):
                if abs(snapped - raw) > 1.0:
                    log_fn(
                        f"  Boundary after section {i+1}: "
                        f"{raw:.0f}s → {snapped:.0f}s (music sync)",
                        color="subtle",
                    )

        clips = []
        total = 0.0
        beat_idx = 0
        for (sf, sf_pool, sf_cache_data), sec_dur, build_dur in zip(
            subfolder_pools, section_durs, build_durs
        ):
            sf_pool_copy = list(sf_pool)
            rng.shuffle(sf_pool_copy)
            dur_label = "all videos" if use_all else f"target {sec_dur:.0f}s"
            log_fn(f"\n  Section '{sf.name}' — {dur_label}", color="accent")
            sf_hcache = sf_cache_data.setdefault("highlights", {})
            sf_hcache_prev_len = len(sf_hcache)
            sec_clips, beat_idx = build_section_clips(
                sf_pool_copy, build_dur, rng, max_clip, beats, beats_per_clip, beat_idx,
                log_fn=log_fn, cancel_event=cancel_event, hcache=sf_hcache,
                clip_order=clip_order, use_all=use_all,
            )
            if len(sf_hcache) > sf_hcache_prev_len:
                _save_cache(sf, sf_cache_data)
            if sec_clips is None:
                log_fn("Cancelled.", color="subtle")
                status_fn("Cancelled")
                return None
            if clip_order == "intensity" and sec_clips:
                sec_clips.sort(key=lambda c: c[1].get("energy") or float("-inf"))
            sec_total = sum(d for _, _, _, d in sec_clips)
            clips.extend(sec_clips)
            total += sec_total
            log_fn(f"    → {len(sec_clips)} clip(s), {sec_total:.1f}s")

        log_fn(f"\nTotal: {len(clips)} clip(s) → ~{total:.1f}s")

    else:
        rng.shuffle(pool)
        clips = []
        total = 0.0
        pool_idx = 0

        dur_label = "all videos" if use_all else f"target: {target_dur:.0f}s"
        log_fn(f"\nBuilding clip list ({dur_label})…", color="accent")
        effective_target = float("inf") if use_all else target_dur

        if change_points and not use_all and countdown_cfg and countdown_cfg.get("sync"):
            # Build clips so transitions land exactly on music change points.
            hcache = vcache_data.setdefault("highlights", {})
            hcache_prev_len = len(hcache)
            all_cps = sorted(t for t in change_points if 0 < t < effective_target)
            boundaries = [0.0] + all_cps + [effective_target]
            for b_idx in range(len(boundaries) - 1):
                iv_remaining = round(boundaries[b_idx + 1] - boundaries[b_idx], 3)
                while iv_remaining > 0.05:
                    if _is_cancelled(cancel_event):
                        return None
                    if pool_idx >= len(pool):
                        pool_idx = 0
                        rng.shuffle(pool)
                    vf, info = pool[pool_idx]
                    pool_idx += 1
                    raw_dur = info["duration"]
                    clip_dur = round(
                        min(iv_remaining, max_clip, raw_dur) if max_clip
                        else min(iv_remaining, raw_dur),
                        3,
                    )
                    if raw_dur > clip_dur:
                        cache_key = f"{vf.name}:{clip_dur:.3f}"
                        if cache_key in hcache and _fp_match(vf, hcache[cache_key]):
                            start = hcache[cache_key]["start"]
                        else:
                            log_fn(f"  Analysing {vf.name} for highlights…", color="subtle")
                            start = find_highlight_start(
                                vf, clip_dur, info,
                                log_fn=lambda m: log_fn(m, color="subtle"),
                            )
                            mtime, size = _fingerprint(vf)
                            hcache[cache_key] = {"mtime": mtime, "size": size, "start": start}
                    else:
                        start = 0.0
                        clip_dur = raw_dur
                    clips.append((vf, info, start, clip_dur))
                    total += clip_dur
                    iv_remaining = round(iv_remaining - clip_dur, 3)
            if len(hcache) > hcache_prev_len:
                _save_cache(folder, vcache_data)
            log_fn(f"  Change-point synced: {len(clips)} clip(s) → ~{total:.1f}s total")
        elif beats and len(beats) > beats_per_clip:
            beat_idx = 0
            cycle = 0
            hcache = vcache_data.setdefault("highlights", {})
            hcache_prev_len = len(hcache)
            while total < effective_target:
                if _is_cancelled(cancel_event):
                    return None
                if beat_idx + beats_per_clip >= len(beats):
                    beat_idx = 0
                    cycle += 1
                    if cycle > 20:
                        break

                if pool_idx >= len(pool):
                    if use_all:
                        break
                    pool_idx = 0
                    rng.shuffle(pool)
                vf, info = pool[pool_idx]
                pool_idx += 1

                raw_dur = info["duration"]
                target = min(raw_dur, max_clip) if max_clip else raw_dur
                n_beats = beats_per_clip
                while (beat_idx + n_beats + 1 < len(beats) and
                       beats[beat_idx + n_beats + 1] - beats[beat_idx] <= target):
                    n_beats += 1
                beat_dur = beats[beat_idx + n_beats] - beats[beat_idx]
                beat_idx += n_beats

                if raw_dur >= beat_dur:
                    if max_clip and raw_dur > beat_dur:
                        window = min(raw_dur, max_clip)
                        cache_key = f"{vf.name}:{window:.3f}"
                        if cache_key in hcache and _fp_match(vf, hcache[cache_key]):
                            hs = hcache[cache_key]["start"]
                        else:
                            log_fn(f"  Analysing {vf.name} for highlights…", color="subtle")
                            hs = find_highlight_start(
                                vf, window, info,
                                log_fn=lambda m: log_fn(m, color="subtle"),
                            )
                            mtime, size = _fingerprint(vf)
                            hcache[cache_key] = {"mtime": mtime, "size": size, "start": hs}
                        he = hs + window
                        start = round(max(0.0, min(he - beat_dur, raw_dur - beat_dur)), 3)
                    else:
                        start = round(rng.uniform(0, raw_dur - beat_dur), 3)
                    clip_dur = beat_dur
                else:
                    start = 0.0
                    clip_dur = raw_dur
                clips.append((vf, info, start, clip_dur))
                total += clip_dur

            if len(hcache) > hcache_prev_len:
                _save_cache(folder, vcache_data)
            log_fn(f"  Beat-aligned: {len(clips)} clip(s) → ~{total:.1f}s total")
        else:
            cycle = 0
            hcache = vcache_data.setdefault("highlights", {})
            hcache_prev_len = len(hcache)
            while total < effective_target:
                if _is_cancelled(cancel_event):
                    return None
                if pool_idx >= len(pool):
                    if use_all:
                        break
                    pool_idx = 0
                    cycle += 1
                    rng.shuffle(pool)
                    if cycle > 50:
                        break
                vf, info = pool[pool_idx]
                pool_idx += 1

                raw_dur = info["duration"]
                if max_clip and raw_dur > max_clip:
                    clip_dur = max_clip
                    cache_key = f"{vf.name}:{clip_dur}"
                    if cache_key in hcache and _fp_match(vf, hcache[cache_key]):
                        start = hcache[cache_key]["start"]
                    else:
                        log_fn(f"  Analysing {vf.name} for highlights…", color="subtle")
                        start = find_highlight_start(
                            vf, clip_dur, info,
                            log_fn=lambda m: log_fn(m, color="subtle"),
                        )
                        mtime, size = _fingerprint(vf)
                        hcache[cache_key] = {"mtime": mtime, "size": size, "start": start}
                else:
                    start = 0.0
                    clip_dur = raw_dur
                clips.append((vf, info, start, clip_dur))
                total += clip_dur
            if len(hcache) > hcache_prev_len:
                _save_cache(folder, vcache_data)

            log_fn(f"  Planned {len(clips)} clip(s) → ~{total:.1f}s total")

        if clip_order == "intensity" and clips:
            clips.sort(key=lambda c: c[1].get("energy") or float("-inf"))

    # Outro: snap fade-start to a music cue and extend clips if needed
    outro_start = None
    if outro_dur > 0:
        outro_start = _find_outro_start(total, outro_dur, music_tracks, change_points)
        if outro_start is not None:
            log_fn(
                f"\nOutro: music-aligned fade at {outro_start:.1f}s "
                f"(video total {total:.1f}s, fade {outro_dur:.1f}s)",
                color="accent",
            )
            outro_end = outro_start + outro_dur
            if outro_end > total:
                extra_needed = outro_end - total
                log_fn(f"  Extending {extra_needed:.1f}s to reach outro…", color="subtle")
                ext_pool = list(pool) if pool else (
                    list(subfolder_pools[-1][1]) if _subfolder_mode and subfolder_pools else []
                )
                if ext_pool:
                    rng.shuffle(ext_pool)
                    extra_clips, _ = build_section_clips(
                        ext_pool, extra_needed, rng, max_clip, beats, beats_per_clip, 0,
                        log_fn=log_fn, cancel_event=cancel_event, use_all=False,
                    )
                    if extra_clips:
                        clips.extend(extra_clips)
                        total += sum(d for _, _, _, d in extra_clips)
                        log_fn(f"  → {len(extra_clips)} clip(s) added, total {total:.1f}s")
        else:
            log_fn(
                "\nOutro: no music cue found near end — fade from last clip.",
                color="subtle",
            )
            outro_start = max(0.0, total - outro_dur)

    # Portrait motion analysis
    portrait_clips = [
        (i, vf, info, start, dur)
        for i, (vf, info, start, dur) in enumerate(clips)
        if info["height"] > info["width"]
    ]
    motion_by_idx = {}
    if portrait_clips:
        log_fn("\nAnalysing portrait motion…", color="accent")
        folder_clips: dict = {}
        for item in portrait_clips:
            folder_clips.setdefault(str(item[1].parent), []).append(item)
        for folder_str, fclips in folder_clips.items():
            cache_data = _load_cache(folder_str)
            mcache = cache_data.setdefault("portrait_motion", {})
            dirty = False
            for i, vf, info, start, dur in fclips:
                if _is_cancelled(cancel_event):
                    return None
                cache_key = f"{vf.name}:{start:.3f}:{dur:.3f}"
                if cache_key in mcache and _fp_match(vf, mcache[cache_key]):
                    motion = tuple(mcache[cache_key]["motion"])
                    log_fn(
                        f"  {vf.name} y={motion[0]:.2f} x={motion[1]:.2f}"
                        f" conf={motion[2]:.2f} (cached)",
                        color="subtle",
                    )
                else:
                    log_fn(f"  Analysing {vf.name}…", color="subtle")
                    motion = analyze_portrait_motion(vf, start, dur, info["width"], info["height"])
                    mtime, size = _fingerprint(vf)
                    mcache[cache_key] = {"mtime": mtime, "size": size, "motion": list(motion)}
                    dirty = True
                    log_fn(
                        f"    motion y={motion[0]:.2f} x={motion[1]:.2f}"
                        f" conf={motion[2]:.2f}",
                        color="subtle",
                    )
                motion_by_idx[i] = motion
            if dirty:
                _save_cache(folder_str, cache_data)

    # Auto-mute: detect music-only clips and mark them muted
    music_only_by_idx = {}
    if auto_mute:
        log_fn("\nDetecting music-only clips…", color="accent")
        folder_clips_music: dict = {}
        for idx, (vf, info, start, dur) in enumerate(clips):
            if info.get("has_audio", True):
                folder_clips_music.setdefault(str(vf.parent), []).append(
                    (idx, vf, info, start, dur)
                )
        for folder_str, fclips in folder_clips_music.items():
            cache_data = _load_cache(folder_str)
            mcache = cache_data.setdefault("music_detect", {})
            dirty = False
            for idx, vf, info, start, dur in fclips:
                if _is_cancelled(cancel_event):
                    return None
                cache_key = f"{vf.name}:{start:.3f}:{dur:.3f}"
                if cache_key in mcache and _fp_match(vf, mcache[cache_key]):
                    is_music = mcache[cache_key]["is_music_only"]
                    log_fn(
                        f"  {vf.name}: {'music only' if is_music else 'has voice'} (cached)",
                        color="subtle",
                    )
                else:
                    log_fn(f"  Analysing {vf.name}…", color="subtle")
                    is_music = detect_is_music_only(vf, start, dur)
                    mtime, size = _fingerprint(vf)
                    mcache[cache_key] = {"mtime": mtime, "size": size, "is_music_only": is_music}
                    dirty = True
                    log_fn(
                        f"    → {'music only — will mute' if is_music else 'voice detected — keeping audio'}",
                        color="subtle",
                    )
                music_only_by_idx[idx] = is_music
            if dirty:
                _save_cache(folder_str, cache_data)

    # Sort music chill → intense
    if music_tracks:
        selected, acc = [], 0.0
        for track in music_tracks:
            selected.append(track)
            acc += track[1]
            if acc >= total:
                break
        selected.sort(key=lambda t: t[2] if t[2] is not None else float("-inf"))
        music_tracks = selected
        log_fn("  Music order (chill → intense): " +
               ", ".join(t[0].name for t in music_tracks), color="subtle")

    plan = {
        "version": 1,
        "output_path": output,
        "crossfade_dur": fade_dur,
        "music_vol": music_vol,
        "seed": seed,
        "tile_portrait": tile_portrait,
        "clips": [
            {
                "path": str(vf),
                "start": round(start, 3),
                "duration": round(dur, 3),
                "width": info["width"],
                "height": info["height"],
                "has_audio": info["has_audio"],
                "motion": list(motion_by_idx[i]) if i in motion_by_idx else None,
                "muted": music_only_by_idx.get(i, False),
            }
            for i, (vf, info, start, dur) in enumerate(clips)
        ],
        "music": [
            {"path": str(mf), "duration": round(dur, 3)}
            for mf, dur, *_ in music_tracks
        ],
        "countdown": None,
        "outro_dur": outro_dur,
        "outro_start": outro_start,
    }

    if countdown_cfg:
        plan["countdown"] = {
            "dur": countdown_cfg["dur"],
            "iv_min": countdown_cfg["iv_min"],
            "iv_max": countdown_cfg["iv_max"],
            "corner": countdown_cfg["corner"],
            "text1": countdown_cfg["text1"],
            "text1_dur": countdown_cfg["text1_dur"],
            "text2": countdown_cfg["text2"],
            "text2_dur": countdown_cfg["text2_dur"],
            "sync": countdown_cfg["sync"],
            "change_points": change_points,
        }

    prog_fn(40)
    status_fn("Plan ready")
    return plan


def _unique_path(path):
    p = Path(path)
    if not p.exists():
        return str(p)
    stem, suffix, parent = p.stem, p.suffix, p.parent
    n = 1
    while True:
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return str(candidate)
        n += 1


def do_generate(
    plan,
    *, log_fn=_noop, status_fn=_noop, prog_fn=_noop, cancel_event=None,
):
    """Encode clips and produce the final video from a plan dict."""
    if cancel_event is None:
        cancel_event = threading.Event()

    output = plan["output_path"]
    output = _unique_path(output)
    fade_dur = plan.get("crossfade_dur", 0.0)
    music_vol = plan.get("music_vol", 0.3)
    seed = plan.get("seed")
    tile_portrait = plan.get("tile_portrait", True)
    clips_data = plan.get("clips", [])
    music_data = plan.get("music", [])
    countdown_cfg = plan.get("countdown")
    outro_dur = plan.get("outro_dur", 0.0)
    outro_start = plan.get("outro_start")

    music_tracks = [(Path(m["path"]), m["duration"]) for m in music_data]
    total = sum(c["duration"] for c in clips_data)
    n = len(clips_data)

    log_fn(f"Encoding {n} clip(s)…", color="accent")

    _motion_caches = {}
    _motion_dirty = set()

    def _get_motion_cache(folder):
        key = str(folder)
        if key not in _motion_caches:
            _motion_caches[key] = _load_cache(folder)
        return _motion_caches[key]

    def _flush_motion_caches():
        for fk in _motion_dirty:
            _save_cache(fk, _motion_caches[fk])
        _motion_dirty.clear()

    _evicted_clip_caches = set()

    with tempfile.TemporaryDirectory(prefix="videomixer_") as tmp:
        tmp_path = Path(tmp)
        encoded = []

        for i, c in enumerate(clips_data):
            if _is_cancelled(cancel_event):
                _flush_motion_caches()
                log_fn("Cancelled.", color="subtle")
                status_fn("Cancelled")
                return

            prog_fn(int(i / n * 80))
            status_fn(f"Clip {i+1} / {n}")
            vf = Path(c["path"])
            start = c["start"]
            dur = c["duration"]
            info = {
                "width": c["width"],
                "height": c["height"],
                "has_audio": c.get("has_audio", True),
                "duration": dur,
            }
            orient = "portrait" if info["height"] > info["width"] else "landscape"
            log_fn(
                f"  [{i+1:>{len(str(n))}}/{n}] {vf.name}"
                f"  {info['width']}×{info['height']} ({orient})"
                f"  start={start:.1f}s  dur={dur:.1f}s"
            )

            motion = None
            if orient == "portrait":
                motion_data = c.get("motion")
                if motion_data is not None:
                    motion = tuple(motion_data)
                    log_fn(
                        f"    motion y={motion[0]:.2f} x={motion[1]:.2f}"
                        f" conf={motion[2]:.2f} (from plan)",
                        color="subtle",
                    )
                else:
                    folder_key = str(vf.parent)
                    cache_data = _get_motion_cache(vf.parent)
                    mcache = cache_data.setdefault("portrait_motion", {})
                    cache_key = f"{vf.name}:{start:.3f}:{dur:.3f}"
                    if cache_key in mcache and _fp_match(vf, mcache[cache_key]):
                        motion = tuple(mcache[cache_key]["motion"])
                        log_fn(
                            f"    motion y={motion[0]:.2f} x={motion[1]:.2f}"
                            f" conf={motion[2]:.2f} (cached)",
                            color="subtle",
                        )
                    else:
                        log_fn("    Analysing portrait motion…", color="subtle")
                        motion = analyze_portrait_motion(vf, start, dur, info["width"], info["height"])
                        mtime, size = _fingerprint(vf)
                        mcache[cache_key] = {"mtime": mtime, "size": size, "motion": list(motion)}
                        _motion_dirty.add(folder_key)
                        log_fn(
                            f"    motion y={motion[0]:.2f} x={motion[1]:.2f}"
                            f" conf={motion[2]:.2f}",
                            color="subtle",
                        )

            out_clip = tmp_path / f"clip_{i:05d}.mp4"

            clip_cache_dir = vf.parent / CLIP_CACHE_DIR_NAME
            try:
                clip_cache_dir.mkdir(exist_ok=True)
                if str(clip_cache_dir) not in _evicted_clip_caches:
                    _evict_clip_cache(clip_cache_dir)
                    _evicted_clip_caches.add(str(clip_cache_dir))
            except Exception:
                clip_cache_dir = None

            cached_encoded = None
            if clip_cache_dir:
                ck = _clip_cache_key(vf, start, dur, motion)
                cached_encoded = clip_cache_dir / f"{ck}.mp4"

            if cached_encoded and cached_encoded.exists():
                shutil.copy2(cached_encoded, out_clip)
                cached_encoded.touch()
                log_fn("    (encoded clip cached)", color="subtle")
            else:
                ok = process_clip(vf, out_clip, start, dur, info, cancel_event, motion=motion,
                                  tile_portrait=tile_portrait)
                if not ok:
                    _flush_motion_caches()
                    log_fn("Cancelled.", color="subtle")
                    status_fn("Cancelled")
                    return
                if cached_encoded:
                    try:
                        shutil.copy2(out_clip, cached_encoded)
                    except Exception:
                        pass

            encoded.append(out_clip)

        _flush_motion_caches()

        music_path = None
        if music_tracks:
            log_fn("\nPreparing music…", color="accent")
            status_fn("Preparing music…")
            prog_fn(82)
            try:
                music_path = prepare_music_audio(music_tracks, total, tmp_path)
                if music_path is None:
                    log_fn("  Warning: could not prepare music audio — skipping.", color="error")
            except Exception as e:
                log_fn(f"  Warning: music preparation failed: {e} — skipping.", color="error")

        prog_fn(85)
        use_xfade = fade_dur > 0 or music_path is not None

        if use_xfade:
            log_fn("\nPass 1: Cross-fading clips and mixing music into video…", color="accent")
            status_fn("Mixing…")
            muted_flags = [bool(c.get("muted", False)) for c in clips_data]
            try:
                xfade_concat(
                    encoded, Path(output), fade_dur, music_path, music_vol,
                    log_fn=lambda m: log_fn(m, color="subtle"),
                    cancel_event=cancel_event,
                    muted_flags=muted_flags,
                )
            except RuntimeError as exc:
                if str(exc) == "Cancelled":
                    log_fn("Cancelled.", color="subtle")
                    status_fn("Cancelled")
                    return
                raise
        else:
            log_fn("\nPass 1: Concatenating clips…", color="accent")
            status_fn("Concatenating…")
            concat_clips(encoded, Path(output))

    if countdown_cfg:
        log_fn("\nPass 2: Burning countdown overlay (full re-encode of video)…", color="accent")
        status_fn("Countdown overlay…")
        prog_fn(90)

        final_info = get_video_info(output)
        final_dur = final_info["duration"] if final_info else total

        if countdown_cfg.get("sync"):
            clip_durations = [c["duration"] for c in clips_data]
            n_clips = len(clip_durations)
            actual_fd = (
                max(0.0, (sum(clip_durations) - final_dur) / (n_clips - 1))
                if n_clips > 1 else 0.0
            )
            stored_cps = countdown_cfg.get("change_points")
            if stored_cps:
                # Convert music-timeline change points to video timeline by subtracting
                # the accumulated crossfade offset for each point's position in the clip list.
                music_cumsum = 0.0
                music_ends = []
                for d in clip_durations:
                    music_cumsum += d
                    music_ends.append(music_cumsum)
                cp = []
                for t in stored_cps:
                    for i, me in enumerate(music_ends):
                        if t <= me:
                            cp.append(max(0.0, t - i * actual_fd))
                            break
                    else:
                        cp.append(max(0.0, t - (n_clips - 1) * actual_fd))
            else:
                # No stored change points — fall back to clip boundary positions
                cumsum = 0.0
                cp = []
                for i, d in enumerate(clip_durations[:-1]):
                    cumsum += d
                    cp.append(max(0.0, cumsum - (i + 1) * actual_fd))
        else:
            cp = None

        events = build_countdown_events(
            total_dur=final_dur,
            countdown_dur=countdown_cfg["dur"],
            interval_min=countdown_cfg["iv_min"],
            interval_max=countdown_cfg["iv_max"],
            text1=countdown_cfg["text1"],
            text1_dur=countdown_cfg["text1_dur"],
            text2=countdown_cfg["text2"],
            text2_dur=countdown_cfg["text2_dur"],
            change_points=cp,
            rng=random.Random(seed),
        )

        if events:
            log_fn(f"  {len(events)} overlay segment(s) planned", color="subtle")
            overlay_tmp = Path(output).with_suffix(".overlay_tmp.mp4")
            ok = apply_countdown_overlay(
                Path(output), overlay_tmp, events, countdown_cfg["corner"], cancel_event,
                log_fn=lambda m: log_fn(m, color="subtle"),
            )
            if not ok:
                log_fn("Cancelled.", color="subtle")
                status_fn("Cancelled")
                overlay_tmp.unlink(missing_ok=True)
                return
            overlay_tmp.replace(Path(output))
        else:
            log_fn("  No countdown events fit within video duration — skipping.", color="subtle")

    if outro_dur and outro_dur > 0:
        log_fn("\nPass: Applying outro (fade to black + silence)…", color="accent")
        status_fn("Outro…")
        prog_fn(95)
        outro_tmp = Path(output).with_suffix(".outro_tmp.mp4")
        try:
            apply_outro(
                Path(output), outro_tmp, outro_dur,
                fade_start=outro_start,
                cancel_event=cancel_event,
                log_fn=lambda m: log_fn(m, color="subtle"),
            )
        except RuntimeError as exc:
            if str(exc) == "Cancelled":
                log_fn("Cancelled.", color="subtle")
                status_fn("Cancelled")
                outro_tmp.unlink(missing_ok=True)
                return
            raise
        outro_tmp.replace(Path(output))

    prog_fn(100)
    log_fn(f"\n✅  Done!  →  {output}", color="success")
    status_fn("Complete!")

    if sys.platform == "darwin":
        subprocess.run(["open", "-R", output], check=False)
