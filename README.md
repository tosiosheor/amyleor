# Video Mixer

A local web app that combines landscape and portrait clips into a single 1920×1080 landscape video, with optional music, beat-synced transitions, countdown overlays, and smart clip selection. Settings and a visual timeline run in the browser; all encoding happens via a local Python/FFmpeg server.

```
python3 amyleor.py           # opens in default browser
python3 amyleor.py --app     # opens in a native window (requires pywebview)
```

**Requires:** `ffmpeg` + `ffprobe` — `brew install ffmpeg`  
**Requires:** `fastapi`, `uvicorn` — `pip install fastapi "uvicorn[standard] pillow librosa"`  
**Optional:** `pywebview` — `pip install pywebview` (native window instead of browser tab)

---

## How it works

### Portrait clip treatment

Portrait clips receive a smart background rather than letterboxing. The clip is analysed for motion activity (~15 sampled frames, frame-difference heuristic) and the result drives three things:

- **Background crop**: the zoomed background pans vertically to keep the most active region centred.
- **Blur level**: fully blurred when motion is diffuse (confidence < 0.4), half-strength when moderately concentrated (≥ 0.4), unblurred when clearly localised (≥ 0.7).
- **Foreground offset**: the sharp portrait overlay shifts left or right (up to ±240 px) away from the active side, exposing relevant background beside it.

When motion is uniform or analysis fails (static clip, ffmpeg error, etc.), the clip falls back to centred + fully blurred. Rotation metadata from phone videos is handled correctly.

### Landscape clip treatment

Scale-to-fill + centre-crop to 16:9. Handles 4:3, ultra-wide, and other aspect ratios.

### Clip selection and timing

- Clips without audio get a silent audio track injected automatically so the concat is seamless.
- **Max clip length**: when a source video is longer than the limit, the script finds the most highlight-dense window (highest scene-change density at 160 px wide) and uses that section. Falls back to the end of the file if analysis fails.
- **Target duration**: clips are added until the target is met or just exceeded — the last clip always plays in full, never cut off. Enable "Use all videos" to skip the target and include every file exactly once instead.
- **Fixed seed**: reproduce the same random order for re-exporting with tweaked settings.

---

## Features

### Music

Set a music folder in "Source & Destination" (optional — leave blank to skip).

- **Beat-sync** (requires `librosa`): detects beats from the first music file and sizes each clip to span exactly N beats (default 8). Clip cuts land on beat boundaries.
- **Cross-fade**: FFmpeg `xfade` filter for video and `acrossfade` for audio, chained across all clips. Configurable duration (default 0.5 s). Works with or without music.
- **Music mixing**: music plays alongside original clip audio. Volume controlled by "Music volume %" (default 30%). Music is looped/concatenated as needed to cover the full duration.
- **Energy ordering**: tracks are randomly selected then sorted chill → intense by RMS loudness, so the video naturally builds in energy over time. Energy values are cached alongside other music metadata.

### Countdown overlay

Optional animated countdown numbers burned into a chosen corner at random intervals.

| Setting | Description |
|---|---|
| Corner | top-left, top-right, bottom-left, bottom-right |
| Countdown duration | Length of the numeric countdown (e.g. 5 s counts 5→1) |
| Interval range | Place a countdown every X–Y seconds (random within range) |
| Text 1 | Optional text shown after the countdown (e.g. `HOLD` for 7 s) |
| Text 2 | Optional second text shown after Text 1 (e.g. `RELEASE` for 4 s) |
| Sync to music | Shifts each countdown so `countdown_end + text1_dur` lands on the nearest musical change point (build/drop/section) within ±20 s |

Text rendering: white, 90 px, 5 px black border. Countdown uses FFmpeg's `%{eif\:ceil(end-t)\:d}` expression.

### Subfolder support

If the input folder contains 2+ subfolders with videos, the output is built in folder order with each subfolder as its own section. Section boundaries snap to the nearest musical change point within ±60 s of the target split. Each subfolder's video cache is stored independently. Beat-sync index threads across sections so cuts stay beat-aligned throughout.

**Subfolder split modes** (Settings → "Subfolder split"):

| Mode | Behaviour |
|---|---|
| Equal | Every folder gets the same share of the target duration (e.g. 3 folders, 9 min target → ~3 min each) |
| By count | Each folder's share is proportional to how many video files it contains |
| By duration | Each folder's share is proportional to the total raw video length it contains |

**Use all videos** (Settings → "Use all videos" checkbox): ignores the target duration entirely and includes every video file exactly once (capped at "Max clip length" if set). Works in both flat and subfolder modes. In subfolder mode the section split mode still determines change-point snapping proportions.

### Settings file / instruction set

"Plan video" generates an editable instruction set (clips, music, countdowns, timestamps, etc.) that can be saved, loaded, and modified before the actual encode. "Generate video" runs the encode from the instruction set. This separates analysis from generation so you can tweak and re-export without re-analysing.

---

## Caching

All expensive analysis is cached so it is never repeated for unchanged files.

