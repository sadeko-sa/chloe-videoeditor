"""
ui/export.py - The export dialog: container, video, dimension, audio and advanced options, queued as a job.
Every field explains itself in a tooltip (language entries "tip.export.<field>"), there are no help texts in the window.
"""

import os
import tkinter as tk
from dataclasses import replace
from tkinter import filedialog, messagebox, ttk

from lang import t

from engine import encode as enc
from engine.compiler import CompileError, compile_export
from engine.ffmpeg import Export
from engine.jobs import QueueError
from ui.common import Dialog, Tip, buttons_row, fmt_number


class Choice:
    """A read-only drop-down list that shows labels and hands out codes."""

    def __init__(self, parent, width=28, on_change=None):
        """@param on_change  called without arguments after the user picked an entry"""
        self.box = ttk.Combobox(parent, state="readonly", width=width)
        self._codes = []
        if on_change:
            self.box.bind("<<ComboboxSelected>>", lambda _e: on_change())

    def set_items(self, items):
        """@param items  (code, label) pairs; the selection is cleared"""
        self._codes = [code for code, _label in items]
        self.box.configure(values=[label for _code, label in items])
        self.box.set("")

    def select(self, code):
        """Show the entry with this code (the first one if there is none like it)."""
        if code in self._codes:
            self.box.current(self._codes.index(code))
        elif self._codes:
            self.box.current(0)

    @property
    def code(self):
        """@return code of the selected entry, None if the list is empty"""
        i = self.box.current()
        return self._codes[i] if 0 <= i < len(self._codes) else None

    def enable(self, on):
        self.box.configure(state="readonly" if on and self._codes else "disabled")


def _optional(values):
    """@return (code, label) pairs for a list that may stay unchosen: \"\" first, then the values"""
    return [("", t("export.auto"))] + [(v, v) for v in values]


def _tip(key, *widgets):
    """
    Give several widgets the same tooltip, so the label and its field both answer to the mouse (like in HandBrake).

    @param key      language entry with the help text of the field, "tip.export.<field>"
    @param widgets  widgets that show the tooltip
    """
    for widget in widgets:
        Tip(widget, key)


