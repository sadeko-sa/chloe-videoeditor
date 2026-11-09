"""
ui/jobs.py - The job queue window: the workers and what each one does, and every job with a progress bar
and a colored status. The Start / Stop and the Pause / Resume buttons work on the whole queue; Delete and Clear on the list; the
right-click menu of a job works on that job. A normal window (not modal), so editing goes on.
"""

import os
import shlex
import subprocess
import sys
import tkinter as tk
from tkinter import font as tkfont, messagebox, ttk

from lang import t
from theme import resolve_color

from engine.jobs import JobState, QueueError
from ui.common import IS_WIN, fmt_time, mix_color

REFRESH_MS = 500
"""How often the window is redrawn (progress is polled, not pushed)."""

STATE_COLORS = {JobState.RUNNING: "#3b8eea", JobState.PAUSED: "#d29922", JobState.DONE: "#2ea043",
                JobState.CANCELLED: "#8b949e"}
"""Status colors that read well on light and dark themes. Waiting jobs use the normal text color and failed
jobs the theme's error color (viewer.error_text, fallback ERROR_COLOR)."""

ERROR_COLOR = "#f85149"

IS_MAC = sys.platform == "darwin"

CONTEXT_EVENTS = ("<Button-2>", "<Control-Button-1>") if IS_MAC else ("<Button-3>",)
"""Mouse events that open the right-click menu of a job (the secondary button of a Mac is Button-2)."""

COLUMNS = (("col.job_name", 28), ("col.job_format", 20), ("col.job_progress", None),
           ("col.job_status", 12), ("col.job_worker", 8))
"""Heading key and width in characters of each column; None = the progress bar (BAR_SIZE pixels). Headings
and rows use the same widths, so they line up."""

BAR_SIZE = (150, 16)

CELL_PAD = (4, 2)
"""Grid padding (x, y) of every cell; headings and rows use the same, inside a frame with the same border."""

ROW_BORDER = 2
"""Width of the frame around a row; it shows the selection. The heading row has the same border."""

DETAIL_WIDTH = 280
"""Width in pixels of the details panel; its texts wrap there."""

DETAIL_FIELDS = ("file", "format", "length", "state", "worker", "progress", "elapsed", "remaining", "size", "error")
"""Rows of the details panel, in order; the texts are the language entries detail.<name>."""