| Cache | Location | Key |
|---|---|---|
| Video probe + portrait motion | `.videomixer_cache.json` in the source folder | `mtime` + file size |
| Music audio info + beat list | `.videomixer_cache.json` in the music folder | `mtime` + file size |
| Music RMS energy | same cache file | `mtime` + file size |
| Music change points | same cache file | `mtime` + file size |
| Highlight start offset | in-memory per run | `(path, clip_dur)` |
| Encoded clips | `.videomixer_clip_cache/` in the source folder | SHA-1 of `mtime + size + start + dur + motion + dims + fps` |

Encoded clip cache entries older than 30 days are evicted at the start of each run (first time a source folder is encountered). Cache files silently ignore read/write errors so a read-only folder never crashes the app.

---

## Settings persistence

GUI settings are saved to `~/.videomixer_settings.json` and restored on next launch.

---

## Code structure (for AI context)

The codebase is split into focused modules so each fits in a single AI context window:

| File | Lines | Contents |
|---|---|---|
| `amyleor.py` | ~55 | Entry point — starts uvicorn, opens browser or pywebview window |
| `server.py` | ~220 | FastAPI server — settings, browse dialogs, SSE-streamed plan/generate/cancel endpoints |
| `static/index.html` | ~560 | Single-file web UI — settings accordion, timeline editor, log console |
| `pipeline.py` | ~460 | `do_analyse`, `do_generate`, `build_section_clips` — all processing logic, no GUI deps |
| `constants.py` | 30 | All shared constants (paths, extensions, output dimensions, colours) |
| `cache.py` | 55 | `_load_cache`, `_save_cache`, `_fingerprint`, `_fp_match`, `_clip_cache_key`, `_evict_clip_cache` |
| `ffmpeg_utils.py` | 81 | `_run`, `_run_ffmpeg`, `check_ffmpeg`, `get_video_info` |
| `audio.py` | 118 | `get_audio_info`, `get_music_energy`, `detect_beats`, `prepare_music_audio`, `mix_music` |
| `overlay.py` | 183 | `build_countdown_events`, `apply_countdown_overlay`, `_render_overlay_png`, `_corner_pos` |
| `analysis.py` | 188 | `find_highlight_start`, `analyze_portrait_motion`, `detect_change_points`, `_compute_section_boundaries` |
| `video.py` | 275 | `_filter`, `process_clip`, `concat_clips`, `_xfade_chunk`, `xfade_concat` |
| `app.py` | ~1560 | Legacy tkinter GUI (kept for reference; superseded by the web UI) |

### Architecture

```
browser  ──HTTP/SSE──►  server.py (FastAPI + uvicorn)
                              │
                         pipeline.py
                         (do_analyse / do_generate)
                              │
            ┌─────────────────┼─────────────────┐
         analysis.py      video.py           audio.py
         overlay.py       cache.py           ffmpeg_utils.py
```

- **Plan Video** POSTs settings to `/api/plan`; the server runs `do_analyse` in a background thread and streams log/progress/plan events back via SSE.
- **Generate Video** POSTs the (possibly edited) plan JSON to `/api/generate`; the server runs `do_generate` and streams progress back the same way.
- **Timeline** in the browser lets you drag clips to reorder, click to edit start/duration, or remove clips before generating.

### Key functions

| Function | File | Purpose |
|---|---|---|
| `do_analyse` | `pipeline.py` | Scans folder, probes videos, detects beats/change-points, returns plan dict |
| `do_generate` | `pipeline.py` | Encodes clips from a plan dict, mixes music, applies countdown overlay |
| `get_video_info` | `ffmpeg_utils.py` | ffprobe wrapper — width, height, duration, rotation, has_audio |
| `find_highlight_start` | `analysis.py` | Scene-change density scan for the most active clip window |
| `analyze_portrait_motion` | `analysis.py` | Frame-difference motion analysis for portrait blur/pan/offset |
| `get_music_energy` | `audio.py` | ffmpeg `astats` RMS loudness for chill→intense track ordering |
| `detect_beats` | `audio.py` | librosa beat tracking with cache |
| `detect_change_points` | `analysis.py` | librosa onset-strength z-score for music build/drop detection |
| `_compute_section_boundaries` | `analysis.py` | Snaps proportional subfolder section boundaries to musical change points; accepts optional `weights` for equal/by-count/by-duration splits |
| `build_countdown_events` | `overlay.py` | Returns `(start, end, text)` tuples for all overlay segments, optionally music-synced |
| `apply_countdown_overlay` | `overlay.py` | Re-encodes final video with Pillow-rendered PNG overlays burned in |
| `_filter` | `video.py` | Builds the FFmpeg filtergraph for a single clip (portrait or landscape) |
| `process_clip` | `video.py` | Transcodes one clip to 1920×1080/30 fps |
| `xfade_concat` | `video.py` | Chains clips with xfade/acrossfade and optionally mixes music |
| `concat_clips` | `video.py` | Simple lossless concat via concat demuxer |

---

## Output

- Format: MP4, H.264, AAC
- Resolution: 1920×1080
- Frame rate: 30 fps
- On Mac, the output file is auto-revealed in Finder on completion.
- Cancel button works cleanly mid-encode.
