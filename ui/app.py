"""ui/app.py - The main window: wires the panels, the project model, undo and the preview together."""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from chloe_lang import list_languages, resolve_language, set_language, t
from chloe_theme import apply_theme, resolve_color, set_theme

from engine import APP_NAME, VERSION
from engine import ffmpeg as ff
from engine.compiler import CompileError
from engine.config import push_recent, save_config
from engine.preview import PreviewEngine, save_frame
from engine.project import (PROJECT_EXT, AdjustmentLayer, EffectInstance, History, Project, ProjectError, even,
                            load_project, save_project)
from ui.common import IS_WIN, Tip, fmt_time
from ui.dialogs import AboutDialog, ExportDialog, ProjectDialog, SettingsDialog
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
        self.t, self._seq, self._job, self._img = 0.0, 0, None, None
        self._time_lock, self.compare = False, False
        self.inbox = queue.Queue()                  # functions from worker threads, run in the Tk thread
        self.tools = ff.locate(cfg["ffmpeg"])
        self.engine = PreviewEngine(self.tools, lambda seq, path, err: self.inbox.put(
            lambda: self._on_result(seq, path, err)), cfg["cache_dir"])
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
        self.canvas.bind("<Configure>", lambda e: self.request_preview(200))
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())
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
        add(file_menu, "menu.save_frame", self.save_frame_dialog, "e", True)
        file_menu.add_separator()
        add(file_menu, "menu.settings", lambda: SettingsDialog(self), ",")
        add(file_menu, "menu.quit", self.quit, "q")
        edit = tk.Menu(bar, tearoff=0)
        add(edit, "menu.undo", self.undo, "z")
        add(edit, "menu.redo", self.redo, "y")
        root.bind("<%s-Shift-Z>" % mod, lambda e: self._key(self.redo))
        edit.add_separator()
        add(edit, "menu.project", lambda: self.project_settings())
        add(edit, "menu.duplicate", self.duplicate_layer, "d")
        bar.add_cascade(label=t("menu.file"), menu=file_menu)
        bar.add_cascade(label=t("menu.edit"), menu=edit)
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
        self.request_preview(10)

    def run(self, path=None):
        if path and os.path.isfile(path):
            self.open_path(path)
        self.root.mainloop()

    def quit(self):
        if not self.confirm_discard():
            return
        try:
            zoomed = self.root.state() == "zoomed" if IS_WIN or IS_MAC else bool(self.root.attributes("-zoomed"))
        except tk.TclError:
            zoomed = False
        self.cfg["zoomed"] = zoomed
        if not self.cfg["zoomed"]:
            self.cfg["geometry"] = self.root.geometry()
        self.save_config()
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

    def _scale_moved(self, raw):
        fps = self.project.fps
        t = min(max(round(float(raw) * fps) / fps, 0.0), self._max_time())
        if t != self.t:
            self.t = t
            self._time_text()
            self.request_preview()

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
        self._seq = self.engine.request(self.project, self.t, self.preview_size(), self.sel_layer, self.compare)

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
        self.canvas.delete("frame")
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                 image=self._img, tags="frame")
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
        self.layers.refresh(self.project, self.sel_layer)

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

    def project_settings(self):
        result = ProjectDialog(self).result
        if result:
            width, height, fps = result
            self.project.width, self.project.height = width, height
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

    def save_as(self):
        path = filedialog.asksaveasfilename(parent=self.root, title=t("dlg.save"), defaultextension=PROJECT_EXT,
                                            filetypes=[(APP_NAME, "*" + PROJECT_EXT)],
                                            initialdir=self.cfg["last_dir"] or None)
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
            ExportDialog(self)

    def save_frame_dialog(self):
        """Save the picture at the current second in full project size (PNG or JPEG)."""
        if not self.tools:
            messagebox.showwarning(t("dlg.error"), t("msg.no_ffmpeg"), parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title=t("menu.save_frame"), defaultextension=".png",
                                            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg")],
                                            initialdir=self.cfg["last_dir"] or None)
        if not path:
            return
        project, seconds, tools = self.project, self.t, self.tools
        self._say(t("status.saving_frame"))

        def work():
            try:
                save_frame(tools, project, seconds, path)
                message = (t("msg.frame_saved", path=path), False)
            except (ff.FFmpegError, CompileError) as exc:
                message = (t("msg.frame_failed", error=exc), True)
            self.inbox.put(lambda: self._say(*message))

        threading.Thread(target=work, daemon=True).start()
