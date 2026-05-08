from pathlib import Path

SETTINGS_FILE        = Path.home() / ".videomixer_settings.json"
CACHE_FILE           = ".videomixer_cache.json"
CLIP_CACHE_DIR_NAME  = ".videomixer_clip_cache"
CLIP_CACHE_MAX_AGE_S = 30 * 86400  # 30 days

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".wmv", ".flv", ".mts", ".m2ts"}
MUSIC_EXTENSIONS = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}

OUT_W, OUT_H = 1920, 1080
OUT_FPS = 30

FONT_CANDIDATES = [
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]

# GUI colour palette
DARK_BG   = "#1c1c1e"
PANEL_BG  = "#2c2c2e"
TEXT      = "#f5f5f7"
SUBTLE    = "#8e8e93"
ACCENT    = "#0a84ff"
SUCCESS   = "#30d158"
ERROR_COL = "#ff453a"
BORDER    = "#3a3a3c"