def _clock(seconds):
    """@return seconds as h:mm:ss"""
    seconds = int(max(seconds, 0))
    return "%d:%02d:%02d" % (seconds // 3600, seconds // 60 % 60, seconds % 60)


def _size(count):
    """@return a file size in bytes as KB / MB / GB text"""
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "%d %s" % (value, unit) if unit == "B" else "%.1f %s" % (value, unit)
        value /= 1024


def _command_line(args):
    """@return an argument list as one command line text for this platform"""
    return subprocess.list2cmdline(args) if IS_WIN else " ".join(shlex.quote(a) for a in args)


def _open_path(path):
    """Open a file or folder with the program of the system."""
    try:
        if IS_WIN:
            os.startfile(path)
        else:
            subprocess.Popen(["open" if IS_MAC else "xdg-open", path])
    except OSError:
        pass


def _base_colors(widget):
    """@return (background, text) of the current theme, with system like fallbacks"""
    style = ttk.Style(widget)
    return style.lookup("TFrame", "background") or "#d9d9d9", style.lookup("TLabel", "foreground") or "#000000"


class Bar(tk.Canvas):
    """A progress bar drawn by hand, so its color can follow the job status with every ttk theme."""

    def __init__(self, parent):
        bg, self._fg = _base_colors(parent)
        self._trough = mix_color(parent, bg, self._fg, 0.18)
        super().__init__(parent, width=BAR_SIZE[0], height=BAR_SIZE[1], highlightthickness=0, bd=0,
                         background=self._trough)
        self._shown = None
        self.bind("<Configure>", lambda e: self._draw())

    def show(self, fraction, color, text):
        """
        @param fraction  0..1 of the bar that is filled
        @param color     fill color
        @param text      text in the middle of the bar
        """
        state = (round(fraction, 3), color, text)
        if state != self._shown:
            self._shown = state
            self._draw()

    def _draw(self):
        self.delete("all")
        if not self._shown:
            return
        fraction, color, text = self._shown
        width, height = self.winfo_width(), self.winfo_height()
        if fraction > 0:
            self.create_rectangle(0, 0, int(width * fraction), height, fill=color, width=0)
        self.create_text(width // 2, height // 2, text=text, fill=self._fg)


class JobRow:
    """One job in the list: name, format, progress bar, status, worker."""

    def __init__(self, window, job):
        """
        @param window  the JobsWindow (parent, selection, colors)
        @param job     the ExportJob this row shows
        """
        self.job_id = job.id
        bg, _fg = _base_colors(window.body)
        self.frame = tk.Frame(window.body, background=bg, highlightthickness=ROW_BORDER, highlightbackground=bg,
                              highlightcolor=bg)
        self.frame.pack(fill="x")
        self.texts = []
        for column, (_key, width) in enumerate(COLUMNS):
            if width is None:
                self.bar = Bar(self.frame)
                self.bar.grid(row=0, column=column, padx=CELL_PAD[0], pady=CELL_PAD[1], sticky="w")
            else:
                label = ttk.Label(self.frame, width=width, anchor="w")
                label.grid(row=0, column=column, padx=CELL_PAD[0], pady=CELL_PAD[1], sticky="w")
                self.texts.append(label)
        self._bg = bg
        self.name, self.fmt, self.status, self.worker = self.texts
        for widget in (self.frame, self.bar, *self.texts):
            widget.bind("<Button-1>", lambda e: window.select(self.job_id))
            for seq in CONTEXT_EVENTS:
                widget.bind(seq, lambda e: window.show_menu(self.job_id, e))

    def update(self, job, window, selected):
        """Show the current state of the job."""
        color = window.color(job.state)
        self.name.configure(text=job.name)
        self.fmt.configure(text="%s, %s" % (job.export.preset.label, t("quality." + job.export.quality)))
        self.status.configure(text=t("job." + job.state.value), foreground=color or "")
        self.worker.configure(text=str(job.worker) if job.worker else "")
        self.bar.show(job.progress, color or self._bg, "%d%%" % int(job.progress * 100))
        edge = window.select_color if selected else self._bg
        self.frame.configure(highlightbackground=edge, highlightcolor=edge)

    def destroy(self):
        self.frame.destroy()


class JobsWindow(tk.Toplevel):
    """The job queue window, opened with App.show_jobs()."""

    def __init__(self, app):
        """@param app  the App (gives the job queue)"""
        super().__init__(app.root)
        self.app, self._timer, self.selected = app, None, None
        self.rows = {}
        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        self.error_color = resolve_color(self, "viewer", "error_text") or ERROR_COLOR
        self.select_color = resolve_color(self, "palette", "select_bg") or "#3399ff"
        self.title(t("dlg.jobs"))
        self.geometry("1040x480")
        self.minsize(760, 340)
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        buttons = ttk.Frame(frm)
        buttons.pack(side="bottom", fill="x", pady=(8, 0))
        self.run_btn = ttk.Button(buttons, text=t("btn.start"), command=self._toggle_run)
        self.run_btn.pack(side="left")
        self.pause_btn = ttk.Button(buttons, text=t("btn.job_pause"), command=self._toggle_pause)
        self.pause_btn.pack(side="left", padx=(6, 0))
        self.delete_btn = ttk.Button(buttons, text=t("btn.job_delete"), command=self._delete)
        self.delete_btn.pack(side="left", padx=(18, 0))
        ttk.Button(buttons, text=t("btn.job_clear"), command=self._clear).pack(side="left", padx=(18, 0))
        ttk.Button(buttons, text=t("btn.ok"), command=self.destroy).pack(side="right")

        panes = ttk.PanedWindow(frm, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left, right = ttk.Frame(panes), ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(left, weight=1)
        panes.add(right, weight=0)
        self._build_list(left)
        self._build_details(right)
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            self.bind(seq, self._wheel)
        for seq in ("<Escape>", "<Control-w>", "<Control-W>") + (("<Command-w>",) if IS_MAC else ()):
            self.bind(seq, lambda e: self.destroy())
        self.refresh()

    def _build_list(self, parent):
        """The Worker box, the headings and the scrolling list of jobs."""
        self.worker_box = ttk.LabelFrame(parent, text=t("group.workers"), padding=(8, 4))
        self.worker_box.pack(fill="x")
        self.threads_label = ttk.Label(self.worker_box)
        self.threads_label.pack(anchor="w")
        self.worker_labels = {}

        bg = _base_colors(parent)[0]
        head = tk.Frame(parent, background=bg, highlightthickness=ROW_BORDER, highlightbackground=bg)
        head.pack(fill="x", pady=(10, 0))
        for column, (key, width) in enumerate(COLUMNS):
            if width is None:                           # the bar column is sized in pixels, not characters
                cell = ttk.Frame(head, width=BAR_SIZE[0])
                label = ttk.Label(cell, text=t(key), anchor="w")
                label.pack(fill="both", expand=True)
                cell.configure(height=label.winfo_reqheight())
                cell.pack_propagate(False)
            else:
                cell = ttk.Label(head, text=t(key), width=width, anchor="w")
            cell.grid(row=0, column=column, padx=CELL_PAD[0], pady=CELL_PAD[1], sticky="w")
        view = ttk.Frame(parent)
        view.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(view, highlightthickness=0, background=bg)
        scroll = ttk.Scrollbar(view, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, background=bg)
        window = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))

    def _build_details(self, parent):
        """The panel with everything about the selected job."""
        box = ttk.LabelFrame(parent, text=t("detail.title"), padding=8, width=DETAIL_WIDTH)
        box.pack(fill="both", expand=True)
        box.pack_propagate(False)
        self.empty_label = ttk.Label(box, text=t("detail.none"), wraplength=DETAIL_WIDTH - 24, justify="left")
        self.empty_label.pack(anchor="w")
        self.detail_frame = ttk.Frame(box)
        self.detail_names, self.detail_values = {}, {}
        for row, name in enumerate(DETAIL_FIELDS):
            self.detail_names[name] = ttk.Label(self.detail_frame, text=t("detail." + name), font=self.bold)
            self.detail_names[name].grid(row=row, column=0, sticky="nw", pady=2)
            self.detail_values[name] = ttk.Label(self.detail_frame, wraplength=DETAIL_WIDTH - 130, justify="left")
            self.detail_values[name].grid(row=row, column=1, sticky="nw", padx=(10, 0), pady=2)
        self.detail_frame.columnconfigure(1, weight=1)

    # ------------------------------------------------------------------ #

    def _wheel(self, event):
        """Scroll the list with the mouse wheel (Button-4/5 on X11)."""
        step = {4: -1, 5: 1}.get(event.num) or (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(step, "units")

    def color(self, state):
        """@return the color of a job status, \"\" for the normal text color"""
        return self.error_color if state is JobState.FAILED else STATE_COLORS.get(state, "")

    def select(self, job_id):
        self.selected = job_id
        self.refresh()

    def destroy(self):
        if self._timer:
            self.after_cancel(self._timer)
            self._timer = None
        super().destroy()

    # ------------------------------------------------------------------ #

    def refresh(self):
        """Bring workers and list in line with the queue, then look again after REFRESH_MS."""
        queue = self.app.jobs
        threads = queue.threads or t("worker.auto")
        self.threads_label.configure(text=t("worker.threads", threads=threads))
        self._show_workers(queue.workers())
        jobs = queue.jobs()
        self.run_btn.configure(text=t("btn.job_stop" if queue.started else "btn.start"))
        self.pause_btn.configure(text=t("btn.job_resume" if queue.paused else "btn.job_pause"),
                                 state="normal" if queue.started else "disabled")
        for gone in set(self.rows) - {j.id for j in jobs}:
            self.rows.pop(gone).destroy()
        for job in jobs:
            if job.id not in self.rows:
                self.rows[job.id] = JobRow(self, job)
            self.rows[job.id].update(job, self, job.id == self.selected)
        self._show_detail(next((j for j in jobs if j.id == self.selected), None))
        if self._timer:
            self.after_cancel(self._timer)
        self._timer = self.after(REFRESH_MS, self.refresh)

    def _show_workers(self, workers):
        """One line per worker: idle, or the job it renders with its percent."""
        for number in set(self.worker_labels) - {n for n, _job in workers}:
            self.worker_labels.pop(number).destroy()
        for number, job in workers:
            label = self.worker_labels.get(number)
            if label is None:
                label = self.worker_labels[number] = ttk.Label(self.worker_box)
                label.pack(anchor="w")
            if job is None:
                label.configure(text=t("worker.idle", number=number))
            else:
                label.configure(text=t("worker.busy", number=number, name=job.name,
                                       percent=int(job.progress * 100)))

    def _show_detail(self, job):
        """Fill the details panel with the selected job (or the hint if there is none); set the buttons."""
        self.delete_btn.configure(state="normal" if job else "disabled")
        if job is None:
            self.detail_frame.pack_forget()
            self.empty_label.pack(anchor="w")
            return
        self.empty_label.pack_forget()
        self.detail_frame.pack(fill="both", expand=True)
        export, elapsed = job.export, job.elapsed
        remaining = elapsed * (1 - job.progress) / job.progress if job.state is JobState.RUNNING and job.progress >= 0.01 \
            else None
        size = os.path.getsize(job.out_path) if job.state is JobState.DONE and os.path.exists(job.out_path) else None
        values = {
            "file": job.out_path,
            "format": "%s, %s" % (export.preset.label, t("quality." + export.quality)),
            "length": fmt_time(export.job.duration),
            "state": t("job." + job.state.value),
            "worker": str(job.worker) if job.worker else "",
            "progress": "%d%%" % int(job.progress * 100),
            "elapsed": _clock(elapsed) if job.started else "",
            "remaining": _clock(remaining) if remaining is not None else "",
            "size": _size(size) if size is not None else "",
            "error": job.error,
        }
        for name in DETAIL_FIELDS:
            shown = bool(values[name]) or name in ("file", "format", "length", "state", "progress")
            if shown:
                self.detail_names[name].grid()
                self.detail_values[name].grid()
                self.detail_values[name].configure(text=values[name],
                                                   foreground=self.color(job.state) if name == "state" else "")
            else:
                self.detail_names[name].grid_remove()
                self.detail_values[name].grid_remove()

    def _selected_job(self):
        """@return the selected ExportJob, None if nothing is selected"""
        return next((j for j in self.app.jobs.jobs() if j.id == self.selected), None)

    def _copy_command(self):
        """Put the FFmpeg command line of the selected job on the clipboard."""
        job = self._selected_job()
        if job is None:
            return
        self.clipboard_clear()
        self.clipboard_append(_command_line(job.export.command(job.out_path, self.app.jobs.threads)))

    def show_menu(self, job_id, event):
        """
        Right click on a job: select it and pop up its menu. The entries that do not fit the state of the job are
        greyed out; Reset only works once the job has ended, never while it waits, renders or is paused.
        """
        self.select(job_id)
        job = self._selected_job()
        if job is None:
            return
        paused = job.state is JobState.PAUSED
        can_pause = job.state in (JobState.RUNNING, JobState.PAUSED)
        out_exists = job.state is JobState.DONE and os.path.exists(job.out_path)
        folder = os.path.dirname(os.path.abspath(job.out_path))

        def state(enabled):
            return "normal" if enabled else "disabled"

        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=t("btn.job_resume" if paused else "btn.job_pause"), command=self._pause,
                         state=state(can_pause))
        menu.add_command(label=t("btn.job_stop"), command=self._stop, state=state(job.state.active))
        menu.add_command(label=t("btn.job_reset"), command=self._reset, state=state(job.state.resettable))
        menu.add_separator()
        menu.add_command(label=t("btn.job_open"), command=lambda: _open_path(job.out_path), state=state(out_exists))
        menu.add_command(label=t("btn.job_folder"), command=lambda: _open_path(folder),
                         state=state(os.path.isdir(folder)))
        menu.add_command(label=t("btn.job_copy_path"), command=lambda: self._clip(job.out_path))
        menu.add_command(label=t("btn.job_copy_cmd"), command=self._copy_command)
        menu.add_separator()
        menu.add_command(label=t("btn.job_delete"), command=self._delete)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _clip(self, text):
        """Put a text on the clipboard."""
        self.clipboard_clear()
        self.clipboard_append(text)

    def _reset(self):
        """Queue the selected job again (only possible when it is done, failed or stopped)."""
        job = self._selected_job()
        if job is None or not job.state.resettable:
            return
        try:
            self.app.jobs.reset(job.id)
        except QueueError:
            messagebox.showerror(t("dlg.error"), t("msg.job_duplicate", name=job.name), parent=self)
        self.refresh()

    def _toggle_run(self):
        """Start / Stop of the whole queue: Start renders the waiting jobs one after the other; Stop stops every job
        that renders (Reset queues it again) and nothing new is taken until Start."""
        queue = self.app.jobs
        (queue.stop_all if queue.started else queue.start_all)()
        self.refresh()

    def _toggle_pause(self):
        """Pause / Resume of the whole queue: Pause freezes every job that renders and takes nothing new; Resume
        lets them go on."""
        queue = self.app.jobs
        (queue.resume_all if queue.paused else queue.pause_all)()
        self.refresh()

    def _pause(self):
        """Context menu: pause the selected job, or resume it if it is paused."""
        job = self._selected_job()
        if job is not None:
            (self.app.jobs.resume if job.state is JobState.PAUSED else self.app.jobs.pause)(job.id)
            self.refresh()

    def _stop(self):
        if self.selected is not None:
            self.app.jobs.cancel(self.selected)
            self.refresh()

    def _delete(self):
        """Take the selected job out of the list (stopping it first if it is still active)."""
        if self.selected is not None:
            self.app.jobs.remove(self.selected)
            self.selected = None
            self.refresh()

    def _clear(self):
        self.app.jobs.clear_finished()
        self.refresh()