class ExportDialog(Dialog):
    """
    Pick the export options and the file; Queue / Start put the export into the queue (result "add" / "start").
    The options are kept in the config for the next time.
    """

    def __init__(self, app):
        super().__init__(app.root, "dlg.export")
        self.app, self.encoders = app, app.tools.encoders
        self.last = enc.ExportOptions.from_dict(app.cfg["export"], self.encoders)
        self.presets = enc.available_presets(self.encoders)
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        self.preset = Choice(frm, on_change=self._preset_changed)
        self.container = Choice(frm, on_change=self._reload)
        self.path_var, self.crf_var, self.rate_var = tk.StringVar(), tk.DoubleVar(), tk.StringVar()
        self.bitrate_var, self.two_pass_var = tk.StringVar(), tk.BooleanVar()
        self.fast_var, self.extra_var = tk.BooleanVar(), tk.StringVar()
        self.crf_text = tk.StringVar()
        self._rows(frm, ((t("field.preset"), self.preset.box, "tip.export.preset"),
                         (t("field.container"), self.container.box, "tip.export.container")))
        output_label = ttk.Label(frm, text=t("field.output"))
        output_label.grid(row=2, column=0, sticky="w", pady=4)
        self.path_entry = ttk.Entry(frm, textvariable=self.path_var, width=44)
        self.path_entry.grid(row=2, column=1, sticky="we", padx=(12, 4))
        pick = ttk.Button(frm, text=t("btn.pick"), command=self._pick)
        pick.grid(row=2, column=2)
        _tip("tip.export.output", output_label, self.path_entry, pick)

        self.nb = ttk.Notebook(frm)
        self.nb.grid(row=3, column=0, columnspan=3, sticky="we", pady=(10, 0))
        self._tab_video()
        self._tab_dimension()
        self._tab_audio()
        self._tab_advanced()

        project = app.project
        ttk.Label(frm, text=t("export.info", width=project.width, height=project.height,
                              fps=fmt_number(project.fps, 3), seconds=fmt_number(project.duration(), 2))).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        buttons_row(frm, ("btn.queue", lambda: self._ok("add")), ("btn.start", lambda: self._ok("start")),
                    ("btn.cancel", self.destroy)).grid(row=5, column=0, columnspan=3, sticky="we", pady=(12, 0))

        self.container.set_items([(c, enc.CONTAINERS[c].label) for c in enc.containers(self.encoders)])
        self.preset.set_items([("last", t("export.preset_last"))] +
                              [(k, t("export.preset." + k)) for k in self.presets])
        self.preset.select("last")
        self._apply(self.last)
        self.show(self.path_entry, lambda: self._ok("add"))

    # ------------------------------------------------------------------ #
    # Building
    # ------------------------------------------------------------------ #

    @staticmethod
    def _rows(page, rows, first=0):
        """
        Put (label text, widget) pairs under each other: labels left, widgets stretched in column 1.

        @param rows   (label text, widget) or (label text, widget, tooltip key); a tooltip shows over label and widget
        @param first  grid row of the first pair
        """
        for row, (text, widget, *tip) in enumerate(rows, first):
            label = ttk.Label(page, text=text)
            label.grid(row=row, column=0, sticky="w", pady=4)
            widget.grid(row=row, column=1, sticky="w", padx=(12, 0))
            if tip:
                _tip(tip[0], label, widget)

    def _page(self, key):
        page = ttk.Frame(self.nb, padding=12)
        page.columnconfigure(1, weight=1)
        self.nb.add(page, text=t(key))
        return page

    def _tab_video(self):
        page = self._page("tab.video")
        self.video = Choice(page, on_change=self._codec_changed)
        self.speed, self.tune = Choice(page), Choice(page)
        self.profile, self.level, self.depth = Choice(page), Choice(page), Choice(page)
        self._rows(page, ((t("field.video_codec"), self.video.box, "tip.export.video_codec"),
                          (t("field.speed"), self.speed.box, "tip.export.speed"),
                          (t("field.tune"), self.tune.box, "tip.export.tune"),
                          (t("field.profile"), self.profile.box, "tip.export.profile"),
                          (t("field.level"), self.level.box, "tip.export.level"),
                          (t("field.bit_depth"), self.depth.box, "tip.export.bit_depth")))
        rate_label = ttk.Label(page, text=t("field.rate"))
        rate_label.grid(row=6, column=0, sticky="w", pady=(10, 4))
        _tip("tip.export.rate", rate_label)
        modes = ttk.Frame(page)
        modes.grid(row=6, column=1, sticky="w", padx=(12, 0), pady=(10, 4))
        for key, value in (("rate.quality", enc.RATE_QUALITY), ("rate.bitrate", enc.RATE_BITRATE)):
            radio = ttk.Radiobutton(modes, text=t(key), value=value, variable=self.rate_var, command=self._sync_state)
            radio.pack(side="left", padx=(0, 12))
            _tip("tip.export.rate", radio)
        crf_label = ttk.Label(page, text=t("field.crf"))
        crf_label.grid(row=7, column=0, sticky="w", pady=4)
        crf = ttk.Frame(page)
        crf.grid(row=7, column=1, sticky="w", padx=(12, 0))
        self.crf_scale = ttk.Scale(crf, variable=self.crf_var, length=220, command=self._crf_moved)
        self.crf_scale.pack(side="left")
        self.crf_value = ttk.Label(crf, textvariable=self.crf_text, width=6)
        self.crf_value.pack(side="left", padx=(8, 0))
        _tip("tip.export.crf", crf_label, self.crf_scale, self.crf_value)
        bitrate_label = ttk.Label(page, text=t("field.bitrate"))
        bitrate_label.grid(row=8, column=0, sticky="w", pady=4)
        rate = ttk.Frame(page)
        rate.grid(row=8, column=1, sticky="w", padx=(12, 0))
        self.bitrate_spin = ttk.Spinbox(rate, textvariable=self.bitrate_var, from_=enc.BITRATE_RANGE[0],
                                        to=enc.BITRATE_RANGE[1], increment=500, width=8)
        self.bitrate_spin.pack(side="left")
        self.two_pass_check = ttk.Checkbutton(rate, text=t("check.two_pass"), variable=self.two_pass_var)
        self.two_pass_check.pack(side="left", padx=(12, 0))
        _tip("tip.export.bitrate", bitrate_label, self.bitrate_spin)
        _tip("tip.export.two_pass", self.two_pass_check)

    def _tab_dimension(self):
        page = self._page("tab.dimension")
        self.height = Choice(page)
        self.fps = Choice(page)
        self._rows(page, ((t("field.resolution"), self.height.box, "tip.export.resolution"),
                          (t("field.fps"), self.fps.box, "tip.export.fps")))
        self.height.set_items([(0, t("export.same"))] + [(h, "%dp" % h) for h in enc.HEIGHTS if h])
        self.fps.set_items([("", t("export.same"))] +
                           [(f, fmt_number(enc.fps_value(f), 3)) for f in enc.FPS_CHOICES if f])

    def _tab_audio(self):
        page = self._page("tab.audio")
        self.audio = Choice(page, on_change=self._audio_changed)
        self.audio_rate, self.channels, self.sample_rate = Choice(page), Choice(page), Choice(page)
        self._rows(page, ((t("field.audio_codec"), self.audio.box, "tip.export.audio_codec"),
                          (t("field.audio_bitrate"), self.audio_rate.box, "tip.export.audio_bitrate"),
                          (t("field.channels"), self.channels.box, "tip.export.channels"),
                          (t("field.sample_rate"), self.sample_rate.box, "tip.export.sample_rate")))
        self.channels.set_items([(0, t("export.auto")), (1, t("export.mono")), (2, t("export.stereo"))])
        self.sample_rate.set_items([(0, t("export.auto")), (44100, "44.1 kHz"), (48000, "48 kHz")])

    def _tab_advanced(self):
        page = self._page("tab.advanced")
        self.fast_check = ttk.Checkbutton(page, text=t("check.faststart"), variable=self.fast_var)
        self.fast_check.grid(row=0, column=0, columnspan=2, sticky="w", pady=4)
        _tip("tip.export.faststart", self.fast_check)
        extra_label = ttk.Label(page, text=t("field.extra"))
        extra_label.grid(row=1, column=0, sticky="w", pady=(10, 4))
        extra_entry = ttk.Entry(page, textvariable=self.extra_var, width=44)
        extra_entry.grid(row=1, column=1, sticky="we", padx=(12, 0))
        _tip("tip.export.extra", extra_label, extra_entry)

    # ------------------------------------------------------------------ #
    # Options <-> widgets
    # ------------------------------------------------------------------ #

    def _collect(self):
        """@return the options the widgets show now, made valid"""
        try:
            bitrate = int(float(self.bitrate_var.get()))
        except ValueError:
            bitrate = self.last.bitrate
        return enc.ExportOptions(
            container=self.container.code, video=self.video.code, rate=self.rate_var.get(),
            crf=int(round(self.crf_var.get())), bitrate=bitrate, two_pass=self.two_pass_var.get(),
            speed=self.speed.code or "", tune=self.tune.code or "", profile=self.profile.code or "",
            level=self.level.code or "", bit_depth=self.depth.code or 8, fps=self.fps.code or "",
            height=self.height.code or 0, audio=self.audio.code, audio_bitrate=self.audio_rate.code or 0,
            channels=self.channels.code or 0, sample_rate=self.sample_rate.code or 0,
            faststart=self.fast_var.get(), extra=self.extra_var.get()).normalized(self.encoders)

    def _apply(self, options):
        """Show options: fill every list that depends on the container and the codecs, then every value."""
        o = options.normalized(self.encoders)
        v = enc.VIDEO[o.video]
        self.container.select(o.container)
        self.video.set_items([(c, enc.VIDEO[c].label) for c in enc.video_codecs(o.container, self.encoders)])
        self.video.select(o.video)
        self.speed.set_items([(s, s) for s in v.speeds])
        self.tune.set_items(_optional(v.tunes) if v.tunes else [])
        self.profile.set_items(_optional(v.profiles) if v.profiles else [])
        self.level.set_items(_optional(v.levels) if v.levels else [])
        self.depth.set_items([(8, "8-bit")] + ([(10, "10-bit")] if v.ten_bit else []))
        for box, code in ((self.speed, o.speed), (self.tune, o.tune), (self.profile, o.profile),
                          (self.level, o.level), (self.depth, o.bit_depth)):
            box.select(code)
        self.crf_scale.configure(from_=v.crf[0], to=v.crf[1])
        self.crf_var.set(o.crf)
        self._crf_moved()
        self.rate_var.set(o.rate)
        self.bitrate_var.set(str(o.bitrate))
        self.two_pass_var.set(o.two_pass)
        self.height.select(o.height)
        self.fps.select(o.fps)
        self.audio.set_items([(enc.NO_AUDIO, t("export.no_audio")), (enc.COPY_AUDIO, t("export.audio_copy"))] +
                             [(c, enc.AUDIO[c].label) for c in enc.audio_codecs(o.container, self.encoders)])
        self.audio.select(o.audio)
        lossy = enc.AUDIO[o.audio].bitrates if o.audio in enc.AUDIO else ()
        self.audio_rate.set_items([(b, "%d kb/s" % b) for b in lossy])
        self.audio_rate.select(o.audio_bitrate)
        self.channels.select(o.channels)
        self.sample_rate.select(o.sample_rate)
        self.fast_var.set(o.faststart)
        self.extra_var.set(o.extra)
        self._sync_path(o)
        self._sync_state()

    def _sync_state(self):
        """Grey out what the choices made do not use."""
        o = self._collect()
        v, quality = enc.VIDEO[o.video], o.rate == enc.RATE_QUALITY
        for box in (self.tune, self.profile, self.level):
            box.enable(True)
        self.depth.enable(v.ten_bit)
        for widget in (self.crf_scale, self.crf_value):
            widget.state(["!disabled"] if quality else ["disabled"])
        self.bitrate_spin.state(["disabled"] if quality else ["!disabled"])
        self.two_pass_check.state(["!disabled"] if v.two_pass and not quality else ["disabled"])
        sound = o.audio in enc.AUDIO                  # only an encoder takes bitrate, channels and sample rate
        self.audio_rate.enable(sound)
        self.channels.enable(sound)
        self.sample_rate.enable(sound)
        self.fast_check.state(["!disabled"] if o.container == "mp4" else ["disabled"])

    def _sync_path(self, o):
        """Keep the file extension in step with the container; suggest a file name if there is none."""
        ext = enc.CONTAINERS[o.container].ext
        path = self.path_var.get()
        if not path:
            base = os.path.splitext(os.path.basename(self.app.project_path or ""))[0] or "video"
            path = os.path.join(self.app.cfg["last_dir"] or os.path.expanduser("~"), base + ext)
        self.path_var.set(os.path.splitext(path)[0] + ext)

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #

    def _crf_moved(self, _value=None):
        self.crf_text.set(str(int(round(self.crf_var.get()))))

    def _reload(self):
        """The container changed: codecs that do not fit are replaced, the rest is kept."""
        self._apply(self._collect())

    def _codec_changed(self):
        """A new video codec starts with its own default quality, speed and no tune, profile or level."""
        v = enc.VIDEO[self.video.code]
        self._apply(replace(self._collect(), crf=v.crf[2], speed=v.speed, tune="", profile="", level=""))

    def _audio_changed(self):
        code = self.audio.code
        o = self._collect()
        self._apply(replace(o, audio_bitrate=enc.AUDIO[code].bitrate) if code in enc.AUDIO else o)

    def _preset_changed(self):
        code = self.preset.code
        self._apply(self.last if code == "last" else self.presets[code])

    def _pick(self):
        o = self._collect()
        container = enc.CONTAINERS[o.container]
        path = filedialog.asksaveasfilename(parent=self, title=t("dlg.export"), defaultextension=container.ext,
                                            initialfile=os.path.basename(self.path_var.get()),
                                            initialdir=os.path.dirname(self.path_var.get()) or None,
                                            filetypes=[(container.label, "*" + container.ext)])
        if path:
            self.path_var.set(path)

    def _ok(self, action):
        """
        Compile the project as it is now and queue the export.

        @param action  "add" puts the job in the queue; "start" also starts the queue
        """
        app, path, options = self.app, self.path_var.get().strip(), self._collect()
        if not path:
            messagebox.showerror(t("dlg.error"), t("msg.output_required"), parent=self)
            return
        try:
            options.extra_args()
        except ValueError:
            messagebox.showerror(t("dlg.error"), t("msg.extra_invalid"), parent=self)
            return
        if os.path.exists(path) and not messagebox.askyesno(t("dlg.export"), t("msg.overwrite", name=os.path.basename(path)),
                                                            parent=self):
            return
        try:
            job = compile_export(app.project)
        except CompileError as exc:
            messagebox.showerror(t("dlg.error"), str(exc), parent=self)
            return
        if options.audio == enc.COPY_AUDIO and not job.audio_source:
            messagebox.showerror(t("dlg.error"), t("msg.audio_copy_impossible"), parent=self)
            return
        try:
            app.jobs.add(Export(app.tools, job, options, path))
        except QueueError:
            messagebox.showerror(t("dlg.error"), t("msg.job_duplicate", name=os.path.basename(path)), parent=self)
            return
        app.cfg.update(export=options.to_dict(), last_dir=os.path.dirname(path))
        app.save_config()
        self.result, self.queued_name = action, os.path.basename(path)
        self.destroy()
