import base64
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from analysis import (
    _compute_section_boundaries,
    analyze_portrait_motion,
    detect_change_points,
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
from constants import (
    ACCENT,
    BORDER,
    CLIP_CACHE_DIR_NAME,
    DARK_BG,
    ERROR_COL,
    MUSIC_EXTENSIONS,
    PANEL_BG,
    SETTINGS_FILE,
    SUBTLE,
    SUCCESS,
    TEXT,
    VIDEO_EXTENSIONS,
)
from ffmpeg_utils import check_ffmpeg, get_video_info
from overlay import apply_countdown_overlay, build_countdown_events
from video import concat_clips, process_clip, xfade_concat

_APP_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAAAF7ElEQVR4nO3dW2ojVxSGUanpYQjh"
    "eZj4MZNoMrxMIo9uPI1ghN8yCAcbbHyTXKU6pXP513psgk8h2J/2UXzZbhqx2+0faz8DXMrDw2G7"
    "aUC1hzDwUD8IFz3U0ENbMVj9IEMP7cZgtS9u8KH9EBT/ogYf+gnBj5JfzPDDukrPWJGaGHzocxtY"
    "vAEYfqijxOwtCoDhh7qWzuBZK4TBhzGuBLM3AMMPbTpnNmcFwPBD2+bO6OQAwPBDH+bMaocfgABg"
    "AgDBBACCCQAEEwAIJgAQTAAgmABAsJ+bztzc3NR+BDjp9vZ20wsbAAQTAAjW3RXgrb//+7fMF/rn"
    "blPcn9flv6bnLKvgc/769demRzYACCYAEEwAIJgAQDABgGACAMEEAIIJAAQTAAgmABBMACCYAEAw"
    "AYBgAgDBBACCCQAEEwAIJgAQTAAgmABAMAGAYAIAwQQAgm13u/3jpiP+NBiru/WnwYAeCAAE6/pP"
    "g93f39d+BHh2dXW16ZENAIIJAAQTAAgmABBMACCYAEAwAYBgAgDBBACCCQAEEwAIJgAQTAAg"
    "mABAMAGAYAIAwQQAggkABBMACCYAEEwAIJgAQDABgGACAMEEAIIJAAQTAAgmABBMACCYAEAw"
    "AYBgAgDBBGBAd3e/az8CnRCAgSMgBHxHAAYnApwiAAFsAxwjAEGEgI8EIJAI8EIAQtkGeCIA4YQg"
    "mwDwTAQyCQCvbAN5BIBPhCCHAHCUCIxPADjJNjA2AWASIRiTADCLCIxFAJjNNjAOAeBsQtA/AW"
    "AuJQA+fWMeJQA+fWMeJQA+fWMeAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAAAAAAAAAAAAAKAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABJRu5ErkJggg=="
)


