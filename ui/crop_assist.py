"""
ui/crop_assist.py - The crop assist: a window in which a cut is set with the
mouse, like the crop of a phone gallery. It shows the picture of the preview (made by the preview cache, see
PreviewEngine.render_still) with a box on it. Drag the corners or edges of the box, drag inside it to move it, choose
a ratio and turn the box between
portrait and landscape. All the geometry is in engine/cropbox.py; this module only turns mouse positions into it.

It serves two cuts:
  * a Crop effect (CropAssist(app, layer_id, index)): the box lies on the picture of the project, Apply writes the four
    cuts into the effect in one undo step.
  * the crop of the whole video (CropAssist(app, None, None), Edit > Crop video): the box lies on the whole frame before
    the crop, Apply sets the project crop in one undo step, and the picture of the project (preview, export) becomes
    the size that is left, like in HandBrake. The Auto button finds black bars (engine/autocrop.py).
"""

import copy
import math
import threading
import tkinter as tk
from tkinter import font as tkfont, ttk

from lang import t
from theme import resolve_color

from engine import effect as fx
from engine.compiler import CompileError
from engine import autocrop
from engine.cropbox import CropBox
from engine.ffmpeg import FFmpegError
from engine.project import even, normalize_cuts
from ui.common import Dialog, Tip, ok_cancel

HANDLE = 14
"""Largest distance in pixels between the mouse and an edge or corner of the box for it to take it."""

MARGIN = 16
"""Space in pixels around the picture, so that handles on its border can be taken."""

SIZE = (780, 440)
"""Start size of the drawing area."""

REDRAW_DELAY = 120
"""Milliseconds without a change of the window size before the picture is made again."""

RATIOS = ((1, 1), (5, 4), (4, 3), (3, 2), (16, 9), (21, 9))
"""Shapes offered in the ratio list after Free and Original, as (long side, short side)."""

CURSORS = {"move": "fleur", "n": "top_side", "s": "bottom_side", "w": "left_side", "e": "right_side",
           "nw": "top_left_corner", "ne": "top_right_corner", "sw": "bottom_left_corner",
           "se": "bottom_right_corner"}
"""Mouse pointer over each part of the box (names that Tk knows on every system)."""


