"""ui/dialogs.py - Settings, project settings and export dialogs."""

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from chloe_lang import list_languages, resolve_language, t
from chloe_theme import ThemeDialog, list_themes, resolve_theme

from engine import APP_NAME, VERSION
from engine import effect as fx
from engine import ffmpeg as ff
from engine.compiler import CompileError, compile_export
from engine.config import PREVIEW_RANGE, config_path
from engine.jobs import THREADS_RANGE, WORKERS_RANGE, QueueError
from engine.project import even
from ui.common import Dialog, buttons_row, fmt_number, fmt_time, ok_cancel, parse_time


def _int(text, lo, hi, default):
    try:
        return max(lo, min(hi, int(float(text))))
    except (TypeError, ValueError):
        return default


def build_about(page, app):
    """
    Fill a frame with the program name and version, the description and the facts about this installation
    (Python, Tk, drag and drop, FFmpeg, effects, config file, cache folder, plugin errors).

    @param page  frame to fill (the About tab of Settings, or the body of the About window)
    @param app   the App (gives the FFmpeg tools, the drag and drop state and the preview cache)
    """
    ttk.Label(page, text="%s %s" % (APP_NAME, VERSION), font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
    ttk.Label(page, text=t("about.description"), wraplength=400, justify="left").pack(anchor="w", pady=(4, 12))
    tools = app.tools
    rows = [(t("about.python"), sys.version.split()[0]), (t("about.tk"), str(tk.TkVersion)),
            (t("about.dnd"), t("about.dnd_on") if app.dnd_ok else t("about.dnd_off")),
            (t("about.ffmpeg"), "%s (%s)" % (tools.version, tools.ffmpeg) if tools else t("about.missing")),
            (t("about.effects"), str(len(fx.all_effects()))),
            (t("about.config"), config_path()),
            (t("about.cache"), os.path.dirname(app.engine.cache.folder))]
    errors = fx.load_errors()
    if errors:
        rows.append((t("about.plugin_errors"), "\n".join(errors)))
    grid = ttk.Frame(page)
    grid.pack(anchor="w")
    for row, (name, value) in enumerate(rows):
        ttk.Label(grid, text=name).grid(row=row, column=0, sticky="nw", pady=2)
        ttk.Label(grid, text=value, wraplength=320, justify="left").grid(
            row=row, column=1, sticky="w", padx=(12, 0), pady=2)


class AboutDialog(Dialog):
    """The About window of the main menu: the same information as the About tab of Settings, and an OK button."""

    def __init__(self, app):
        """@param app  the App that opened it"""
        super().__init__(app.root, "dlg.about")
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        buttons = ttk.Frame(frm)                                    # packed first so it keeps its place at the bottom
        buttons.pack(side="bottom", fill="x", pady=(12, 0))
        ok = ttk.Button(buttons, text=t("btn.ok"), command=self.destroy)
        ok.pack(side="right")
        build_about(frm, app)
        self.show(ok, self.destroy)


class SettingsDialog(Dialog):
    """Settings with the tabs General, Appearance, Language and About. OK saves the config, Cancel drops the changes."""

    def __init__(self, app):
        """@param app  the App that opened it"""
        super().__init__(app.root, "dlg.settings")
        self.app, cfg = app, app.cfg
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ok_cancel(frm, self._ok, self.destroy).pack(side="bottom", fill="x", pady=(10, 0))
        self.nb = ttk.Notebook(frm)
        self.nb.pack(fill="both", expand=True)
        self._tab_general(cfg)
        self._tab_workers(cfg)
        self._tab_appearance(cfg)
        self._tab_language(cfg)                                     # next to the last tab (About)
        self._tab_about()
        self.show()

    def _page(self, key):
        """
        Add a tab.

        @param key  language key of the tab text
        @return     the empty page (frame) of the tab
        """
        page = ttk.Frame(self.nb, padding=14)
        self.nb.add(page, text=t(key))
        return page

    def _tab_general(self, cfg):
        page = self._page("tab.general")
        page.columnconfigure(1, weight=1)
        ttk.Label(page, text=t("field.ffmpeg")).grid(row=0, column=0, sticky="w", pady=4)
        self.ffmpeg_var = tk.StringVar(value=cfg["ffmpeg"])
        ttk.Entry(page, textvariable=self.ffmpeg_var, width=40).grid(row=0, column=1, sticky="we", padx=(12, 4))
        ttk.Button(page, text=t("btn.pick"), command=self._pick_ffmpeg).grid(row=0, column=2)
        ttk.Label(page, text=t("hint.ffmpeg"), wraplength=420, justify="left").grid(
            row=1, column=1, columnspan=2, sticky="w", padx=(12, 0))
        ttk.Label(page, text=t("field.preview_max")).grid(row=2, column=0, sticky="w", pady=(14, 4))
        self.preview_var = tk.StringVar(value=str(cfg["preview_max"]))
        ttk.Spinbox(page, textvariable=self.preview_var, from_=PREVIEW_RANGE[0], to=PREVIEW_RANGE[1],
                    increment=160, width=8).grid(row=2, column=1, sticky="w", padx=(12, 0), pady=(14, 4))
        # preview cache folder: empty = cache/ next to the program, else the folder typed or picked here
        ttk.Label(page, text=t("field.cache_dir")).grid(row=3, column=0, sticky="w", pady=(14, 4))
        self.cache_var = tk.StringVar(value=cfg["cache_dir"])
        ttk.Entry(page, textvariable=self.cache_var, width=40).grid(row=3, column=1, sticky="we", padx=(12, 4),
                                                                    pady=(14, 4))
        ttk.Button(page, text=t("btn.pick"), command=self._pick_cache).grid(row=3, column=2, pady=(14, 4))
        ttk.Label(page, text=t("hint.cache_dir"), wraplength=420, justify="left").grid(
            row=4, column=1, columnspan=2, sticky="w", padx=(12, 0))

    def _tab_workers(self, cfg):
        """The Workers tab: how many workers render the job queue, and the thread limit of each."""
        page = self._page("tab.workers")
        page.columnconfigure(1, weight=1)
        self.workers_var = tk.StringVar(value=str(cfg["workers"]))
        self.threads_var = tk.StringVar(value=str(cfg["worker_threads"]))
        for row, (key, var, bounds) in enumerate((("field.workers", self.workers_var, WORKERS_RANGE),
                                                  ("field.worker_threads", self.threads_var, THREADS_RANGE))):
            ttk.Label(page, text=t(key)).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Spinbox(page, textvariable=var, from_=bounds[0], to=bounds[1], width=8).grid(
                row=row, column=1, sticky="w", padx=(12, 0), pady=4)
        ttk.Label(page, text=t("hint.workers"), wraplength=420, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

    def _pick_ffmpeg(self):
        path = filedialog.askopenfilename(parent=self, title=t("dlg.pick_ffmpeg"))
        if path:
            self.ffmpeg_var.set(path)

    def _pick_cache(self):
        """Choose the cache folder in a folder dialog (starts at the folder typed in the field, if any)."""
        path = filedialog.askdirectory(parent=self, title=t("dlg.pick_cache"),
                                       initialdir=self.cache_var.get().strip() or None)
        if path:
            self.cache_var.set(path)

    def _tab_appearance(self, cfg):
        page = self._page("tab.appearance")
        page.columnconfigure(1, weight=1)
        self.theme_var = tk.StringVar()
        ttk.Label(page, text=t("field.theme")).grid(row=0, column=0, sticky="w", pady=6)
        self.theme_box = ttk.Combobox(page, textvariable=self.theme_var, state="readonly", width=30)
        self.theme_box.grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Button(page, text=t("btn.theme_edit"), command=self._edit_theme).grid(
            row=1, column=1, sticky="w", padx=(12, 0), pady=(8, 0))
        self._show_themes(cfg["theme"])

    def _tab_language(self, cfg):
        """The Language tab: one box with the languages ("English", "Japanese - 日本語", ...)."""
        page = self._page("tab.language")
        page.columnconfigure(1, weight=1)
        self.langs = list_languages()
        self.lang_var = tk.StringVar()
        ttk.Label(page, text=t("field.language")).grid(row=0, column=0, sticky="w", pady=6)
        self.lang_box = ttk.Combobox(page, textvariable=self.lang_var, state="readonly", width=30,
                                     values=[n for _c, n in self.langs])
        self.lang_box.grid(row=0, column=1, sticky="w", padx=(12, 0))
        names = dict(self.langs)
        self.lang_var.set(names.get(cfg["language"], names.get("en", cfg["language"])))

    def _show_themes(self, code):
        """Fill the theme box and select the theme with this code."""
        self.themes = list_themes()
        self.theme_box.configure(values=[n for _c, n in self.themes])
        self.theme_var.set(dict(self.themes).get(code, code))

    @staticmethod
    def _picked(box, items, default):
        """@return the code chosen in a combobox, or `default` if nothing matches"""
        i = box.current()
        return items[i][0] if 0 <= i < len(items) else default

    def _edit_theme(self):
        code = self._picked(self.theme_box, self.themes, self.app.cfg["theme"])
        dlg = ThemeDialog(self, code)
        if dlg.result:
            self._show_themes(dlg.result)

    def _tab_about(self):
        """The About tab (same content as the About window of the main menu)."""
        build_about(self._page("tab.about"), self.app)

    def _ok(self):
        """Copy everything into the config, save, apply what works at once, close."""
        app, cfg = self.app, self.app.cfg
        old = (cfg["language"], cfg["theme"], cfg["ffmpeg"], cfg["cache_dir"])
        cfg["ffmpeg"] = self.ffmpeg_var.get().strip()
        cache = self.cache_var.get().strip()
        cfg["cache_dir"] = os.path.abspath(os.path.expanduser(cache)) if cache else ""    # empty = default folder
        cfg["preview_max"] = _int(self.preview_var.get(), PREVIEW_RANGE[0], PREVIEW_RANGE[1], cfg["preview_max"])
        cfg["workers"] = _int(self.workers_var.get(), *WORKERS_RANGE, cfg["workers"])
        cfg["worker_threads"] = _int(self.threads_var.get(), *THREADS_RANGE, cfg["worker_threads"])
        cfg["language"] = resolve_language(self._picked(self.lang_box, self.langs, cfg["language"]))
        cfg["theme"] = resolve_theme(self._picked(self.theme_box, self.themes, cfg["theme"]))
        app.save_config()
        app.apply_settings(ffmpeg_changed=old[2] != cfg["ffmpeg"], cache_changed=old[3] != cfg["cache_dir"])
        self.destroy()
        if old[:2] != (cfg["language"], cfg["theme"]):
            messagebox.showinfo(t("dlg.restart"), t("msg.restart"), parent=app.root)


class ProjectDialog(Dialog):
    """Frame size and frame rate of the project."""

    def __init__(self, app):
        super().__init__(app.root, "dlg.project")
        project = app.project
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        self.vars = {"width": tk.StringVar(value=str(project.width)),
                     "height": tk.StringVar(value=str(project.height)),
                     "fps": tk.StringVar(value=fmt_number(project.fps, 3))}
        spins = {}
        for row, (key, lo, hi, step) in enumerate((("width", 16, 16384, 2), ("height", 16, 16384, 2),
                                                   ("fps", 1, 120, 1))):
            ttk.Label(frm, text=t("field." + key)).grid(row=row, column=0, sticky="w", pady=4)
            spins[key] = ttk.Spinbox(frm, textvariable=self.vars[key], from_=lo, to=hi, increment=step, width=10)
            spins[key].grid(row=row, column=1, padx=(12, 0))
        ttk.Label(frm, text=t("hint.project"), wraplength=280, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ok_cancel(frm, self._ok, self.destroy).grid(row=4, column=0, columnspan=2, sticky="we", pady=(12, 0))
        self.show(spins["width"], self._ok)

    def _ok(self):
        try:
            fps = max(1.0, min(120.0, float(self.vars["fps"].get().replace(",", "."))))
        except ValueError:
            fps = None
        self.result = (even(_int(self.vars["width"].get(), 16, 16384, 1280)),
                       even(_int(self.vars["height"].get(), 16, 16384, 720)), fps)
        self.destroy()


class GotoDialog(Dialog):
    """Go to an exact time or frame. The two fields follow each other; .result is the time in seconds."""

    def __init__(self, app):
        super().__init__(app.root, "dlg.goto")
        self.fps = app.project.fps
        self.last_frame = round(app._max_time() * self.fps)
        self.source = "time"                                # the field the user changed last, it decides on OK
        self._busy = False                                  # True while one field is being filled from the other
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        self.time_var = tk.StringVar(value=fmt_time(app.t))
        self.frame_var = tk.StringVar(value=str(round(app.t * self.fps)))
        ttk.Label(frm, text=t("field.time")).grid(row=0, column=0, sticky="w", pady=4)
        self.time_entry = ttk.Entry(frm, textvariable=self.time_var, width=14)
        self.time_entry.grid(row=0, column=1, padx=(12, 0))
        ttk.Label(frm, text=t("field.frame")).grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=self.frame_var, width=14).grid(row=1, column=1, padx=(12, 0))
        hint = t("hint.goto", last=self.last_frame, time=fmt_time(self.last_frame / self.fps))
        ttk.Label(frm, text=hint, wraplength=280, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ok_cancel(frm, self._ok, self.destroy).grid(row=3, column=0, columnspan=2, sticky="we", pady=(12, 0))
        self.time_var.trace_add("write", lambda *a: self._follow("time"))
        self.frame_var.trace_add("write", lambda *a: self._follow("frame"))
        self.time_entry.selection_range(0, "end")
        self.show(self.time_entry, self._ok)

    def _frame_of(self, text):
        """@return frame number (limited to the picture range) of the text of the Frame field; None if unusable"""
        try:
            return min(max(int(float(text)), 0), self.last_frame)
        except (ValueError, OverflowError):
            return None

    def _follow(self, source):
        """The field `source` was edited: show the same position in the other field (if the text is usable)."""
        if self._busy:
            return
        self.source = source
        self._busy = True
        try:
            if source == "time":
                seconds = parse_time(self.time_var.get())
                if seconds is not None:
                    self.frame_var.set(str(min(round(seconds * self.fps), self.last_frame)))
            else:
                frame = self._frame_of(self.frame_var.get())
                if frame is not None:
                    self.time_var.set(fmt_time(frame / self.fps))
        finally:
            self._busy = False

    def _ok(self):
        if self.source == "time":
            seconds = parse_time(self.time_var.get())
        else:
            frame = self._frame_of(self.frame_var.get())
            seconds = None if frame is None else frame / self.fps
        if seconds is None:
            self.bell()                                     # not a time / frame: stay open so it can be fixed
            return
        self.result = seconds
        self.destroy()


class ExportDialog(Dialog):
    """Pick format, quality and file; OK puts the export into the queue (result True) and closes."""

    def __init__(self, app):
        super().__init__(app.root, "dlg.export")
        self.app = app
        self.presets = ff.available_presets(app.tools)
        cfg = app.cfg
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        codes = list(self.presets)
        self.format_var, self.quality_var, self.path_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ttk.Label(frm, text=t("field.format")).grid(row=0, column=0, sticky="w", pady=4)
        self.format_box = ttk.Combobox(frm, textvariable=self.format_var, state="readonly",
                                       values=[self.presets[c].label for c in codes], width=24)
        self.format_box.grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.format_box.current(codes.index(cfg["export_format"]) if cfg["export_format"] in codes else 0)
        self.format_box.bind("<<ComboboxSelected>>", self._format_changed)
        ttk.Label(frm, text=t("field.quality")).grid(row=1, column=0, sticky="w", pady=4)
        self.quality_box = ttk.Combobox(frm, textvariable=self.quality_var, state="readonly", width=24,
                                        values=[t("quality." + q) for q in ff.QUALITIES])
        self.quality_box.grid(row=1, column=1, sticky="w", padx=(12, 0))
        self.quality_box.current(ff.QUALITIES.index(cfg["export_quality"]))
        ttk.Label(frm, text=t("field.output")).grid(row=2, column=0, sticky="w", pady=4)
        self.path_entry = ttk.Entry(frm, textvariable=self.path_var, width=44)
        self.path_entry.grid(row=2, column=1, sticky="we", padx=(12, 4))
        ttk.Button(frm, text=t("btn.pick"), command=self._pick).grid(row=2, column=2)
        project = app.project
        ttk.Label(frm, text=t("export.info", width=project.width, height=project.height,
                              fps=fmt_number(project.fps, 3), seconds=fmt_number(project.duration(), 2))).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        buttons_row(frm, ("btn.queue", lambda: self._ok("add")), ("btn.start", lambda: self._ok("start")),
                    ("btn.cancel", self.destroy)).grid(row=4, column=0, columnspan=3, sticky="we", pady=(12, 0))
        self._format_changed()
        self.show(self.path_entry, lambda: self._ok("add"))

    def _preset_code(self):
        i = self.format_box.current()
        return list(self.presets)[max(i, 0)]

    def _format_changed(self, _event=None):
        """Keep the file extension in step with the format."""
        path = self.path_var.get()
        ext = self.presets[self._preset_code()].ext
        if not path:
            base = os.path.splitext(os.path.basename(self.app.project_path or ""))[0] or "video"
            folder = self.app.cfg["last_dir"] or os.path.expanduser("~")
            path = os.path.join(folder, base + ext)
        self.path_var.set(os.path.splitext(path)[0] + ext)

    def _pick(self):
        preset = self.presets[self._preset_code()]
        path = filedialog.asksaveasfilename(parent=self, title=t("dlg.export"), defaultextension=preset.ext,
                                            initialfile=os.path.basename(self.path_var.get()),
                                            initialdir=os.path.dirname(self.path_var.get()) or None,
                                            filetypes=[(preset.label, "*" + preset.ext)])
        if path:
            self.path_var.set(path)

    def _ok(self, action):
        """
        Compile the project as it is now and queue the export.

        @param action  "add" puts the job in the queue; "start" also starts the queue
        """
        app, path = self.app, self.path_var.get().strip()
        if not path:
            messagebox.showerror(t("dlg.error"), t("msg.output_required"), parent=self)
            return
        if os.path.exists(path) and not messagebox.askyesno(t("dlg.export"), t("msg.overwrite", name=os.path.basename(path)),
                                                            parent=self):
            return
        try:
            job = compile_export(app.project)
        except CompileError as exc:
            messagebox.showerror(t("dlg.error"), str(exc), parent=self)
            return
        code, quality = self._preset_code(), ff.QUALITIES[max(self.quality_box.current(), 0)]
        try:
            app.jobs.add(ff.Export(app.tools, job, self.presets[code], quality, path))
        except QueueError:
            messagebox.showerror(t("dlg.error"), t("msg.job_duplicate", name=os.path.basename(path)), parent=self)
            return
        app.cfg.update(export_format=code, export_quality=quality, last_dir=os.path.dirname(path))
        app.save_config()
        self.result, self.queued_name = action, os.path.basename(path)
        self.destroy()