class VideoMixer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Video Mixer")
        self.resizable(True, True)
        self.configure(bg=DARK_BG)
        self.update_idletasks()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{sw}x{sh}+0+0")
        self.state("zoomed")
        try:
            icon = tk.PhotoImage(data=_APP_ICON_B64)
            self.wm_iconphoto(True, icon)
        except Exception:
            pass

        if sys.platform == "darwin":
            try:
                import tempfile
                from AppKit import NSApplication, NSImage
                _icon_bytes = base64.b64decode(_APP_ICON_B64)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as _f:
                    _f.write(_icon_bytes)
                    _tmp = _f.name
                _img = NSImage.alloc().initWithContentsOfFile_(_tmp)
                NSApplication.sharedApplication().setApplicationIconImage_(_img)
                os.unlink(_tmp)
            except Exception:
                pass

        self._cancel = threading.Event()
        self._running = False

        self._setup_scroll()
        self._build()
        self._load_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._ffmpeg_ok = check_ffmpeg()
        if not self._ffmpeg_ok:
            self._log("⚠  ffmpeg / ffprobe not found.", color=ERROR_COL)
            self._log("   Install with:  brew install ffmpeg", color=SUBTLE)
            self._log("   Then restart this app.", color=SUBTLE)
            self.run_btn.configure_btn(state="disabled")
            self.generate_btn.configure_btn(state="disabled")

    # ── Scroll infrastructure ─────────────────────────────────────────────────

    def _setup_scroll(self):
        self._canvas = tk.Canvas(self, bg=DARK_BG, highlightthickness=0)
        self._vsb = tk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self._vsb.pack(side="right", fill="y")
        self._canvas.pack(side="left", fill="both", expand=True)
        self._body = tk.Frame(self._canvas, bg=DARK_BG)
        self._body_id = self._canvas.create_window((0, 0), window=self._body, anchor="nw")
        self._canvas.configure(yscrollcommand=self._vsb.set)
        self._body.bind("<Configure>", lambda _: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.bind("<Configure>", lambda e: self._canvas.itemconfig(
            self._body_id, width=e.width))
        self._canvas.bind_all("<MouseWheel>", lambda e: self._canvas.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

    # ── UI construction ───────────────────────────────────────────────────────

    def _build(self):
        # ── Header ──
        hdr = tk.Frame(self._body, bg=DARK_BG)
        hdr.pack(fill="x", padx=20, pady=(18, 4))
        tk.Label(hdr, text="🎬", font=("SF Pro", 26), bg=DARK_BG, fg=TEXT).pack(side="left")
        tk.Label(hdr, text=" Video Mixer", font=("SF Pro Display", 22, "bold"),
                 bg=DARK_BG, fg=TEXT).pack(side="left")

        tk.Label(self._body,
                 text="Combines landscape & portrait clips into one landscape video.",
                 font=("SF Pro", 11), bg=DARK_BG, fg=SUBTLE).pack(anchor="w", padx=24, pady=(0, 10))

        self._sep()

        # ── Paths ──
        self._section("Source & Destination")
        self.input_var = self._path_row("Input folder", browse_dir=True)
        self.output_var = self._path_row(
            "Output file",
            default=str(Path.home() / "Desktop" / "mixed_video.mp4"),
            browse_dir=False,
        )
        self.music_var = self._path_row("Music folder", hint="(optional)", browse_dir=True)

        self._sep()

        # ── Video Settings ──
        self._section("Settings")
        settings = tk.Frame(self._body, bg=DARK_BG)
        settings.pack(fill="x", padx=24)

        self._inline_label(settings, "Target duration", row=0)
        self.dur_var = tk.StringVar(value="60")
        self._num_entry(settings, self.dur_var, row=0, unit="seconds")

        self.use_max_var = tk.BooleanVar(value=True)
        self._checkbox(settings, "Max clip length", self.use_max_var, row=1,
                       on_toggle=self._toggle_max)
        self.max_clip_var = tk.StringVar(value="10")
        self.max_clip_entry = self._num_entry(settings, self.max_clip_var, row=1, unit="seconds",
                                              col_offset=2)

        self.use_fade_var = tk.BooleanVar(value=True)
        self._checkbox(settings, "Cross-fade duration", self.use_fade_var, row=2,
                       on_toggle=self._toggle_fade)
        self.fade_dur_var = tk.StringVar(value="0.5")
        self.fade_dur_entry = self._num_entry(settings, self.fade_dur_var, row=2, unit="seconds",
                                              col_offset=2)

        self.use_seed_var = tk.BooleanVar(value=False)
        self._checkbox(settings, "Fixed random seed", self.use_seed_var, row=3,
                       on_toggle=self._toggle_seed)
        self.seed_var = tk.StringVar(value="42")
        self.seed_entry = self._num_entry(settings, self.seed_var, row=3, unit="",
                                          col_offset=2, state="disabled")

        self._sep()

        # ── Music ──
        self._section("Music")
        music = tk.Frame(self._body, bg=DARK_BG)
        music.pack(fill="x", padx=24)

        self._inline_label(music, "Music volume", row=0)
        self.music_vol_var = tk.StringVar(value="30")
        self._num_entry(music, self.music_vol_var, row=0, unit="%")

        self.beat_sync_var = tk.BooleanVar(value=True)
        self._checkbox(music, "Beat-sync clip cuts", self.beat_sync_var, row=1)

        self._inline_label(music, "Beats per clip", row=2)
        self.beats_per_clip_var = tk.StringVar(value="8")
        self._num_entry(music, self.beats_per_clip_var, row=2, unit="beats")

        self._sep()

        # ── Countdown Overlay ──
        self._section("Countdown Overlay")
        self._countdown_widgets = []

        cdown_outer = tk.Frame(self._body, bg=DARK_BG)
        cdown_outer.pack(fill="x", padx=24)

        self.use_countdown_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            cdown_outer, text="Enable countdown overlay",
            variable=self.use_countdown_var,
            font=("SF Pro", 12), bg=DARK_BG, fg=TEXT,
            activebackground=DARK_BG, activeforeground=TEXT,
            selectcolor=PANEL_BG, relief="flat", bd=0, anchor="w",
            command=self._toggle_countdown,
        ).grid(row=0, column=0, columnspan=6, sticky="w", pady=4)

        tk.Label(cdown_outer, text="Corner", font=("SF Pro", 12),
                 bg=DARK_BG, fg=TEXT, width=14, anchor="w",
                 ).grid(row=1, column=0, sticky="w", pady=4)
        self.countdown_corner_var = tk.StringVar(value="top-right")
        corner_cb = ttk.Combobox(
            cdown_outer, textvariable=self.countdown_corner_var,
            values=["top-left", "top-right", "bottom-left", "bottom-right"],
            state="disabled", width=13, font=("SF Pro", 11),
        )
        corner_cb.grid(row=1, column=1, columnspan=2, sticky="w", pady=4, padx=(0, 20))
        self._countdown_widgets.append(corner_cb)

        tk.Label(cdown_outer, text="Countdown", font=("SF Pro", 12),
                 bg=DARK_BG, fg=TEXT, anchor="w",
                 ).grid(row=1, column=3, sticky="w", pady=4)
        self.countdown_dur_var = tk.StringVar(value="5")
        cd_e = tk.Entry(
            cdown_outer, textvariable=self.countdown_dur_var, font=("SF Pro", 12),
            width=6, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=6, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled",
        )
        cd_e.grid(row=1, column=4, sticky="w", pady=4)
        tk.Label(cdown_outer, text="  sec", font=("SF Pro", 11),
                 bg=DARK_BG, fg=SUBTLE).grid(row=1, column=5, sticky="w")
        self._countdown_widgets.append(cd_e)

        tk.Label(cdown_outer, text="Every", font=("SF Pro", 12),
                 bg=DARK_BG, fg=TEXT, width=14, anchor="w",
                 ).grid(row=2, column=0, sticky="w", pady=4)
        self.countdown_ivmin_var = tk.StringVar(value="50")
        ivmin_e = tk.Entry(
            cdown_outer, textvariable=self.countdown_ivmin_var, font=("SF Pro", 12),
            width=6, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=6, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled",
        )
        ivmin_e.grid(row=2, column=1, sticky="w", pady=4)
        tk.Label(cdown_outer, text="  to", font=("SF Pro", 11),
                 bg=DARK_BG, fg=SUBTLE).grid(row=2, column=2, sticky="w")
        self.countdown_ivmax_var = tk.StringVar(value="60")
        ivmax_e = tk.Entry(
            cdown_outer, textvariable=self.countdown_ivmax_var, font=("SF Pro", 12),
            width=6, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=6, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled",
        )
        ivmax_e.grid(row=2, column=3, sticky="w", pady=4, padx=(12, 0))
        tk.Label(cdown_outer, text="  sec", font=("SF Pro", 11),
                 bg=DARK_BG, fg=SUBTLE).grid(row=2, column=4, sticky="w")
        self._countdown_widgets += [ivmin_e, ivmax_e]

        tk.Label(cdown_outer, text="Text after  (empty=skip)", font=("SF Pro", 12),
                 bg=DARK_BG, fg=TEXT, width=22, anchor="w",
                 ).grid(row=3, column=0, sticky="w", pady=4)
        self.countdown_text1_var = tk.StringVar(value="HOLD")
        t1_e = tk.Entry(
            cdown_outer, textvariable=self.countdown_text1_var, font=("SF Pro", 12),
            width=10, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=6, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled",
        )
        t1_e.grid(row=3, column=1, columnspan=2, sticky="w", pady=4)
        tk.Label(cdown_outer, text="  for", font=("SF Pro", 11),
                 bg=DARK_BG, fg=SUBTLE).grid(row=3, column=3, sticky="w", padx=(4, 0))
        self.countdown_text1_dur_var = tk.StringVar(value="7")
        t1d_e = tk.Entry(
            cdown_outer, textvariable=self.countdown_text1_dur_var, font=("SF Pro", 12),
            width=6, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=6, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled",
        )
        t1d_e.grid(row=3, column=4, sticky="w", pady=4)
        tk.Label(cdown_outer, text="  sec", font=("SF Pro", 11),
                 bg=DARK_BG, fg=SUBTLE).grid(row=3, column=5, sticky="w")
        self._countdown_widgets += [t1_e, t1d_e]

        tk.Label(cdown_outer, text="Second text  (empty=skip)", font=("SF Pro", 12),
                 bg=DARK_BG, fg=TEXT, width=22, anchor="w",
                 ).grid(row=4, column=0, sticky="w", pady=4)
        self.countdown_text2_var = tk.StringVar(value="RELEASE")
        t2_e = tk.Entry(
            cdown_outer, textvariable=self.countdown_text2_var, font=("SF Pro", 12),
            width=10, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=6, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled",
        )
        t2_e.grid(row=4, column=1, columnspan=2, sticky="w", pady=4)
        tk.Label(cdown_outer, text="  for", font=("SF Pro", 11),
                 bg=DARK_BG, fg=SUBTLE).grid(row=4, column=3, sticky="w", padx=(4, 0))
        self.countdown_text2_dur_var = tk.StringVar(value="4")
        t2d_e = tk.Entry(
            cdown_outer, textvariable=self.countdown_text2_dur_var, font=("SF Pro", 12),
            width=6, bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=6, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, state="disabled",
        )
        t2d_e.grid(row=4, column=4, sticky="w", pady=4)
        tk.Label(cdown_outer, text="  sec", font=("SF Pro", 11),
                 bg=DARK_BG, fg=SUBTLE).grid(row=4, column=5, sticky="w")
        self._countdown_widgets += [t2_e, t2d_e]

        self.countdown_sync_var = tk.BooleanVar(value=True)
        sync_cb = tk.Checkbutton(
            cdown_outer, text="Sync to music change points",
            variable=self.countdown_sync_var,
            font=("SF Pro", 12), bg=DARK_BG, fg=TEXT,
            activebackground=DARK_BG, activeforeground=TEXT,
            selectcolor=PANEL_BG, relief="flat", bd=0, anchor="w",
            state="disabled",
        )
        sync_cb.grid(row=5, column=0, columnspan=6, sticky="w", pady=4)
        self._countdown_widgets.append(sync_cb)

        self._sep()

        # ── Log ──
        self._section("Log")
        log_frame = tk.Frame(self._body, bg=PANEL_BG, highlightbackground=BORDER,
                             highlightthickness=1, bd=0)
        log_frame.pack(fill="x", padx=24)

        self.log = tk.Text(log_frame, height=9, font=("Menlo", 11),
                           bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
                           selectbackground=ACCENT, relief="flat", bd=8,
                           state="disabled", wrap="word")
        sb = tk.Scrollbar(log_frame, command=self.log.yview, bg=PANEL_BG,
                          troughcolor=PANEL_BG, relief="flat")
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_configure("accent",  foreground=ACCENT)
        self.log.tag_configure("success", foreground=SUCCESS)
        self.log.tag_configure("error",   foreground=ERROR_COL)
        self.log.tag_configure("subtle",  foreground=SUBTLE)

        # ── Progress ──
        prog_frame = tk.Frame(self._body, bg=DARK_BG)
        prog_frame.pack(fill="x", padx=24, pady=(10, 0))

        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("Dark.Horizontal.TProgressbar",
                        troughcolor=PANEL_BG, background=ACCENT,
                        bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)
        self.progress = ttk.Progressbar(prog_frame, style="Dark.Horizontal.TProgressbar",
                                        mode="determinate", length=560)
        self.progress.pack(side="left", expand=True, fill="x")

        self.status_var = tk.StringVar(value="Ready")
        tk.Label(prog_frame, textvariable=self.status_var, font=("SF Pro", 11),
                 bg=DARK_BG, fg=SUBTLE, width=14, anchor="e").pack(side="right")

        # ── Buttons ──
        btn_row = tk.Frame(self._body, bg=DARK_BG)
        btn_row.pack(fill="x", padx=24, pady=(10, 8))

        self._btn(btn_row, "Clear Log", self._clear_log,
                  fg=SUBTLE).pack(side="left")

        self.cancel_btn = self._btn(btn_row, "Cancel", self._cancel_run,
                                    fg=SUBTLE, state="disabled")
        self.cancel_btn.pack(side="left", padx=(8, 0))

        self.generate_btn = self._btn(btn_row, "  Generate Video  ", self._generate_run,
                                      bg=ACCENT, fg="white", hover_bg="#0071e3",
                                      font=("SF Pro", 13, "bold"), padx=18, pady=8,
                                      state="disabled")
        self.generate_btn.pack(side="right")

        self.run_btn = self._btn(btn_row, "  Plan Video  ", self._plan_run,
                                 bg="#636366", fg="white", hover_bg="#48484a",
                                 font=("SF Pro", 13, "bold"), padx=18, pady=8)
        self.run_btn.pack(side="right", padx=(0, 8))

        # ── Plan / Instruction Set editor ──
        self._sep()
        plan_hdr = tk.Frame(self._body, bg=DARK_BG)
        plan_hdr.pack(fill="x", padx=24, pady=(0, 6))
        tk.Label(plan_hdr, text="Instruction Set", font=("SF Pro", 11, "bold"),
                 bg=DARK_BG, fg=SUBTLE).pack(side="left")
        self._btn(plan_hdr, "Load…", self._load_plan,
                  font=("SF Pro", 11), padx=10, pady=3).pack(side="right")
        self._btn(plan_hdr, "Save…", self._save_plan,
                  font=("SF Pro", 11), padx=10, pady=3).pack(side="right", padx=(0, 8))

        plan_frame = tk.Frame(self._body, bg=PANEL_BG, highlightbackground=BORDER,
                              highlightthickness=1, bd=0)
        plan_frame.pack(fill="x", padx=24, pady=(0, 24))

        self.plan_text = tk.Text(plan_frame, height=14, font=("Menlo", 11),
                                 bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
                                 selectbackground=ACCENT, relief="flat", bd=8,
                                 wrap="none")
        plan_sb_y = tk.Scrollbar(plan_frame, command=self.plan_text.yview,
                                 bg=PANEL_BG, troughcolor=PANEL_BG, relief="flat")
        plan_sb_x = tk.Scrollbar(plan_frame, orient="horizontal",
                                 command=self.plan_text.xview,
                                 bg=PANEL_BG, troughcolor=PANEL_BG, relief="flat")
        self.plan_text.configure(yscrollcommand=plan_sb_y.set,
                                 xscrollcommand=plan_sb_x.set)
        plan_sb_y.pack(side="right", fill="y")
        plan_sb_x.pack(side="bottom", fill="x")
        self.plan_text.pack(side="left", fill="both", expand=True)

    # ── Helper widget builders ────────────────────────────────────────────────

    def _sep(self):
        tk.Frame(self._body, height=1, bg=BORDER).pack(fill="x", padx=20, pady=10)

    def _section(self, text):
        tk.Label(self._body, text=text, font=("SF Pro", 11, "bold"),
                 bg=DARK_BG, fg=SUBTLE).pack(anchor="w", padx=24, pady=(2, 6))

    def _path_row(self, label, default="", browse_dir=True, hint=""):
        row = tk.Frame(self._body, bg=DARK_BG)
        row.pack(fill="x", padx=24, pady=3)
        lbl = f"{label}"
        if hint:
            lbl += f"  {hint}"
        tk.Label(row, text=lbl, font=("SF Pro", 12), bg=DARK_BG, fg=TEXT if not hint else SUBTLE,
                 width=18, anchor="w").pack(side="left")
        var = tk.StringVar(value=default)
        ent = tk.Entry(row, textvariable=var, font=("SF Pro", 11),
                       bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
                       relief="flat", bd=6, highlightthickness=1,
                       highlightbackground=BORDER, highlightcolor=ACCENT)
        ent.pack(side="left", fill="x", expand=True, padx=(0, 8))

        def browse():
            if browse_dir:
                path = filedialog.askdirectory(title=f"Select {label}")
            else:
                path = filedialog.asksaveasfilename(
                    title="Save output video",
                    defaultextension=".mp4",
                    filetypes=[("MP4 Video", "*.mp4"), ("All Files", "*.*")],
                )
            if path:
                var.set(path)

        self._btn(row, "Browse…", browse, font=("SF Pro", 11),
                  padx=10, pady=4).pack(side="right")
        return var

    def _btn(self, parent, text, command, bg=None, fg=TEXT,
             hover_bg=None, font=("SF Pro", 12), padx=14, pady=6, state="normal"):
        """tk.Button ignores bg/fg on macOS Aqua; use Label instead."""
        if bg is None:
            bg = PANEL_BG
        if hover_bg is None:
            hover_bg = BORDER
        lbl = tk.Label(parent, text=text, font=font, bg=bg, fg=fg,
                       padx=padx, pady=pady, cursor="hand2")
        lbl._btn_bg = bg
        lbl._btn_hover = hover_bg
        lbl._btn_fg = fg
        lbl._btn_cmd = command
        lbl._btn_state = state

        def _on_enter(_):
            if lbl._btn_state == "normal":
                lbl.configure(bg=lbl._btn_hover)
        def _on_leave(_):
            if lbl._btn_state == "normal":
                lbl.configure(bg=lbl._btn_bg)
        def _on_click(_):
            if lbl._btn_state == "normal":
                lbl._btn_cmd()

        lbl.bind("<Enter>", _on_enter)
        lbl.bind("<Leave>", _on_leave)
        lbl.bind("<Button-1>", _on_click)

        def _configure(**kw):
            if "state" in kw:
                lbl._btn_state = kw.pop("state")
                if lbl._btn_state == "disabled":
                    lbl.configure(fg=SUBTLE, bg=lbl._btn_bg, cursor="")
                else:
                    lbl.configure(fg=lbl._btn_fg, cursor="hand2")
            if kw:
                lbl.configure(**kw)

        lbl.configure_btn = _configure
        return lbl

    def _inline_label(self, parent, text, row):
        tk.Label(parent, text=text, font=("SF Pro", 12), bg=DARK_BG, fg=TEXT,
                 width=20, anchor="w").grid(row=row, column=0, sticky="w", pady=4)

    def _checkbox(self, parent, text, var, row, on_toggle=None):
        tk.Checkbutton(parent, text=text, variable=var,
                       font=("SF Pro", 12), bg=DARK_BG, fg=TEXT,
                       activebackground=DARK_BG, activeforeground=TEXT,
                       selectcolor=PANEL_BG, relief="flat", bd=0,
                       width=20, anchor="w",
                       command=on_toggle).grid(row=row, column=0, sticky="w", pady=4)

    def _num_entry(self, parent, var, row, unit="", col_offset=1, state="normal"):
        f = tk.Frame(parent, bg=DARK_BG)
        f.grid(row=row, column=col_offset, sticky="w", pady=4, padx=(0, 10))
        ent = tk.Entry(f, textvariable=var, font=("SF Pro", 12), width=7,
                       bg=PANEL_BG, fg=TEXT, insertbackground=TEXT,
                       relief="flat", bd=6, highlightthickness=1,
                       highlightbackground=BORDER, highlightcolor=ACCENT,
                       state=state)
        ent.pack(side="left")
        if unit:
            tk.Label(f, text=f"  {unit}", font=("SF Pro", 11),
                     bg=DARK_BG, fg=SUBTLE).pack(side="left")
        return ent

    # ── Toggles ──────────────────────────────────────────────────────────────

    def _toggle_max(self):
        self.max_clip_entry.configure(state="normal" if self.use_max_var.get() else "disabled")

    def _toggle_fade(self):
        self.fade_dur_entry.configure(state="normal" if self.use_fade_var.get() else "disabled")

    def _toggle_seed(self):
        self.seed_entry.configure(state="normal" if self.use_seed_var.get() else "disabled")

    def _toggle_countdown(self):
        enabled = self.use_countdown_var.get()
        for w in self._countdown_widgets:
            if isinstance(w, ttk.Combobox):
                w.configure(state="readonly" if enabled else "disabled")
            else:
                w.configure(state="normal" if enabled else "disabled")

    # ── Settings persistence ──────────────────────────────────────────────────

    def _load_settings(self):
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            return
        for key, var in [
            ("input", self.input_var), ("output", self.output_var),
            ("music", self.music_var), ("duration", self.dur_var),
            ("max_clip", self.max_clip_var), ("fade_dur", self.fade_dur_var),
            ("seed", self.seed_var), ("music_vol", self.music_vol_var),
            ("beats_per_clip", self.beats_per_clip_var),
            ("cd_dur", self.countdown_dur_var), ("cd_ivmin", self.countdown_ivmin_var),
            ("cd_ivmax", self.countdown_ivmax_var), ("cd_text1", self.countdown_text1_var),
            ("cd_text1_dur", self.countdown_text1_dur_var), ("cd_text2", self.countdown_text2_var),
            ("cd_text2_dur", self.countdown_text2_dur_var),
            ("cd_corner", self.countdown_corner_var),
        ]:
            if key in data:
                var.set(data[key])
        for key, var, toggle in [
            ("use_max", self.use_max_var, self._toggle_max),
            ("use_fade", self.use_fade_var, self._toggle_fade),
            ("use_seed", self.use_seed_var, self._toggle_seed),
            ("use_countdown", self.use_countdown_var, self._toggle_countdown),
        ]:
            if key in data:
                var.set(data[key])
                toggle()
        if "beat_sync" in data:
            self.beat_sync_var.set(data["beat_sync"])
        if "cd_sync" in data:
            self.countdown_sync_var.set(data["cd_sync"])

    def _save_settings(self):
        data = {
            "input":          self.input_var.get(),
            "output":         self.output_var.get(),
            "music":          self.music_var.get(),
            "duration":       self.dur_var.get(),
            "use_max":        self.use_max_var.get(),
            "max_clip":       self.max_clip_var.get(),
            "use_fade":       self.use_fade_var.get(),
            "fade_dur":       self.fade_dur_var.get(),
            "use_seed":       self.use_seed_var.get(),
            "seed":           self.seed_var.get(),
            "music_vol":      self.music_vol_var.get(),
            "beat_sync":      self.beat_sync_var.get(),
            "beats_per_clip": self.beats_per_clip_var.get(),
            "use_countdown":  self.use_countdown_var.get(),
            "cd_corner":      self.countdown_corner_var.get(),
            "cd_dur":         self.countdown_dur_var.get(),
            "cd_ivmin":       self.countdown_ivmin_var.get(),
            "cd_ivmax":       self.countdown_ivmax_var.get(),
            "cd_text1":       self.countdown_text1_var.get(),
            "cd_text1_dur":   self.countdown_text1_dur_var.get(),
            "cd_text2":       self.countdown_text2_var.get(),
            "cd_text2_dur":   self.countdown_text2_dur_var.get(),
            "cd_sync":        self.countdown_sync_var.get(),
        }
        try:
            SETTINGS_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def _on_close(self):
        self._save_settings()
        self.destroy()

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log(self, msg, color=None):
        def _write():
            self.log.configure(state="normal")
            tag = {ACCENT: "accent", SUCCESS: "success", ERROR_COL: "error",
                   SUBTLE: "subtle"}.get(color)
            if tag:
                self.log.insert("end", msg + "\n", tag)
            else:
                self.log.insert("end", msg + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _write)

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _status(self, msg):
        self.after(0, self.status_var.set, msg)

    def _prog(self, val):
        self.after(0, self.progress.configure, {"value": val})

    # ── Cancel ────────────────────────────────────────────────────────────────

    def _cancel_run(self):
        if self._running:
            self._cancel.set()
            self._log("⏹  Cancelling…", color=SUBTLE)
            self._status("Cancelling…")

    # ── Validation & launch ───────────────────────────────────────────────────

    def _plan_run(self):
        if self._running:
            return

        folder = self.input_var.get().strip()
        output = self.output_var.get().strip()

        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Missing input", "Please choose a valid input folder.")
            return
        if not output:
            messagebox.showerror("Missing output", "Please specify an output file path.")
            return

        try:
            target = float(self.dur_var.get())
            assert target > 0
        except Exception:
            messagebox.showerror("Invalid value", "Target duration must be a positive number.")
            return

        max_clip = None
        if self.use_max_var.get():
            try:
                max_clip = float(self.max_clip_var.get())
                assert max_clip > 0
            except Exception:
                messagebox.showerror("Invalid value", "Max clip length must be a positive number.")
                return

        fade_dur = 0.0
        if self.use_fade_var.get():
            try:
                fade_dur = float(self.fade_dur_var.get())
                assert fade_dur > 0
            except Exception:
                messagebox.showerror("Invalid value", "Cross-fade duration must be a positive number.")
                return

        seed = None
        if self.use_seed_var.get():
            try:
                seed = int(self.seed_var.get())
            except Exception:
                messagebox.showerror("Invalid value", "Seed must be an integer.")
                return

        music_folder = self.music_var.get().strip()
        if music_folder and not os.path.isdir(music_folder):
            messagebox.showerror("Invalid music folder", "The music folder path is not a valid directory.")
            return

        try:
            music_vol = float(self.music_vol_var.get()) / 100.0
            assert 0 <= music_vol <= 2
        except Exception:
            messagebox.showerror("Invalid value", "Music volume must be a number (0–200).")
            return

        try:
            beats_per_clip = int(self.beats_per_clip_var.get())
            assert beats_per_clip >= 1
        except Exception:
            messagebox.showerror("Invalid value", "Beats per clip must be a positive integer.")
            return

        beat_sync = self.beat_sync_var.get()

        countdown_cfg = None
        if self.use_countdown_var.get():
            try:
                cd_dur = float(self.countdown_dur_var.get())
                assert cd_dur > 0
            except Exception:
                messagebox.showerror("Invalid value", "Countdown duration must be a positive number.")
                return
            try:
                cd_ivmin = float(self.countdown_ivmin_var.get())
                cd_ivmax = float(self.countdown_ivmax_var.get())
                assert 0 < cd_ivmin <= cd_ivmax
            except Exception:
                messagebox.showerror("Invalid value", "Countdown interval must be positive with min ≤ max.")
                return
            text1 = self.countdown_text1_var.get().strip()
            text2 = self.countdown_text2_var.get().strip()
            try:
                text1_dur = float(self.countdown_text1_dur_var.get()) if text1 else 0.0
                text2_dur = float(self.countdown_text2_dur_var.get()) if text2 else 0.0
                assert (not text1 or text1_dur > 0) and (not text2 or text2_dur > 0)
            except Exception:
                messagebox.showerror("Invalid value", "Text durations must be positive numbers.")
                return
            countdown_cfg = {
                "dur": cd_dur, "iv_min": cd_ivmin, "iv_max": cd_ivmax,
                "corner": self.countdown_corner_var.get(),
                "text1": text1, "text1_dur": text1_dur,
                "text2": text2, "text2_dur": text2_dur,
                "sync": self.countdown_sync_var.get(),
            }

        self._cancel.clear()
        self._running = True
        self.run_btn.configure_btn(state="disabled")
        self.generate_btn.configure_btn(state="disabled")
        self.cancel_btn.configure_btn(state="normal")
        self._clear_log()
        self._prog(0)

        t = threading.Thread(
            target=self._analyse_thread,
            args=(folder, output, target, max_clip, seed,
                  music_folder, music_vol, fade_dur, beat_sync, beats_per_clip,
                  countdown_cfg),
            daemon=True,
        )
        t.start()

    def _finish(self):
        self._running = False
        self.run_btn.configure_btn(state="normal")
        self.cancel_btn.configure_btn(state="disabled")

    def _finish_generate(self):
        self._running = False
        self.run_btn.configure_btn(state="normal")
        self.generate_btn.configure_btn(state="normal")
        self.cancel_btn.configure_btn(state="disabled")

    # ── Plan editor helpers ───────────────────────────────────────────────────

    def _set_plan(self, plan_dict):
        self.plan_text.delete("1.0", "end")
        self.plan_text.insert("1.0", json.dumps(plan_dict, indent=2))
        self._log("\n✓  Plan ready — review/edit below, then click Generate Video.",
                  color=SUCCESS)

    def _get_plan(self):
        text = self.plan_text.get("1.0", "end").strip()
        if not text:
            raise ValueError("No plan loaded. Click 'Plan Video' first.")
        return json.loads(text)

    def _save_plan(self):
        try:
            plan = self._get_plan()
        except Exception:
            messagebox.showerror("No plan", "Create a plan first by clicking 'Plan Video'.")
            return
        path = filedialog.asksaveasfilename(
            title="Save Instruction Set",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All Files", "*.*")],
        )
        if path:
            with open(path, "w") as fh:
                json.dump(plan, fh, indent=2)
            self._log(f"✓  Plan saved → {path}", color=SUCCESS)

    def _load_plan(self):
        path = filedialog.askopenfilename(
            title="Load Instruction Set",
            filetypes=[("JSON", "*.json"), ("All Files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path) as fh:
                plan = json.load(fh)
        except Exception as e:
            messagebox.showerror("Load failed", f"Could not parse plan file:\n{e}")
            return
        self._set_plan(plan)
        self.generate_btn.configure_btn(state="normal")

    # ── Generate from plan ────────────────────────────────────────────────────

    def _generate_run(self):
        if self._running:
            return
        try:
            plan = self._get_plan()
        except Exception as e:
            messagebox.showerror("Invalid plan", str(e))
            return

        self._cancel.clear()
        self._running = True
        self.run_btn.configure_btn(state="disabled")
        self.generate_btn.configure_btn(state="disabled")
        self.cancel_btn.configure_btn(state="normal")
        self._clear_log()
        self._prog(0)

        t = threading.Thread(target=self._generate_thread, args=(plan,), daemon=True)
        t.start()

    def _generate_thread(self, plan):
        try:
            self._do_generate(plan)
        except Exception as exc:
            self._log(f"❌  {exc}", color=ERROR_COL)
            self._status("Failed")
        finally:
            self.after(0, self._finish_generate)

    def _do_generate(self, plan):
        output = plan["output_path"]
        fade_dur = plan.get("crossfade_dur", 0.0)
        music_vol = plan.get("music_vol", 0.3)
        seed = plan.get("seed")
        clips_data = plan.get("clips", [])
        music_data = plan.get("music", [])
        countdown_cfg = plan.get("countdown")

        music_tracks = [(Path(m["path"]), m["duration"]) for m in music_data]
        total = sum(c["duration"] for c in clips_data)
        n = len(clips_data)

        self._log(f"Encoding {n} clip(s)…", color=ACCENT)

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
                if self._cancel.is_set():
                    _flush_motion_caches()
                    self._log("Cancelled.", color=SUBTLE)
                    self._status("Cancelled")
                    return

                self._prog(int(i / n * 80))
                self._status(f"Clip {i+1} / {n}")
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
                loud_tag = " [LOUD review]" if c.get("audio_review_loud") else ""
                self._log(
                    f"  [{i+1:>{len(str(n))}}/{n}] {vf.name}"
                    f"  {info['width']}×{info['height']} ({orient})"
                    f"  start={start:.1f}s  dur={dur:.1f}s{loud_tag}"
                )

                motion = None
                if orient == "portrait":
                    motion_data = c.get("motion")
                    if motion_data is not None:
                        motion = tuple(motion_data)
                        self._log(
                            f"    motion y={motion[0]:.2f} x={motion[1]:.2f}"
                            f" conf={motion[2]:.2f} (from plan)",
                            color=SUBTLE,
                        )
                    else:
                        folder_key = str(vf.parent)
                        cache_data = _get_motion_cache(vf.parent)
                        mcache = cache_data.setdefault("portrait_motion", {})
                        cache_key = f"{vf.name}:{start:.3f}:{dur:.3f}"
                        if cache_key in mcache and _fp_match(vf, mcache[cache_key]):
                            motion = tuple(mcache[cache_key]["motion"])
                            self._log(
                                f"    motion y={motion[0]:.2f} x={motion[1]:.2f}"
                                f" conf={motion[2]:.2f} (cached)",
                                color=SUBTLE,
                            )
                        else:
                            self._log("    Analysing portrait motion…", color=SUBTLE)
                            motion = analyze_portrait_motion(
                                vf, start, dur, info["width"], info["height"]
                            )
                            mtime, size = _fingerprint(vf)
                            mcache[cache_key] = {
                                "mtime": mtime, "size": size, "motion": list(motion),
                            }
                            _motion_dirty.add(folder_key)
                            self._log(
                                f"    motion y={motion[0]:.2f} x={motion[1]:.2f}"
                                f" conf={motion[2]:.2f}",
                                color=SUBTLE,
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
                    self._log("    (encoded clip cached)", color=SUBTLE)
                else:
                    ok = process_clip(vf, out_clip, start, dur, info, self._cancel, motion=motion)
                    if not ok:
                        _flush_motion_caches()
                        self._log("Cancelled.", color=SUBTLE)
                        self._status("Cancelled")
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
                self._log("\nPreparing music…", color=ACCENT)
                self._status("Preparing music…")
                self._prog(82)
                try:
                    music_path = prepare_music_audio(music_tracks, total, tmp_path)
                    if music_path is None:
                        self._log("  Warning: could not prepare music audio — skipping.",
                                  color=ERROR_COL)
                except Exception as e:
                    self._log(f"  Warning: music preparation failed: {e} — skipping.",
                              color=ERROR_COL)

            self._prog(85)
            use_xfade = fade_dur > 0 or music_path is not None

            if use_xfade:
                self._log("\nApplying cross-fades and mixing…", color=ACCENT)
                self._status("Mixing…")
                try:
                    xfade_concat(encoded, Path(output), fade_dur, music_path, music_vol,
                                 log_fn=lambda m: self._log(m, color=SUBTLE),
                                 cancel_event=self._cancel)
                except RuntimeError as exc:
                    if str(exc) == "Cancelled":
                        self._log("Cancelled.", color=SUBTLE)
                        self._status("Cancelled")
                        return
                    raise
            else:
                self._log("\nConcatenating…", color=ACCENT)
                self._status("Concatenating…")
                concat_clips(encoded, Path(output))

        if countdown_cfg:
            self._log("\nBuilding countdown overlay…", color=ACCENT)
            self._status("Countdown overlay…")
            self._prog(90)

            final_info = get_video_info(output)
            final_dur = final_info["duration"] if final_info else total

            cp = countdown_cfg.get("change_points") if countdown_cfg.get("sync") else None

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
                self._log(f"  {len(events)} overlay segment(s) planned", color=SUBTLE)
                overlay_tmp = Path(output).with_suffix(".overlay_tmp.mp4")
                ok = apply_countdown_overlay(
                    Path(output), overlay_tmp, events, countdown_cfg["corner"], self._cancel
                )
                if not ok:
                    self._log("Cancelled.", color=SUBTLE)
                    self._status("Cancelled")
                    overlay_tmp.unlink(missing_ok=True)
                    return
                overlay_tmp.replace(Path(output))
            else:
                self._log("  No countdown events fit within video duration — skipping.", color=SUBTLE)

        self._prog(100)
        self._log(f"\n✅  Done!  →  {output}", color=SUCCESS)
        self._status("Complete!")

        if sys.platform == "darwin":
            subprocess.run(["open", "-R", output], check=False)

    # ── Per-section clip builder ──────────────────────────────────────────────

    def _build_section_clips(self, pool, section_dur, rng, max_clip, beats, beats_per_clip, beat_idx, hcache=None):
        """
        Build clips from pool filling section_dur seconds.
        Continues beat_idx across calls so beat alignment is seamless across sections.
        Returns (clips, new_beat_idx) or (None, beat_idx) if cancelled.
        """
        clips = []
        total = 0.0
        pool_idx = 0

        if beats and len(beats) > beats_per_clip:
            cycle = 0
            while total < section_dur:
                if self._cancel.is_set():
                    return None, beat_idx
                if beat_idx + beats_per_clip >= len(beats):
                    beat_idx = 0
                    cycle += 1
                    if cycle > 20:
                        break
                beat_dur = beats[beat_idx + beats_per_clip] - beats[beat_idx]
                beat_idx += beats_per_clip

                if pool_idx >= len(pool):
                    pool_idx = 0
                    rng.shuffle(pool)
                vf, info = pool[pool_idx]
                pool_idx += 1

                raw_dur = info["duration"]
                if raw_dur >= beat_dur:
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
                if self._cancel.is_set():
                    return None, beat_idx
                if pool_idx >= len(pool):
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
                        self._log(f"  Analysing {vf.name} for highlights…", color=SUBTLE)
                        start = find_highlight_start(
                            vf, clip_dur, info,
                            log_fn=lambda m: self._log(m, color=SUBTLE),
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

    # ── Analysis (plan creation) ──────────────────────────────────────────────

    def _analyse_thread(self, folder, output, target_dur, max_clip, seed,
                        music_folder, music_vol, fade_dur, beat_sync, beats_per_clip,
                        countdown_cfg=None):
        try:
            plan = self._do_analyse(folder, output, target_dur, max_clip, seed,
                                    music_folder, music_vol, fade_dur, beat_sync,
                                    beats_per_clip, countdown_cfg)
            if plan is not None:
                self.after(0, lambda: self._set_plan(plan))
                self.after(0, lambda: self.generate_btn.configure_btn(state="normal"))
        except Exception as exc:
            self._log(f"❌  {exc}", color=ERROR_COL)
            self._status("Failed")
        finally:
            self.after(0, self._finish)

    def _do_analyse(self, folder, output, target_dur, max_clip, seed,
                    music_folder, music_vol, fade_dur, beat_sync, beats_per_clip,
                    countdown_cfg=None):
        # 1 — Collect videos
        self._log("Scanning folder for videos…", color=ACCENT)
        all_files = [
            f for f in Path(folder).iterdir()
            if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
        ]
        if all_files:
            self._log(f"  Found {len(all_files)} root-level file(s)")

        # 2 — Probe videos (with folder-level cache)
        self._log("Reading video metadata…", color=ACCENT)
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
                self._log(f"  ✓ {f.name}  {info['width']}×{info['height']} {orient}"
                          f"  {info['duration']:.1f}s{cached_tag}")
                pool.append((f, info))
            else:
                self._log(f"  ✗ {f.name}  (skipped — unreadable)", color=SUBTLE)
        if vcache_dirty:
            _save_cache(folder, vcache_data)

        _subfolders = sorted([
            d for d in Path(folder).iterdir()
            if d.is_dir() and not d.name.startswith(".")
            and any(f.suffix.lower() in VIDEO_EXTENSIONS for f in d.iterdir() if f.is_file())
        ])
        _subfolder_mode = len(_subfolders) >= 2

        if not pool and not _subfolder_mode:
            raise RuntimeError("No readable video files found.")

        # 3 — Collect & analyse music
        music_files = []
        music_tracks = []
        beats = None
        change_points = None
        if music_folder and os.path.isdir(music_folder):
            self._log("\nScanning music folder…", color=ACCENT)
            music_files = sorted([
                f for f in Path(music_folder).iterdir()
                if f.is_file() and f.suffix.lower() in MUSIC_EXTENSIONS
            ])
            self._log(f"  Found {len(music_files)} music file(s)")

            mcache_data = _load_cache(music_folder)
            acache = mcache_data.setdefault("audio_info", {})
            bcache = mcache_data.setdefault("beats", {})
            ecache = mcache_data.setdefault("energy", {})
            mcache_dirty = False

            n_music = len(music_files)
            for i, mf in enumerate(music_files, 1):
                self._status(f"Analysing music {i}/{n_music}…")
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
                self._log("Detecting beats…", color=ACCENT)
                first = music_files[0]
                key = first.name
                if key in bcache and _fp_match(first, bcache[key]):
                    beats = bcache[key]["beats"]
                    self._log(f"  {len(beats)} beats (cached)", color=SUBTLE)
                else:
                    beats = detect_beats(
                        first,
                        log_fn=lambda m: self._log(m, color=SUBTLE),
                    )
                    if beats is not None:
                        mtime, size = _fingerprint(first)
                        bcache[key] = {"mtime": mtime, "size": size, "beats": beats}
                        mcache_dirty = True

                if beats is None:
                    self._log("  Falling back to regular clip timing.", color=SUBTLE)

            need_change_points = (
                _subfolder_mode
                or (countdown_cfg and countdown_cfg["sync"])
            )
            if need_change_points and music_files:
                cpcache = mcache_data.setdefault("change_points", {})
                first = music_files[0]
                key = first.name
                if key in cpcache and _fp_match(first, cpcache[key]):
                    change_points = cpcache[key]["points"]
                    self._log(f"  {len(change_points)} change point(s) (cached)", color=SUBTLE)
                else:
                    change_points = detect_change_points(
                        first, log_fn=lambda m: self._log(m, color=SUBTLE)
                    )
                    if change_points is not None:
                        mtime, size = _fingerprint(first)
                        cpcache[key] = {"mtime": mtime, "size": size,
                                        "points": change_points}
                        mcache_dirty = True

            if mcache_dirty:
                _save_cache(music_folder, mcache_data)

        # 4 — Plan clip list
        rng = random.Random(seed)
        if music_tracks:
            rng.shuffle(music_tracks)

        if _subfolder_mode:
            self._log(f"\nSubfolder mode: {len(_subfolders)} folder(s) detected.", color=ACCENT)

            subfolder_pools = []
            for sf in _subfolders:
                sf_files = sorted([
                    f for f in sf.iterdir()
                    if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
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
                if sf_dirty:
                    _save_cache(sf, sf_cache_data)
                if sf_pool:
                    self._log(f"  {sf.name}: {len(sf_pool)} video(s)")
                    subfolder_pools.append((sf, sf_pool, sf_cache_data))
                else:
                    self._log(f"  {sf.name}: no usable videos — skipped", color=SUBTLE)

            if not subfolder_pools:
                raise RuntimeError("No usable videos found in any subfolder.")

            n_secs = len(subfolder_pools)
            section_durs, boundary_info = _compute_section_boundaries(
                n_secs, target_dur, change_points
            )

            if change_points and boundary_info:
                for i, (raw, snapped) in enumerate(boundary_info):
                    if abs(snapped - raw) > 1.0:
                        self._log(
                            f"  Boundary after section {i+1}: "
                            f"{raw:.0f}s → {snapped:.0f}s (music sync)",
                            color=SUBTLE,
                        )

            clips = []
            total = 0.0
            beat_idx = 0
            for (sf, sf_pool, sf_cache_data), sec_dur in zip(subfolder_pools, section_durs):
                sf_pool_copy = list(sf_pool)
                rng.shuffle(sf_pool_copy)
                self._log(f"\n  Section '{sf.name}' — target {sec_dur:.0f}s", color=ACCENT)
                sf_hcache = sf_cache_data.setdefault("highlights", {})
                sf_hcache_prev_len = len(sf_hcache)
                sec_clips, beat_idx = self._build_section_clips(
                    sf_pool_copy, sec_dur, rng, max_clip, beats, beats_per_clip, beat_idx,
                    hcache=sf_hcache,
                )
                if len(sf_hcache) > sf_hcache_prev_len:
                    _save_cache(sf, sf_cache_data)
                if sec_clips is None:
                    self._log("Cancelled.", color=SUBTLE)
                    self._status("Cancelled")
                    return None
                sec_total = sum(d for _, _, _, d in sec_clips)
                clips.extend(sec_clips)
                total += sec_total
                self._log(f"    → {len(sec_clips)} clip(s), {sec_total:.1f}s")

            self._log(f"\nTotal: {len(clips)} clip(s) → ~{total:.1f}s")

        else:
            rng.shuffle(pool)

            clips = []
            total = 0.0
            pool_idx = 0

            self._log(f"\nBuilding clip list (target: {target_dur:.0f}s)…", color=ACCENT)

            if beats and len(beats) > beats_per_clip:
                beat_idx = 0
                cycle = 0
                while total < target_dur:
                    if self._cancel.is_set():
                        return None
                    if beat_idx + beats_per_clip >= len(beats):
                        beat_idx = 0
                        cycle += 1
                        if cycle > 20:
                            break

                    beat_dur = beats[beat_idx + beats_per_clip] - beats[beat_idx]
                    beat_idx += beats_per_clip

                    if pool_idx >= len(pool):
                        pool_idx = 0
                        rng.shuffle(pool)
                    vf, info = pool[pool_idx]
                    pool_idx += 1

                    raw_dur = info["duration"]
                    if raw_dur >= beat_dur:
                        start = round(rng.uniform(0, raw_dur - beat_dur), 3)
                        clip_dur = beat_dur
                    else:
                        start = 0.0
                        clip_dur = raw_dur

                    clips.append((vf, info, start, clip_dur))
                    total += clip_dur

                self._log(f"  Beat-aligned: {len(clips)} clip(s) → ~{total:.1f}s total")
            else:
                cycle = 0
                hcache = vcache_data.setdefault("highlights", {})
                hcache_prev_len = len(hcache)
                while total < target_dur:
                    if self._cancel.is_set():
                        return None
                    if pool_idx >= len(pool):
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
                            self._log(f"  Analysing {vf.name} for highlights…", color=SUBTLE)
                            start = find_highlight_start(
                                vf, clip_dur, info,
                                log_fn=lambda m: self._log(m, color=SUBTLE),
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

                self._log(f"  Planned {len(clips)} clip(s) → ~{total:.1f}s total")

        # Portrait motion analysis
        portrait_clips = [
            (i, vf, info, start, dur)
            for i, (vf, info, start, dur) in enumerate(clips)
            if info["height"] > info["width"]
        ]
        motion_by_idx = {}
        if portrait_clips:
            self._log("\nAnalysing portrait motion…", color=ACCENT)
            folder_clips: dict = {}
            for item in portrait_clips:
                folder_clips.setdefault(str(item[1].parent), []).append(item)
            for folder_str, fclips in folder_clips.items():
                cache_data = _load_cache(folder_str)
                mcache = cache_data.setdefault("portrait_motion", {})
                dirty = False
                for i, vf, info, start, dur in fclips:
                    if self._cancel.is_set():
                        return None
                    cache_key = f"{vf.name}:{start:.3f}:{dur:.3f}"
                    if cache_key in mcache and _fp_match(vf, mcache[cache_key]):
                        motion = tuple(mcache[cache_key]["motion"])
                        self._log(
                            f"  {vf.name} y={motion[0]:.2f} x={motion[1]:.2f}"
                            f" conf={motion[2]:.2f} (cached)",
                            color=SUBTLE,
                        )
                    else:
                        self._log(f"  Analysing {vf.name}…", color=SUBTLE)
                        motion = analyze_portrait_motion(vf, start, dur, info["width"], info["height"])
                        mtime, size = _fingerprint(vf)
                        mcache[cache_key] = {"mtime": mtime, "size": size, "motion": list(motion)}
                        dirty = True
                        self._log(
                            f"    motion y={motion[0]:.2f} x={motion[1]:.2f}"
                            f" conf={motion[2]:.2f}",
                            color=SUBTLE,
                        )
                    motion_by_idx[i] = motion
                if dirty:
                    _save_cache(folder_str, cache_data)

        # Sort music chill→intense
        if music_tracks:
            selected, acc = [], 0.0
            for track in music_tracks:
                selected.append(track)
                acc += track[1]
                if acc >= total:
                    break
            selected.sort(key=lambda t: t[2] if t[2] is not None else float("-inf"))
            music_tracks = selected
            self._log("  Music order (chill → intense): " +
                      ", ".join(t[0].name for t in music_tracks), color=SUBTLE)

        plan = {
            "version": 1,
            "output_path": output,
            "crossfade_dur": fade_dur,
            "music_vol": music_vol,
            "seed": seed,
            "clips": [
                {
                    "path": str(vf),
                    "start": round(start, 3),
                    "duration": round(dur, 3),
                    "width": info["width"],
                    "height": info["height"],
                    "has_audio": info["has_audio"],
                    "motion": list(motion_by_idx[i]) if i in motion_by_idx else None,
                    "audio_review_loud": False,
                }
                for i, (vf, info, start, dur) in enumerate(clips)
            ],
            "music": [
                {"path": str(mf), "duration": round(dur, 3)}
                for mf, dur, *_ in music_tracks
            ],
            "countdown": None,
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

        self._prog(40)
        self._status("Plan ready")
        return plan
