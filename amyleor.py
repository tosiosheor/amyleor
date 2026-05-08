#!/usr/bin/env python3
"""
Video Mixer — combines landscape and portrait videos into a single landscape output.
Portrait clips get a blurred+zoomed background treatment.
Optionally overlays a music folder with beat-synced clip transitions and cross-fades.
Requires: ffmpeg + ffprobe  →  brew install ffmpeg
Optional: librosa           →  pip install librosa  (for beat detection)

Modules:
  constants.py    — shared constants (paths, extensions, output dimensions, colours)
  cache.py        — folder-level and clip-level caching helpers
  ffmpeg_utils.py — ffmpeg/ffprobe wrappers
  analysis.py     — highlight detection, portrait motion, change points, section boundaries
  audio.py        — audio info, beat detection, music prep/mixing
  video.py        — clip encoding, concat, xfade
  overlay.py      — countdown/text PNG overlay
  app.py          — VideoMixer tkinter GUI
"""

from app import VideoMixer

if __name__ == "__main__":
    app = VideoMixer()
    app.mainloop()
