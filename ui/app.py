"""ui/app.py - The main window: wires the panels, the project model, undo and the preview together."""

import math
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from lang import list_languages, resolve_language, set_language, t
from theme import ThemeDialog, apply_theme, list_themes, resolve_color, resolve_theme, set_theme

from engine import APP_NAME, VERSION
from engine import ffmpeg as ff
from engine.compiler import CompileError
from engine.config import push_recent, save_config
from engine.jobs import JobQueue, JobState
from engine.preview import PreviewEngine, save_frame
from engine.project import (PROJECT_EXT, AdjustmentLayer, EffectInstance, History, MediaLayer, Project, ProjectError,
                            even, load_project, save_project)
from ui.common import IS_WIN, Tip, fmt_stamp, fmt_time
from ui.dialogs import AboutDialog, ExportDialog, GotoDialog, ProjectDialog, SettingsDialog
from ui.jobs import JobsWindow
from ui.panels import EffectPanel, LayerPanel, PropertiesPanel

try:                                            # optional: pip install tkinterdnd2
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = TkinterDnD = None

MEDIA_TYPES = [("Video / image", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.mpg *.mpeg *.wmv *.flv *.gif "
                                 "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"), ("*", "*.*")]
IS_MAC = sys.platform == "darwin"


class App:
    """The program: one window, one project."""

    def __init__(self, cfg):
        """@param cfg  config dict (engine.config.load_config())"""
        self.cfg = cfg
        self.dnd_ok = False
        self.root = self._make_root()
        cfg["language"] = set_language(cfg.get("language"))
        cfg["theme"] = set_theme(cfg.get("theme"))
        apply_theme(self.root)                      # before any widget exists
        self.project, self.project_path = Project(), None
        self.history = History(self.project)
        self.saved_state = self.history.current
        self.sel_layer, self.sel_effect = None, None
        self._missing = set()                       # ids of the media layers whose file does not exist
        self.t, self._seq, self._job, self._img = 0.0, 0, None, None
        self._time_lock, self.compare = False, False
        self.zoom, self.cx, self.cy = None, 0.0, 0.0    # preview zoom: None = fit, else screen px per project px; cx, cy = centre in project px
        self._drag, self._view_req = None, None
        self._fit, self._shown, self._interim_img = None, None, None   # (image, x0, y0, scale) of the last fit / last shown picture
        self.inbox = queue.Queue()                  # functions from worker threads, run in the Tk thread
        self.tools = ff.locate(cfg["ffmpeg"])
        self.engine = PreviewEngine(self.tools, lambda seq, path, err: self.inbox.put(
            lambda: self._on_result(seq, path, err)), cfg["cache_dir"])
        self.jobs = JobQueue(cfg["workers"], cfg["worker_threads"],
                             lambda job: self.inbox.put(lambda: self._job_changed(job)))
        self.jobs_win = None
        self._build()
        self.update_title()
        self.root.after(40, self._poll)

    # ------------------------------------------------------------------ #
    # Window
    # ------------------------------------------------------------------ #

    def _make_root(self):
        """@return the Tk root; with tkinterdnd2 installed one that accepts dropped files"""
        if TkinterDnD:
            try:
                root = TkinterDnD.Tk()
                self.dnd_ok = True
                return root
            except (tk.TclError, RuntimeError):
                pass
        return tk.Tk()

    def _enable_drop(self, *widgets):
        """Let files be dropped on these widgets."""
        if not self.dnd_ok:
            return
        for widget in widgets:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", self._dropped)

    def _dropped(self, event):
        """A project opens (after asking about unsaved changes); media files become layers."""
        paths = [p for p in self.root.tk.splitlist(event.data) if os.path.isfile(p)]
        projects = [p for p in paths if os.path.splitext(p)[1].lower() == PROJECT_EXT]
        if projects:
            if self.confirm_discard():
                self.open_path(projects[0])
        elif paths:
            self.add_media(paths)
        return event.action

    def _build(self):
        root, cfg = self.root, self.cfg
        root.title(t("app.title", version=VERSION))
        root.geometry(cfg["geometry"])
        if cfg["zoomed"]:
            try:
                root.state("zoomed") if IS_WIN else root.attributes("-zoomed", True)
            except tk.TclError:
                pass
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.col_bg = resolve_color(root, "viewer", "canvas_bg") or "#101010"
        self.col_info = resolve_color(root, "viewer", "info_text") or "#e6e6e6"
        self.col_err = resolve_color(root, "viewer", "error_text") or "#ff6b6b"
        root.bind("<FocusIn>", self._focus_in)
        self._menus()
        panes = ttk.PanedWindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6, pady=6)
        self.layers = LayerPanel(panes, self)
        centre = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(self.layers, weight=0)
        panes.add(centre, weight=1)
        panes.add(right, weight=0)
        self.layers.configure(width=280)
        right.configure(width=330)

        self.canvas = tk.Canvas(centre, bg=self.col_bg, highlightthickness=0, width=480, height=300)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._view_configure)
        self.canvas.bind("<ButtonPress-1>", self._view_press)
        self.canvas.bind("<B1-Motion>", self._view_drag)
        self.canvas.bind("<ButtonRelease-1>", self._view_release)
        self.canvas.bind("<MouseWheel>", self._view_wheel)                  # Windows, macOS
        self.canvas.bind("<Button-4>", lambda e: self._view_wheel(e, 1))    # Linux
        self.canvas.bind("<Button-5>", lambda e: self._view_wheel(e, -1))
        self.canvas.bind("<Button-3>", self._view_menu)
        if IS_MAC:
            self.canvas.bind("<Button-2>", self._view_menu)                 # Tk on macOS: 2 = right button
        self.status = ttk.Label(centre, text="")
        self.status.pack(fill="x", pady=(4, 0))
        bar = ttk.Frame(centre)
        bar.pack(fill="x", pady=(4, 0))
        self.time_var = tk.DoubleVar(value=0.0)
        for text, tip, step in (("\u25C0", "tip.prev_frame", -1), ("\u25B6", "tip.next_frame", 1)):
            ttk.Button(bar, text=text, width=3, command=lambda s=step: self.step(s)).pack(side="left", padx=(0, 4))
        self.scale = ttk.Scale(bar, from_=0.0, to=1.0, variable=self.time_var, orient="horizontal",
                               command=self._scale_moved)
        self.scale.pack(side="left", fill="x", expand=True)
        self.time_label = ttk.Label(bar, text="", width=22, anchor="e")
        self.time_label.pack(side="left", padx=(8, 0))
        compare = ttk.Button(bar, text=t("btn.compare"))
        compare.pack(side="left", padx=(8, 0))
        Tip(compare, "tip.compare")
        compare.bind("<ButtonPress-1>", lambda e: self._compare(True))
        compare.bind("<ButtonRelease-1>", lambda e: self._compare(False))
        for widget in (self.canvas, self.scale):
            widget.bind("<Left>", lambda e: self.step(-1))
            widget.bind("<Right>", lambda e: self.step(1))
            widget.bind("<Shift-Left>", lambda e: self.step(-int(self.project.fps)))
            widget.bind("<Shift-Right>", lambda e: self.step(int(self.project.fps)))
            widget.bind("<Home>", lambda e: self.set_time(0.0))
            widget.bind("<End>", lambda e: self.set_time(self.project.duration()))

        self.props = PropertiesPanel(right, self)
        self.props.pack(fill="x")
        self.effects = EffectPanel(right, self)
        self.effects.pack(fill="x", pady=(8, 0))
        self._enable_drop(self.root, self.canvas, self.layers.tree, self.props, self.effects)
        self.refresh_all()
        self.update_time_range()
        if not self.tools:
            self.root.after(300, lambda: messagebox.showwarning(t("dlg.error"), t("msg.no_ffmpeg"), parent=self.root))

    def _menus(self):
        root = self.root
        bar = tk.Menu(root)
        mod = "Command" if IS_MAC else "Control"
        label = "Cmd" if IS_MAC else "Ctrl"

        def add(menu, key, command, char=None, shift=False):
            """Menu entry with its keyboard shortcut (Ctrl, or Cmd on macOS)."""
            accel = "%s+%s%s" % (label, "Shift+" if shift else "", char.upper()) if char else ""
            menu.add_command(label=t(key), command=command, accelerator=accel)
            if char:
                for c in ({char.upper()} if shift else {char.lower(), char.upper()}):
                    sym = {",": "comma"}.get(c, c)
                    root.bind("<%s-%s%s>" % (mod, "Shift-" if shift else "", sym), lambda e, f=command: self._key(f))

        file_menu = tk.Menu(bar, tearoff=0)
        add(file_menu, "menu.new", self.new_project, "n")
        add(file_menu, "menu.open", self.open_dialog, "o")
        self.recent_menu = tk.Menu(file_menu, tearoff=0)            # "Open recent" submenu, filled by _fill_recent()
        file_menu.add_cascade(label=t("menu.recent"), menu=self.recent_menu)
        add(file_menu, "menu.save", self.save, "s")
        add(file_menu, "menu.save_as", self.save_as, "s", True)
        file_menu.add_separator()
        add(file_menu, "menu.add_media", self.add_media_dialog, "i")
        add(file_menu, "menu.add_adjustment", self.add_adjustment, "l")
        file_menu.add_separator()
        add(file_menu, "menu.export", self.export_dialog, "e")
        add(file_menu, "menu.jobs", self.show_jobs, "q")
        add(file_menu, "menu.export_frame", self.export_frame_dialog, "e", True)
        file_menu.add_separator()
        add(file_menu, "menu.quit", self.quit, "w")
        edit = tk.Menu(bar, tearoff=0)
        add(edit, "menu.undo", self.undo, "z")
        add(edit, "menu.redo", self.redo, "y")
        root.bind("<%s-Shift-Z>" % mod, lambda e: self._key(self.redo))
        edit.add_separator()
        add(edit, "menu.project", lambda: self.project_settings())
        add(edit, "menu.duplicate", self.duplicate_layer, "d")
        edit.add_separator()
        add(edit, "menu.goto", self.goto_dialog, "g")
        bar.add_cascade(label=t("menu.file"), menu=file_menu)
        bar.add_cascade(label=t("menu.edit"), menu=edit)
        # root menu "Settings": the theme (one radio entry per theme, then Edit theme), then the settings dialog
        settings = tk.Menu(bar, tearoff=0)
        self.theme_menu = tk.Menu(settings, tearoff=0)              # filled by _fill_themes()
        self.theme_var = tk.StringVar(master=root, value=self.cfg["theme"])
        settings.add_cascade(label=t("menu.theme"), menu=self.theme_menu)
        add(settings, "menu.settings", lambda: SettingsDialog(self), ",")
        bar.add_cascade(label=t("menu.settings_menu"), menu=settings)
        # root menu "Language": one radio entry per language ("<English name> - <native name>")
        lang_menu = tk.Menu(bar, tearoff=0)
        self.lang_var = tk.StringVar(master=root, value=self.cfg["language"])
        for code, name in list_languages():
            lang_menu.add_radiobutton(label=name, variable=self.lang_var, value=code,
                                      command=lambda c=code: self.choose_language(c))
        bar.add_cascade(label=t("menu.language"), menu=lang_menu)
        # root menu "Help" comes after "Language" (the last one in the bar); it holds the About entry
        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label=t("menu.about_item"), command=lambda: AboutDialog(self))
        bar.add_cascade(label=t("menu.help"), menu=help_menu)
        root.config(menu=bar)
        self._fill_recent()
        self._fill_themes()
        # preview zoom, like Photoshop: Ctrl+0 fit, Ctrl+1 100%, Ctrl + / - step
        for sym, command in (("0", self.zoom_fit), ("1", lambda: self.zoom_to(1.0)),
                             ("plus", lambda: self.zoom_step(1)), ("equal", lambda: self.zoom_step(1)),
                             ("KP_Add", lambda: self.zoom_step(1)), ("minus", lambda: self.zoom_step(-1)),
                             ("KP_Subtract", lambda: self.zoom_step(-1))):
            root.bind("<%s-%s>" % (mod, sym), lambda e, f=command: self._key(f))

    def _fill_themes(self):
        """Rebuild the Settings > Theme submenu: the themes of theme/ as radio entries, then Edit theme."""
        menu = self.theme_menu
        menu.delete(0, "end")
        self.theme_var.set(self.cfg["theme"])
        for code, name in list_themes():
            menu.add_radiobutton(label=name, variable=self.theme_var, value=code,
                                 command=lambda c=code: self.choose_theme(c))
        menu.add_separator()
        menu.add_command(label=t("btn.theme_edit"), command=self.edit_theme)

    def choose_theme(self, code):
        """
        Theme menu: save the chosen theme in the config. It is used from the next start, like the language.

        @param code  theme code of the chosen entry
        """
        code = resolve_theme(code)
        if code == self.cfg["theme"]:
            return
        self.cfg["theme"] = code
        self.save_config()
        self.theme_var.set(code)
        messagebox.showinfo(t("dlg.restart"), t("msg.restart"), parent=self.root)

    def edit_theme(self):
        """Open the theme editor on the current theme; a theme saved there becomes the chosen one."""
        dlg = ThemeDialog(self.root, self.cfg["theme"])
        if dlg.result:
            self.choose_theme(dlg.result)
        self._fill_themes()

    def choose_language(self, code):
        """
        Language menu: save the chosen language in the config. It is used from the next start, because all
        texts are put into the windows when the program starts.

        @param code  language code of the chosen entry, e.g. "ja"
        """
        code = resolve_language(code)
        if code == self.cfg["language"]:
            return
        self.cfg["language"] = code
        self.save_config()
        messagebox.showinfo(t("dlg.restart"), t("msg.restart"), parent=self.root)

    @staticmethod
    def _key(command):
        """Run a shortcut and stop the key from reaching the widget."""
        command()
        return "break"

    def update_title(self):
        name = os.path.basename(self.project_path) if self.project_path else t("title.untitled")
        mark = "*" if self.dirty() else ""
        self.root.title(t("app.title_file", name=name, mark=mark, version=VERSION))

    def dirty(self):
        return self.history.current != self.saved_state

    def save_config(self):
        try:
            save_config(self.cfg)
        except OSError as exc:
            messagebox.showerror(t("dlg.error"), t("msg.save_failed", error=exc), parent=self.root)

    def apply_settings(self, ffmpeg_changed, cache_changed=False):
        """
        Use settings that work without a restart.

        @param ffmpeg_changed  True if the FFmpeg location was changed (it is searched again)
        @param cache_changed   True if the cache folder was changed (the preview cache moves there)
        """
        if cache_changed:
            self.engine.set_cache_dir(self.cfg["cache_dir"])
        if ffmpeg_changed:
            self.tools = ff.locate(self.cfg["ffmpeg"])
            self.engine.tools = self.tools
            if not self.tools:
                messagebox.showwarning(t("dlg.error"), t("msg.no_ffmpeg"), parent=self.root)
        self.jobs.set_workers(self.cfg["workers"])
        self.jobs.set_threads(self.cfg["worker_threads"])
        self.request_preview(10)

    def run(self, path=None):
        if path and os.path.isfile(path):
            self.open_path(path)
        self.root.mainloop()

    def quit(self):
        if not self.confirm_discard():
            return
        if self.jobs.active_count() and not messagebox.askyesno(t("dlg.export"), t("msg.quit_jobs"), parent=self.root):
            return
        try:
            zoomed = self.root.state() == "zoomed" if IS_WIN or IS_MAC else bool(self.root.attributes("-zoomed"))
        except tk.TclError:
            zoomed = False
        self.cfg["zoomed"] = zoomed
        if not self.cfg["zoomed"]:
            self.cfg["geometry"] = self.root.geometry()
        self.save_config()
        self.jobs.shutdown()
        self.engine.close()
        self.root.destroy()

    def _poll(self):
        """Run what worker threads left in the inbox."""
        try:
            while True:
                self.inbox.get_nowait()()
        except queue.Empty:
            pass
        self.root.after(40, self._poll)

    # ------------------------------------------------------------------ #
    # Time and preview
    # ------------------------------------------------------------------ #

    def _max_time(self):
        return max(self.project.duration() - 1.0 / self.project.fps, 0.0)

    def update_time_range(self):
        top = self._max_time()
        self.scale.configure(to=top if top > 0 else 1.0)
        self.scale.state(["!disabled"] if top > 0 else ["disabled"])
        if self.t > top:
            self.set_time(top)
        else:
            self._time_text()

    def _time_text(self):
        self.time_label.configure(text="%s / %s" % (fmt_time(self.t), fmt_time(self.project.duration())))

    def set_time(self, seconds):
        """Go to a timeline second (snapped to a frame)."""
        fps = self.project.fps
        self.t = min(max(round(seconds * fps) / fps, 0.0), self._max_time())
        self.time_var.set(self.t)
        self._time_text()
        self.request_preview()

    def step(self, frames):
        self.set_time(self.t + frames / self.project.fps)

    def goto_dialog(self):
        """Ctrl+G: ask for a time or frame number and go there."""
        result = GotoDialog(self).result
        if result is not None:
            self.set_time(result)

    def _scale_moved(self, raw):
        fps = self.project.fps
        t = min(max(round(float(raw) * fps) / fps, 0.0), self._max_time())
        if t != self.t:
            self.t = t
            self._time_text()
            self.request_preview()

    # ------------------------------------------------------------------ #
    # Preview zoom and pan
    #   left drag = move, Ctrl + left drag (left / right) = zoom, wheel = zoom, double click = fit / 100%,
    #   Ctrl+0 fit, Ctrl+1 100%, Ctrl +/- step, right click = menu.
    # ------------------------------------------------------------------ #

    ZOOM_STEPS = (0.0625, 0.125, 0.25, 1 / 3.0, 0.5, 2 / 3.0, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0)
    MAX_ZOOM = 16.0

    def _canvas_size(self):
        return max(self.canvas.winfo_width(), 64), max(self.canvas.winfo_height(), 64)

    def _min_zoom(self):
        """Scale of the fitted picture (the smallest zoom)."""
        return self.preview_size()[0] / float(self.project.width)

    def _state(self):
        """@return (zoom, centre x, centre y) of the view now; the fitted picture counts as the lowest zoom"""
        p = self.project
        if self.zoom is None:
            return self._min_zoom(), p.width / 2.0, p.height / 2.0
        return self.zoom, self.cx, self.cy

    def _clamp_view(self):
        """Keep the picture in view; on an axis where it is smaller than the area it stays centred."""
        p, (cw, ch) = self.project, self._canvas_size()
        hw, hh = cw / (2.0 * self.zoom), ch / (2.0 * self.zoom)
        self.cx = p.width / 2.0 if p.width <= 2 * hw else min(max(self.cx, hw), p.width - hw)
        self.cy = p.height / 2.0 if p.height <= 2 * hh else min(max(self.cy, hh), p.height - hh)

    def _sanitize_view(self):
        """Back to fit if the zoom is not above fit any more (window got bigger, project size changed)."""
        if self.zoom is not None:
            if self.zoom <= self._min_zoom() * 1.001:
                self.zoom = None
            else:
                self._clamp_view()
            self._apply_cursor()
            self._draw_badge()

    def _view_request(self):
        """@return (size of the whole frame to render, (x, y, w, h, zoom) = the visible part in project pixels)"""
        p, (cw, ch), z = self.project, self._canvas_size(), self.zoom
        self._clamp_view()
        hw, hh = cw / (2.0 * z) * 1.5, ch / (2.0 * z) * 1.5                 # a margin around the view: small moves need no new render
        x0, x1 = max(0, int(math.floor(self.cx - hw))), min(p.width, int(math.ceil(self.cx + hw)))
        y0, y1 = max(0, int(math.floor(self.cy - hh))), min(p.height, int(math.ceil(self.cy + hh)))
        return (even(p.width), even(p.height)), (x0, y0, max(1, x1 - x0), max(1, y1 - y0), z)

    def zoom_to(self, z, ax=None, ay=None, base=None):
        """
        Zoom to z; the picture point under the canvas point (ax, ay) stays where it is (default: the middle).

        @param base  (zoom, cx, cy) to calculate from (a drag keeps the state it started with), default the view now
        """
        cw, ch = self._canvas_size()
        ax, ay = (cw / 2.0, ch / 2.0) if ax is None else (ax, ay)
        z0, cx0, cy0 = base or self._state()
        z = min(max(z, self._min_zoom()), self.MAX_ZOOM)
        if z <= self._min_zoom() * 1.001:
            self.zoom = None
        else:
            px, py = cx0 + (ax - cw / 2.0) / z0, cy0 + (ay - ch / 2.0) / z0   # picture point under the pointer
            self.zoom = z
            self.cx, self.cy = px - (ax - cw / 2.0) / z, py - (ay - ch / 2.0) / z
            self._clamp_view()
        self._apply_cursor()
        self._show_interim()
        self._draw_badge()
        self.request_preview(30)

    def zoom_fit(self):
        self.zoom = None
        self._apply_cursor()
        self._show_interim()
        self._draw_badge()
        self.request_preview(0)

    def zoom_step(self, direction):
        """Next zoom level up (+1) or down (-1): 6.25 ... 100, 200 ... 1600 %."""
        z, low = self._state()[0], self._min_zoom()
        steps = sorted(set(self.ZOOM_STEPS) | {low})
        if direction > 0:
            self.zoom_to(next((s for s in steps if s > z * 1.001), self.MAX_ZOOM))
        else:
            below = [s for s in steps if s < z / 1.001]
            self.zoom_to(below[-1] if below else low)

    def _apply_cursor(self, zooming=False):
        self.canvas.configure(cursor="sb_h_double_arrow" if zooming else ("fleur" if self.zoom is not None else ""))

    def _show_interim(self):
        """
        Show the new view AT ONCE from a picture that is already there (the last one shown, or the fitted one),
        when it has the same scale (fit, or moving around); other scales come smooth from the engine.
        """
        z, cx, cy = self._state()
        cw, ch = self._canvas_size()
        hw, hh = cw / (2.0 * z), ch / (2.0 * z)
        best, best_area = None, 0.0
        for src in (self._shown, self._fit):                # first: the last shown one (sharper); the fit one covers all
            if not src:
                continue
            img, x0, y0, zs = src
            if abs(z / zs - 1.0) > 0.01:
                continue                                    # other scale: no blocky stretching, the engine sends a smooth picture
            ix0, ix1 = max(cx - hw, x0), min(cx + hw, x0 + img.width() / zs)
            iy0, iy1 = max(cy - hh, y0), min(cy + hh, y0 + img.height() / zs)
            area = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
            if area > 0 and (best is None or area > best_area * 1.02):
                best, best_area = (src, ix0, ix1, iy0, iy1), area
        if best is None:
            return
        (img, x0, y0, zs), ix0, ix1, iy0, iy1 = best
        u0, u1 = max(0, int(math.floor((ix0 - x0) * zs))), min(img.width(), int(math.ceil((ix1 - x0) * zs)))
        v0, v1 = max(0, int(math.floor((iy0 - y0) * zs))), min(img.height(), int(math.ceil((iy1 - y0) * zs)))
        if u1 <= u0 or v1 <= v0:
            return
        out = tk.PhotoImage()
        out.tk.call(out.name, "copy", img.name, "-from", u0, v0, u1, v1)
        # centre of the cut-out on the screen (exact), the picture is placed around it
        mx, my = cw / 2.0 + ((u0 + u1) / 2.0 / zs + x0 - cx) * z, ch / 2.0 + ((v0 + v1) / 2.0 / zs + y0 - cy) * z
        self.canvas.delete("frame")
        self.canvas.create_image(mx, my, image=out, tags="frame")
        self._interim_img = out
        self.canvas.tag_raise("badge")

    def _draw_badge(self):
        """The zoom level in the corner while zoomed; a click on it goes back to fit."""
        self.canvas.delete("badge")
        text = self.canvas.create_text(16, 12, text="%d%%" % round(self._state()[0] * 100), anchor="nw",
                                       fill=self.col_info, tags="badge")
        x0, y0, x1, y1 = self.canvas.bbox(text)
        box = self.canvas.create_rectangle(x0 - 6, y0 - 3, x1 + 6, y1 + 3, fill=self.col_bg, outline=self.col_info,
                                           tags="badge")
        self.canvas.tag_lower(box, text)

    def _view_configure(self, _event):
        self._sanitize_view()
        self.request_preview(200)

    def _view_press(self, event):
        self.canvas.focus_set()
        if "badge" in self.canvas.gettags("current"):
            self._drag = None
            self.zoom_fit()
            return
        ctrl = bool(event.state & 0x4) or (IS_MAC and bool(event.state & 0x8))   # Ctrl, or Cmd on macOS
        self._drag = {"x": event.x, "y": event.y, "lx": event.x, "ly": event.y, "zoom": ctrl, "base": self._state()}
        if ctrl:
            self._apply_cursor(True)

    def _view_drag(self, event):
        d = self._drag
        if not d:
            return
        if d["zoom"]:                                   # Ctrl + drag right = zoom in, left = zoom out, around the press point
            self.zoom_to(d["base"][0] * math.exp((event.x - d["x"]) * 0.006), d["x"], d["y"], d["base"])
            self._apply_cursor(True)
        elif self.zoom is not None:                     # plain drag = move the picture
            ox, oy, z = self.cx, self.cy, self.zoom
            self.cx -= (event.x - d["lx"]) / z
            self.cy -= (event.y - d["ly"]) / z
            self._clamp_view()
            self._show_interim()                        # at once; the sharp picture follows
            self.request_preview(60)
        d["lx"], d["ly"] = event.x, event.y

    def _view_release(self, _event):
        if self._drag:
            self._drag = None
            self._apply_cursor()
            if self.zoom is not None:
                self.request_preview(0)

    def _view_wheel(self, event, direction=None):
        if direction is None:
            direction = 1 if event.delta > 0 else -1
        self.zoom_to(self._state()[0] * 1.25 ** direction, event.x, event.y)
        return "break"

    def _view_menu(self, event):
        """Right click menu."""
        label = "Cmd" if IS_MAC else "Ctrl"
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label=t("view.fit"), accelerator=label + "+0", command=self.zoom_fit)
        for percent in (50, 100, 200, 400, 800):
            menu.add_command(label="%d%%" % percent, accelerator=label + "+1" if percent == 100 else "",
                             command=lambda z=percent / 100.0: self.zoom_to(z))
        menu.add_separator()
        menu.add_command(label=t("view.zoom_in"), accelerator=label + "++", command=lambda: self.zoom_step(1))
        menu.add_command(label=t("view.zoom_out"), accelerator=label + "+-", command=lambda: self.zoom_step(-1))
        menu.add_separator()
        menu.add_command(label=t("menu.goto"), accelerator=label + "+G", command=self.goto_dialog)
        menu.add_command(label=t("menu.export_frame"), accelerator=label + "+Shift+E", command=self.export_frame_dialog)
        menu.add_command(label=t("menu.project"), command=lambda: self.project_settings())
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def preview_size(self):
        """@return (width, height) of the preview picture: the frame fitted into the area, never above the project size"""
        p = self.project
        cw, ch = max(self.canvas.winfo_width() - 8, 64), max(self.canvas.winfo_height() - 8, 64)
        s = min(cw / p.width, ch / p.height, self.cfg["preview_max"] / max(p.width, p.height), 1.0)
        return even(p.width * s), even(p.height * s)

    def request_preview(self, delay=50):
        """Show the picture at the current second after a short pause (many changes in a row render once)."""
        if self._job:
            self.root.after_cancel(self._job)
        self._job = self.root.after(delay, self._fire)

    def _compare(self, original):
        """Held button: show the picture without effects; released: the edited picture again."""
        if original != self.compare:
            self.compare = original
            self.request_preview(0)

    def _fire(self):
        self._job = None
        if not self.tools:
            self._say(t("msg.no_ffmpeg_short"), True)
            return
        self._sanitize_view()
        size, view = self.preview_size(), None
        if self.zoom is not None:
            size, view = self._view_request()
        self._view_req = view
        self.engine.fit_size = self.preview_size()      # the picture the engine makes the quick preview of a zoom from
        self._seq = self.engine.request(self.project, self.t, size, self.sel_layer, self.compare, view)

    def _say(self, text, error=False):
        self.status.configure(text=text, foreground=self.col_err if error else self.col_info)

    def _on_result(self, seq, path, error):
        if seq != self._seq:
            return                                  # a newer picture was asked for
        if error:
            self._say(error.splitlines()[-1] if error.strip() else error, True)
            return
        try:
            self._img = tk.PhotoImage(file=path)
        except tk.TclError as exc:
            self._say(str(exc), True)
            return
        self._say(t("status.original") if self.compare else "")
        cw, ch = self._canvas_size()
        if self.zoom is None and self._view_req is None:                    # fit: the whole frame, centred
            self.canvas.delete("frame")
            self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                     image=self._img, tags="frame")
            self._fit = self._shown = (self._img, 0.0, 0.0, self._img.width() / float(self.project.width))
        elif self.zoom is not None and self._view_req is not None and abs(self._view_req[4] - self.zoom) < 1e-9:
            x0, y0, _w, _h, z = self._view_req                              # zoomed: part of the frame, placed by the
            self.canvas.delete("frame")                                     # CURRENT centre (it may have moved since)
            self.canvas.create_image(cw / 2.0 + (x0 - self.cx) * z, ch / 2.0 + (y0 - self.cy) * z,
                                     image=self._img, anchor="nw", tags="frame")
            self._shown = (self._img, x0, y0, z)
        # else: the view changed since this picture was asked for; the newer one is on its way
        self._draw_badge()
        self.canvas.delete("hint")
        if not self.project.layers:
            self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                    text=t("msg.empty"), fill=self.col_info, tags="hint", justify="center")

    # ------------------------------------------------------------------ #
    # Refreshing
    # ------------------------------------------------------------------ #

    def selected(self):
        return self.project.find(self.sel_layer)

    def refresh_layers(self):
        self._missing = self.media_missing()
        self.layers.refresh(self.project, self.sel_layer, self._missing)

    def media_missing(self):
        """@return ids of the media layers whose file does not exist"""
        return {l.id for l in self.project.media_layers() if l.is_missing()}

    def _focus_in(self, event):
        """The window got the focus back: a file may have been moved away or back meanwhile."""
        if event.widget is self.root and self.media_missing() != self._missing:
            self.refresh_layers()
            self.refresh_inspector()
            self.request_preview()

    def refresh_inspector(self):
        layer = self.selected()
        self.props.show(layer)
        self.effects.refresh(layer, self.sel_effect)

    def refresh_all(self):
        if self.selected() is None:
            self.sel_layer = self.project.layers[-1].id if self.project.layers else None
        layer = self.selected()
        if layer is None or not 0 <= (self.sel_effect if self.sel_effect is not None else -1) < len(layer.effects):
            self.sel_effect = 0 if layer and layer.effects else None
        self.refresh_layers()
        self.refresh_inspector()

    def touch(self, merge_key=None):
        """Call after every change of the project: records undo, updates title, time range and preview."""
        self.history.record(self.project, merge_key)
        self.update_title()
        self.update_time_range()
        self.request_preview()

    # ------------------------------------------------------------------ #
    # Layers
    # ------------------------------------------------------------------ #

    def select_layer(self, layer_id):
        self.sel_layer = layer_id
        layer = self.selected()
        self.sel_effect = 0 if layer and layer.effects else None
        self.refresh_inspector()
        self.request_preview()

    def add_media_dialog(self):
        paths = filedialog.askopenfilenames(parent=self.root, title=t("dlg.add_media"), filetypes=MEDIA_TYPES,
                                            initialdir=self.cfg["last_dir"] or None)
        if paths:
            self.add_media(list(paths))

    def add_media(self, paths):
        """Probe the files and put each on top as a media layer."""
        if not self.tools:
            messagebox.showwarning(t("dlg.error"), t("msg.no_ffmpeg"), parent=self.root)
            return
        added = None
        self.root.configure(cursor="watch")
        self.root.update_idletasks()
        try:
            for path in paths:
                try:
                    added = self.project.add_media(ff.probe(self.tools, path))
                except ff.FFmpegError as exc:
                    messagebox.showerror(t("dlg.error"), t("msg.cannot_open", name=os.path.basename(path), error=exc),
                                         parent=self.root)
        finally:
            self.root.configure(cursor="")
        if added:
            self.cfg["last_dir"] = os.path.dirname(paths[-1])
            self.sel_layer, self.sel_effect = added.id, None
            self.refresh_all()
            self.touch()

    def add_adjustment(self):
        count = sum(isinstance(l, AdjustmentLayer) for l in self.project.layers) + 1
        layer = AdjustmentLayer("%s %d" % (t("kind.adjustment"), count))
        self.project.layers.append(layer)
        self.sel_layer, self.sel_effect = layer.id, None
        self.refresh_all()
        self.touch()

    def remove_layer(self):
        layer = self.selected()
        if layer is None:
            return
        index = self.project.layers.index(layer)
        self.project.layers.remove(layer)
        rest = self.project.layers
        self.sel_layer = rest[min(index, len(rest) - 1)].id if rest else None
        self.sel_effect = None
        self.refresh_all()
        self.touch()

    def duplicate_layer(self):
        layer = self.selected()
        if layer is None:
            return
        other = layer.clone()
        other.name = t("name.copy", name=layer.name)
        self.project.layers.insert(self.project.layers.index(layer) + 1, other)
        self.sel_layer = other.id
        self.refresh_all()
        self.touch()

    def move_layer(self, delta):
        """@param delta  +1 = up (drawn later, over the others), -1 = down"""
        layer = self.selected()
        layers = self.project.layers
        if layer is None:
            return
        i = layers.index(layer)
        j = i + delta
        if 0 <= j < len(layers):
            layers[i], layers[j] = layers[j], layers[i]
            self.refresh_layers()
            self.touch()

    def toggle_layer(self, layer_id):
        layer = self.project.find(layer_id)
        if layer:
            layer.enabled = not layer.enabled
            self.refresh_layers()
            self.touch()

    def set_layer(self, layer_id, attr, value, merge=None):
        layer = self.project.find(layer_id)
        if layer is None or getattr(layer, attr) == value:
            return
        setattr(layer, attr, value)
        if attr == "name":
            self.refresh_layers()
        self.touch((layer_id, merge) if merge else None)

    def relink_layer(self, layer_id):
        """Let the user pick the file of a media layer (the old one was moved or deleted)."""
        layer = self.project.find(layer_id)
        if not isinstance(layer, MediaLayer):
            return
        folder = os.path.dirname(layer.path)
        path = filedialog.askopenfilename(parent=self.root, title=t("dlg.relink"), filetypes=MEDIA_TYPES,
                                          initialdir=folder if os.path.isdir(folder) else (self.cfg["last_dir"] or None),
                                          initialfile=os.path.basename(layer.path))
        if path:
            self.set_media_path(layer_id, path)

    def set_media_path(self, layer_id, path):
        """Point a media layer to another file. The file is probed; timing, effects and name stay as they are."""
        layer = self.project.find(layer_id)
        path = path.strip().strip('"')
        if not isinstance(layer, MediaLayer) or not path or path == layer.path:
            return
        if not self.tools:
            messagebox.showwarning(t("dlg.error"), t("msg.no_ffmpeg"), parent=self.root)
            return
        try:
            info = ff.probe(self.tools, path)
        except ff.FFmpegError as exc:
            messagebox.showerror(t("dlg.error"), t("msg.cannot_open", name=os.path.basename(path), error=exc),
                                 parent=self.root)
            self.refresh_inspector()                # the field shows the old path again
            return
        layer.path, layer.source_duration = info.path, info.duration
        layer.has_audio, layer.is_image = info.has_audio, info.is_image
        self.cfg["last_dir"] = os.path.dirname(path)
        self.refresh_layers()
        self.refresh_inspector()
        self.touch()

    def project_settings(self):
        result = ProjectDialog(self).result
        if result:
            width, height, fps = result
            self.project.width, self.project.height = width, height
            self.zoom = None
            self._apply_cursor()
            if fps:
                self.project.fps = fps
            self.touch()

    # ------------------------------------------------------------------ #
    # Effects
    # ------------------------------------------------------------------ #

    def select_effect(self, index):
        self.sel_effect = index
        self.effects.refresh(self.selected(), index)

    def add_effect(self, effect_id):
        layer = self.selected()
        if layer is None:
            return
        layer.effects.append(EffectInstance.create(effect_id))
        self.sel_effect = len(layer.effects) - 1
        self.effects.refresh(layer, self.sel_effect)
        self.touch()

    def remove_effect(self):
        layer, i = self.selected(), self.sel_effect
        if layer is None or i is None:
            return
        del layer.effects[i]
        self.sel_effect = min(i, len(layer.effects) - 1) if layer.effects else None
        self.effects.refresh(layer, self.sel_effect)
        self.touch()

    def move_effect(self, delta):
        layer, i = self.selected(), self.sel_effect
        if layer is None or i is None or not 0 <= i + delta < len(layer.effects):
            return
        layer.effects[i], layer.effects[i + delta] = layer.effects[i + delta], layer.effects[i]
        self.sel_effect = i + delta
        self.effects.refresh(layer, self.sel_effect)
        self.touch()

    def toggle_effect(self, index):
        layer = self.selected()
        if layer and 0 <= index < len(layer.effects):
            layer.effects[index].enabled = not layer.effects[index].enabled
            self.effects.refresh(layer, self.sel_effect)
            self.touch()

    def set_param(self, layer_id, index, key, value):
        layer = self.project.find(layer_id)
        if layer is None or not 0 <= index < len(layer.effects):
            return
        values = layer.effects[index].values
        if values.get(key) != value:
            values[key] = value
            self.touch((layer_id, index, key))

    # ------------------------------------------------------------------ #
    # Undo, files
    # ------------------------------------------------------------------ #

    def _restore(self, state):
        if state is None:
            return
        self.project = Project.from_dict(state)
        self.refresh_all()
        self.update_title()
        self.update_time_range()
        self.request_preview()

    def undo(self):
        self._restore(self.history.undo())

    def redo(self):
        self._restore(self.history.redo())

    def confirm_discard(self):
        """@return True if it is fine to drop the current project (saved, or the user agreed)"""
        if not self.dirty():
            return True
        answer = messagebox.askyesnocancel(APP_NAME, t("msg.unsaved"), parent=self.root)
        if answer is None:
            return False
        return self.save() if answer else True

    def _load(self, project, path):
        self.project, self.project_path = project, path
        self.history.reset(project)
        self.saved_state = self.history.current
        self.sel_layer = self.sel_effect = None
        self.t = 0.0
        self.zoom = None
        self._apply_cursor()
        self.refresh_all()
        self.update_title()
        self.update_time_range()
        self.set_time(0.0)

    def new_project(self):
        if self.confirm_discard():
            self._load(Project(), None)

    def open_dialog(self):
        if not self.confirm_discard():
            return
        path = filedialog.askopenfilename(parent=self.root, title=t("dlg.open"),
                                          filetypes=[(APP_NAME, "*" + PROJECT_EXT), ("*", "*.*")],
                                          initialdir=self.cfg["last_dir"] or None)
        if path:
            self.open_path(path)

    def open_path(self, path):
        """Open a project file, or put a media file into a new project."""
        if os.path.splitext(path)[1].lower() == PROJECT_EXT:
            try:
                self._load(load_project(path), path)
            except ProjectError as exc:
                messagebox.showerror(t("dlg.error"), t("msg.cannot_open", name=os.path.basename(path), error=exc),
                                     parent=self.root)
                return
            self.cfg["last_dir"] = os.path.dirname(path)
            self.add_recent(path)
        else:
            self.add_media([path])

    # ------------------------------------------------------------------ #
    # Recent projects
    # ------------------------------------------------------------------ #

    @staticmethod
    def _recent_label(index, path):
        """@return menu text of a recent project: its number, the file name and the (shortened) folder"""
        folder = os.path.dirname(path)
        if len(folder) > 48:
            folder = "..." + folder[-45:]
        return "%d. %s   (%s)" % (index + 1, os.path.basename(path), folder)

    def _fill_recent(self):
        """Rebuild the File > Open recent submenu from the config list (newest first)."""
        menu, recent = self.recent_menu, self.cfg["recent"]
        menu.delete(0, "end")
        if not recent:
            menu.add_command(label=t("menu.recent_empty"), state="disabled")
            return
        for i, path in enumerate(recent):
            menu.add_command(label=self._recent_label(i, path), command=lambda p=path: self.open_recent(p))
        menu.add_separator()
        menu.add_command(label=t("menu.recent_clear"), command=self.clear_recent)

    def add_recent(self, path):
        """Put a project file first in the recent list, update the menu and save the config at once."""
        self.cfg["recent"] = push_recent(self.cfg["recent"], path)
        self._fill_recent()
        try:
            save_config(self.cfg)               # at once so a crash does not lose the list; errors show on quit
        except OSError:
            pass

    def open_recent(self, path):
        """Open a project of the recent list (after asking about unsaved changes); a missing file is dropped."""
        if not os.path.isfile(path):
            self.cfg["recent"] = [p for p in self.cfg["recent"] if p != path]
            self._fill_recent()
            messagebox.showerror(t("dlg.error"), t("msg.recent_missing", name=os.path.basename(path)),
                                 parent=self.root)
        elif self.confirm_discard():
            self.open_path(path)

    def clear_recent(self):
        """Empty the recent list (File > Open recent > Clear list)."""
        self.cfg["recent"] = []
        self._fill_recent()

    def save(self):
        """@return True if saved"""
        return self._write(self.project_path) if self.project_path else self.save_as()

    def _media_stem(self):
        """@return the file name (no folder, no extension) of the first media layer; empty if there is none"""
        media = self.project.media_layers()
        return os.path.splitext(os.path.basename(media[0].path))[0] if media and media[0].path else ""

    def suggest_save_path(self):
        """
        @return (folder, file name) the save dialog starts with: the current project file if there is one, else the
                name of the first media file (a number is added if that project file already exists), else Untitled
        """
        if self.project_path:
            return os.path.dirname(self.project_path), os.path.basename(self.project_path)
        folder = self.cfg["last_dir"] or ""
        stem = self._media_stem() or t("title.untitled")
        name, number = stem + PROJECT_EXT, 1
        while folder and os.path.exists(os.path.join(folder, name)):
            number += 1
            name = "%s %d%s" % (stem, number, PROJECT_EXT)
        return folder, name

    def save_as(self):
        folder, name = self.suggest_save_path()
        path = filedialog.asksaveasfilename(parent=self.root, title=t("dlg.save"), defaultextension=PROJECT_EXT,
                                            filetypes=[(APP_NAME, "*" + PROJECT_EXT)],
                                            initialdir=folder or None, initialfile=name)
        return self._write(path) if path else False

    def _write(self, path):
        try:
            save_project(self.project, path)
        except OSError as exc:
            messagebox.showerror(t("dlg.error"), t("msg.save_failed", error=exc), parent=self.root)
            return False
        self.project_path, self.saved_state = path, self.history.current
        self.cfg["last_dir"] = os.path.dirname(path)
        self.add_recent(path)
        self.update_title()
        return True

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #

    def export_dialog(self):
        if not self.tools:
            messagebox.showwarning(t("dlg.error"), t("msg.no_ffmpeg"), parent=self.root)
        elif self.project.duration() <= 0:
            messagebox.showinfo(t("dlg.export"), t("msg.nothing_to_export"), parent=self.root)
        elif not ff.available_presets(self.tools):
            messagebox.showerror(t("dlg.error"), t("msg.no_encoder"), parent=self.root)
        else:
            dlg = ExportDialog(self)
            if dlg.result == "start":
                self.jobs.start_all()
                self.show_jobs()
            elif dlg.result == "add":
                self._say(t("status.job_queued", name=dlg.queued_name))

    def show_jobs(self):
        """Open the job queue window, or bring the open one to the front."""
        if self.jobs_win is not None and self.jobs_win.winfo_exists():
            self.jobs_win.deiconify()
            self.jobs_win.lift()
        else:
            self.jobs_win = JobsWindow(self)

    def _job_changed(self, job):
        """A job was added or changed state (Tk thread): update the queue window and tell the result in the status line."""
        if self.jobs_win is not None and self.jobs_win.winfo_exists():
            self.jobs_win.refresh()
        if job.state is JobState.DONE:
            self._say(t("status.export_done", path=job.out_path))
            self.root.bell()
        elif job.state is JobState.FAILED:
            self._say(t("status.export_failed", name=job.name), True)
            self.root.bell()

    def export_frame_dialog(self):
        """
        Export the picture at the current second in full project size (PNG or JPEG). The file name starts as
        <first media file>_snapshot_<hh.mm.ss.mmm>, like the snapshots of MPC.
        """
        if not self.tools:
            messagebox.showwarning(t("dlg.error"), t("msg.no_ffmpeg"), parent=self.root)
            return
        stem = self._media_stem() or (os.path.splitext(os.path.basename(self.project_path))[0] if self.project_path
                                      else "") or t("title.untitled")
        path = filedialog.asksaveasfilename(parent=self.root, title=t("menu.export_frame"), defaultextension=".png",
                                            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg")],
                                            initialdir=self.cfg["last_dir"] or None,
                                            initialfile="%s_snapshot_%s.png" % (stem, fmt_stamp(self.t)))
        if not path:
            return
        project, seconds, tools = self.project, self.t, self.tools
        self._say(t("status.exporting_frame"))

        def work():
            try:
                save_frame(tools, project, seconds, path)
                message = (t("msg.frame_exported", path=path), False)
            except (ff.FFmpegError, CompileError) as exc:
                message = (t("msg.frame_failed", error=exc), True)
            self.inbox.put(lambda: self._say(*message))

        threading.Thread(target=work, daemon=True).start()