def ratio_text(width, height):
    """@return the shape of a width x height box as "16:9" or "9:16" (small whole numbers, close is enough), else the
    smaller side as 1: "1:1.43" (portrait) or "1.43:1" (landscape)"""
    if width <= 0 or height <= 0:
        return "0:0"
    r = width / float(height)
    for b in range(1, 21):
        a = int(round(r * b))
        if a >= 1 and abs(a / float(b) - r) / r < 0.005:
            g = math.gcd(a, b)
            return "%d:%d" % (a // g, b // g)
    return "%.2f:1" % r if r >= 1 else "1:%.2f" % (1 / r)


class CropAssist(Dialog):
    """
    The crop assist window for one Crop effect, or for the crop of the whole video; build it and it blocks until it is
    closed (Apply writes the values).
    """

    def __init__(self, app, layer_id, index):
        """
        @param app       the App (project, preview engine, colors, set_params, set_crop)
        @param layer_id  id of the layer that holds the effect; None = the crop of the whole video (project crop)
        @param index     index of the Crop effect in layer.effects; None for the project crop
        """
        self.project_mode = layer_id is None
        super().__init__(app.root, "crop_assist.title_video" if self.project_mode else "crop_assist.title")
        from effect.crop import CUT_KEYS                            # here: a broken plugin must not stop the program
        self.app, self.layer_id, self.index = app, layer_id, index
        self.cut_keys = CUT_KEYS
        project = app.project
        # the box lies on the frame the cut is counted from: the whole frame for the project crop, the picture that is
        # left after it (the frame of the effects) for a Crop effect
        self.frame = (project.width, project.height) if self.project_mode else (project.out_width, project.out_height)
        self.box = CropBox(*self.frame)
        if self.project_mode:
            self.box.set_cuts(*project.cuts())
        else:
            layer = project.find(layer_id)
            values = fx.get(layer.effects[index].effect_id).normalize(layer.effects[index].values)
            self.box.set_cuts(*(values[key] for key in CUT_KEYS))
        self._image, self._image_size, self._bad = None, None, None   # picture, its size, error text
        self._timer = self._busy = self._want = None
        self._detecting = False                                     # True while the Auto button's search runs
        self._lock = threading.Lock()                               # guards _want and _busy (worker thread <-> Tk)
        self._ratios = self._ratio_list(self.frame)
        self._colors()
        self._build()
        self._restore_ratio()
        self.minsize(720, 420)
        self.bind("<Configure>", self._configured, add="+")
        self.show(self.canvas, self._apply)

    # ------------------------------------------------------------------ #
    # Window
    # ------------------------------------------------------------------ #

    def _colors(self):
        """Colors of the drawing: the theme's, with plain fallbacks."""
        self.col_back = self.app.col_bg
        self.col_text = self.app.col_info
        self.col_err = self.app.col_err
        self.col_accent = resolve_color(self, "palette", "select_bg") or "#4b9eff"

    @staticmethod
    def _ratio_list(frame):
        """
        @param frame  (width, height) of the frame the box lies on
        @return [(text, width / height or None, config key)] for the ratio list: Free, Original, then the usual shapes
        """
        out = [(t("crop_assist.ratio_free"), None, "free"),
               (t("crop_assist.ratio_original"), frame[0] / float(frame[1]), "original")]
        return out + [("%d:%d" % shape, shape[0] / float(shape[1]), "%d:%d" % shape) for shape in RATIOS]

    def _build(self):
        """Make the widgets: the bar on top, the drawing area and the buttons."""
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)
        ok_cancel(frm, self._apply, self.destroy, "btn.apply").pack(side="bottom", fill="x", pady=(10, 0))
        top = ttk.Frame(frm)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text=t("crop_assist.ratio")).pack(side="left")
        self.ratio_box = ttk.Combobox(top, state="readonly", width=12, values=[item[0] for item in self._ratios])
        self.ratio_box.current(0)
        self.ratio_box.pack(side="left", padx=(6, 12))
        self.ratio_box.bind("<<ComboboxSelected>>", self._ratio_chosen)
        self.turn = ttk.Button(top, text=t("crop_assist.rotate"), width=8, command=self._turn)
        self.turn.pack(side="left")
        Tip(self.turn, "crop_assist.rotate_tip")
        ttk.Button(top, text=t("crop_assist.reset"), command=self._reset).pack(side="right")
        if self.project_mode:                                       # black bars are found for the whole video only
            self.auto = ttk.Button(top, text=t("crop_assist.auto"), command=self._auto)
            self.auto.pack(side="right", padx=(0, 6))
            Tip(self.auto, "crop_assist.auto_tip")
        self.canvas = tk.Canvas(frm, bg=self.col_back, highlightthickness=0, width=SIZE[0], height=SIZE[1])
        self.canvas.pack(fill="both", expand=True)
        for seq, command in (("<ButtonPress-1>", self._press), ("<B1-Motion>", self._drag),
                             ("<ButtonRelease-1>", self._release), ("<Motion>", self._hover)):
            self.canvas.bind(seq, command)
        for key, dx, dy in (("Left", -1, 0), ("Right", 1, 0), ("Up", 0, -1), ("Down", 0, 1)):
            self.canvas.bind("<%s>" % key, lambda e, a=dx, b=dy: self._nudge(a, b, 1))
            self.canvas.bind("<Shift-%s>" % key, lambda e, a=dx, b=dy: self._nudge(a, b, 10))
        self._bold = tkfont.nametofont("TkDefaultFont").copy()
        self._bold.configure(weight="bold")
        self.info = ttk.Frame(frm)
        self.info.pack(fill="x", pady=(6, 0))
        self.info_values = {}
        for key in ("size", "position", "ratio"):
            ttk.Label(self.info, text=t("crop_assist.info_" + key), font=self._bold).pack(side="left")
            self.info_values[key] = ttk.Label(self.info, text="")
            self.info_values[key].pack(side="left", padx=(4, 16))
        self.note = ttk.Label(self.info, text="", foreground=self.col_err)   # result of the Auto button
        self.note.pack(side="right")
        layer = None if self.project_mode else self.app.project.find(self.layer_id)
        if layer is not None and not layer.active_at(self.app.t, self.app.project):
            self.status = ttk.Label(frm, text=t("crop_assist.not_visible"), wraplength=SIZE[0], justify="left",
                                    foreground=self.col_err)
            self.status.pack(fill="x")
        else:
            self.status = ttk.Frame(frm)
            self.status.pack(fill="x")
            ttk.Label(self.status, text=t("crop_assist.hint_title"), font=self._bold).pack(side="left")
            ttk.Label(self.status, text=t("crop_assist.hint_video" if self.project_mode else "crop_assist.hint")
                      ).pack(side="left", padx=(4, 0))

    # ------------------------------------------------------------------ #
    # Picture
    # ------------------------------------------------------------------ #

    def _picture_size(self):
        """@return (width, height) of the picture that fits the drawing area (even, never above the frame the box lies on)"""
        cw, ch = max(self.canvas.winfo_width(), 64), max(self.canvas.winfo_height(), 64)
        scale = min((cw - 2 * MARGIN) / float(self.frame[0]), (ch - 2 * MARGIN) / float(self.frame[1]), 1.0)
        scale = max(scale, 0.01)
        return even(self.frame[0] * scale), even(self.frame[1] * scale)

    def _layout(self):
        """@return (x, y, width, height) of the picture on the canvas, centred"""
        width, height = self._picture_size()
        return (self.canvas.winfo_width() - width) / 2.0, (self.canvas.winfo_height() - height) / 2.0, width, height

    def _configured(self, event):
        """The window got another size: draw again, and make the picture for the new size."""
        if event.widget is self.canvas or event.widget is self:
            self._redraw()
            self._picture_later()

    def _picture_later(self, delay=REDRAW_DELAY):
        """Make the picture (again) after a short quiet time, so a resize of the window asks only once."""
        if self._timer:
            self.after_cancel(self._timer)
        self._timer = self.after(delay, self._picture_now)

    def _still_project(self):
        """
        @return a copy of the project for the picture: for a Crop effect the layer is on and the effect has no cut, so
                the picture is what the cut is made from (other effects stay as they are); for the project crop the
                project has no crop, so the picture is the whole frame
        """
        project = copy.deepcopy(self.app.project)
        if self.project_mode:
            project.crop = (0, 0, 0, 0)
            return project
        layer = project.find(self.layer_id)
        layer.enabled = True
        inst = layer.effects[self.index]
        inst.enabled = True
        inst.values.update({key: 0 for key in self.cut_keys})
        return project

    def _picture_now(self):
        """Ask for the picture; one worker thread makes them one after the other and only the newest request counts."""
        self._timer = None
        with self._lock:
            self._want = (self._picture_size(), self._still_project())
            if self._busy:
                return
            self._busy = True
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self):
        """Thread: render the wanted picture until there is no newer wish."""
        while True:
            with self._lock:
                job, self._want = self._want, None
                if job is None:
                    self._busy = False
                    return
            size, project = job
            try:
                path, error = self.app.engine.render_still(project, self.app.t, size, self.layer_id), None
            except (FFmpegError, CompileError) as exc:
                path, error = None, str(exc)
            self.app.inbox.put(lambda p=path, e=error, s=size: self._arrived(p, e, s))

    def _arrived(self, path, error, size):
        """Tk thread: a picture is ready (or failed); it is only shown if it is still the wanted one."""
        if not self.winfo_exists() or size != self._picture_size():
            return
        self._bad = error.splitlines()[-1] if error and error.strip() else error
        self._image = None
        if path:
            try:
                self._image, self._image_size = tk.PhotoImage(file=path), size
            except tk.TclError as exc:
                self._bad = str(exc)
        self._redraw()

    # ------------------------------------------------------------------ #
    # Drawing
    # ------------------------------------------------------------------ #

    def _to_canvas(self, x, y):
        """@return canvas position of a frame position"""
        left, top, width, height = self._layout()
        return left + x * width / self.box.width, top + y * height / self.box.height

    def _to_frame(self, x, y):
        """@return frame position of a canvas position"""
        left, top, width, height = self._layout()
        return (x - left) * self.box.width / width, (y - top) * self.box.height / height

    def _line(self, points, width=2, **options):
        """A white line with a dark edge, so it can be seen on any picture."""
        self.canvas.create_line(*points, fill="black", width=width + 2, **options)
        self.canvas.create_line(*points, fill="white", width=width, **options)

    def _redraw(self):
        """Draw the picture (or an empty frame while there is none), the dimmed outside, the box and its handles."""
        c = self.canvas
        c.delete("all")
        left, top, width, height = self._layout()
        c.create_rectangle(left, top, left + width, top + height, fill="#202020", outline="#404040")
        if self._image is not None and self._image_size == (width, height):
            c.create_image(left, top, image=self._image, anchor="nw")
        else:
            c.create_text(left + width / 2.0, top + height / 2.0, fill=self.col_text,
                          text=self._bad or t("crop_assist.loading"))
        x0, y0 = self._to_canvas(self.box.x0, self.box.y0)
        x1, y1 = self._to_canvas(self.box.x1, self.box.y1)
        for rect in ((left, top, left + width, y0), (left, y1, left + width, top + height),
                     (left, y0, x0, y1), (x1, y0, left + width, y1)):
            if rect[2] > rect[0] and rect[3] > rect[1]:
                c.create_rectangle(*rect, fill="black", stipple="gray50", outline="")
        for k in (1, 2):                                            # thirds
            self._line((x0 + (x1 - x0) * k / 3.0, y0, x0 + (x1 - x0) * k / 3.0, y1), 1)
            self._line((x0, y0 + (y1 - y0) * k / 3.0, x1, y0 + (y1 - y0) * k / 3.0), 1)
        self._line((x0, y0, x1, y0, x1, y1, x0, y1, x0, y0), 2)
        arm = min(22.0, (x1 - x0) / 3.0, (y1 - y0) / 3.0)          # corner brackets
        for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            self._line((cx + sx * arm, cy, cx, cy, cx, cy + sy * arm), 4)
        bar = min(14.0, (x1 - x0) / 6.0, (y1 - y0) / 6.0)          # edge handles
        mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        self._line((mx - bar, y0, mx + bar, y0), 4)
        self._line((mx - bar, y1, mx + bar, y1), 4)
        self._line((x0, my - bar, x0, my + bar), 4)
        self._line((x1, my - bar, x1, my + bar), 4)
        self._update_info()

    def _cuts(self):
        """@return (left, right, top, bottom) of the box as they will be used: the project crop makes them even"""
        cuts = self.box.cuts()
        return normalize_cuts(cuts, *self.frame) if self.project_mode else cuts

    def _update_info(self):
        """Show the size, the place and the shape of the box."""
        left, right, top, bottom = self._cuts()
        width, height = self.box.width - left - right, self.box.height - top - bottom
        self.info_values["size"].configure(text="%dx%d" % (width, height))
        self.info_values["position"].configure(text="x:%d, y:%d" % (left, top))
        self.info_values["ratio"].configure(text=ratio_text(width, height))

    # ------------------------------------------------------------------ #
    # Mouse and keys
    # ------------------------------------------------------------------ #

    def _zone(self, x, y):
        """
        @param x  canvas position of the mouse
        @param y  same
        @return   the part of the box under the mouse: "move", "n", "se" ... (see engine.cropbox.ZONES); None if far off
        """
        x0, y0 = self._to_canvas(self.box.x0, self.box.y0)
        x1, y1 = self._to_canvas(self.box.x1, self.box.y1)
        hx, hy = min(HANDLE, (x1 - x0) / 3.0), min(HANDLE, (y1 - y0) / 3.0)   # the middle third can always be taken
        if not (x0 - HANDLE <= x <= x1 + HANDLE and y0 - HANDLE <= y <= y1 + HANDLE):
            return None
        west = abs(x - x0) <= max(hx, 1) and abs(x - x0) <= abs(x - x1)
        east = abs(x - x1) <= max(hx, 1) and not west
        north = abs(y - y0) <= max(hy, 1) and abs(y - y0) <= abs(y - y1)
        south = abs(y - y1) <= max(hy, 1) and not north
        return (("n" if north else "s" if south else "") + ("w" if west else "e" if east else "")) or (
            "move" if x0 < x < x1 and y0 < y < y1 else None)

    def _press(self, event):
        self.canvas.focus_set()
        zone = self._zone(event.x, event.y)
        if zone:
            self.box.begin(zone, *self._to_frame(event.x, event.y))

    def _drag(self, event):
        if self.box.dragging:
            self.box.drag_to(*self._to_frame(event.x, event.y))
            self._changed()

    def _release(self, _event):
        self.box.end()
        self._hover(_event)

    def _hover(self, event):
        """The pointer shows what a drag would do."""
        zone = None if self.box.dragging else self._zone(event.x, event.y)
        self.canvas.configure(cursor=CURSORS.get(zone, ""))

    def _nudge(self, dx, dy, step):
        """Arrow keys move the box (Shift: ten times as far)."""
        self.box.nudge(dx * step, dy * step)
        self._changed()
        return "break"

    def _changed(self):
        """The box changed: draw it again."""
        self._redraw()

    # ------------------------------------------------------------------ #
    # Buttons
    # ------------------------------------------------------------------ #

    def _restore_ratio(self):
        """Select the ratio of the last use (config "crop_ratio") and lock the box to it; Free if it is unknown here."""
        keys = [item[2] for item in self._ratios]
        key = self.app.cfg.get("crop_ratio")
        self.ratio_box.current(keys.index(key) if key in keys else 0)
        self.box.set_ratio(self._ratios[self.ratio_box.current()][1])

    def _ratio_chosen(self, _event=None):
        """A ratio of the list: the box becomes the largest of that shape inside the box now (Free keeps the box); remembered."""
        _text, ratio, key = self._ratios[max(self.ratio_box.current(), 0)]
        self.box.set_ratio(ratio)
        if self.app.cfg.get("crop_ratio") != key:
            self.app.cfg["crop_ratio"] = key
            self.app.save_config()
        self._changed()

    def _turn(self):
        """Portrait <-> landscape."""
        self.box.flip()
        self._changed()

    def _reset(self):
        """The whole frame again. The ratio (the list and the box) is not touched."""
        self.box.reset()
        self._changed()

    def _auto(self):
        """The Auto button: look for black bars on pictures of the whole project, in a thread (it renders a few frames)."""
        if self._detecting:
            return
        if not self.app.tools:
            self.note.configure(text=t("msg.no_ffmpeg"))
            return
        self._detecting = True
        self.auto.configure(state="disabled", text=t("crop_assist.auto_busy"))
        project, tools = copy.deepcopy(self.app.project), self.app.tools

        def work():
            try:
                cuts, error = autocrop.detect(tools, project), None
            except (FFmpegError, CompileError) as exc:
                cuts, error = None, str(exc)
            self.app.inbox.put(lambda: self._detected(cuts, error))

        threading.Thread(target=work, daemon=True).start()

    def _detected(self, cuts, error):
        """Tk thread: the search is over; put the bars it found into the box (nothing found = the whole frame)."""
        if not self.winfo_exists():
            return
        self._detecting = False
        self.auto.configure(state="normal", text=t("crop_assist.auto"))
        if error:
            self.note.configure(text=error.splitlines()[-1] if error.strip() else error)
        else:
            self.box.set_cuts(*cuts)                                # the ratio becomes free: show it so
            self.ratio_box.current(0)
            self.note.configure(text="" if any(cuts) else t("crop_assist.auto_none"))
        self._redraw()

    def _apply(self):
        """Write the cuts into the effect, or set the crop of the project (one undo step), and close."""
        if self.project_mode:
            self.app.set_crop(self.box.cuts())
        else:
            values = dict(zip(self.cut_keys, self.box.cuts()))
            self.app.set_params(self.layer_id, self.index, values, enable=True)
        self.destroy()
