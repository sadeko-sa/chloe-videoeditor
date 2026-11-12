"""ui/common.py - Small helpers shared by the windows: text lookups, tooltip, modal dialog base, Windows setup."""

import base64
import math
import os
import struct
import subprocess
import sys
import tkinter as tk
import zlib
from tkinter import ttk

from lang import t
from theme import resolve_color

from engine.ffmpeg import IS_WIN

IS_MAC = sys.platform == "darwin"
MOD = "Command" if IS_MAC else "Control"
"""Tk name of the key for shortcuts: Cmd on macOS, else Ctrl."""
MOD_LABEL = "Cmd" if IS_MAC else "Ctrl"
"""The same key as shown in menus."""
POINTER_GAP_X, POINTER_GAP_Y = 12, 8
"""Pixels between the mouse pointer and a message next to it (right of it, above it)."""
POINTER_CURSOR_HEIGHT = 24
"""Pixels a message must stay below the pointer's position so that it does not cover the mouse cursor."""


def _png(width, height, rgba):
    """
    @param width   picture width in pixels
    @param height  picture height in pixels
    @param rgba    bytes, 4 per pixel (R, G, B, alpha), row by row
    @return        the picture as PNG file content
    """
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def ring_frames(root, fg, bg, size=44, steps=12):
    """
    Smooth (anti-aliased, transparent) frames of a loading ring: a dark soft disc, a faint ring and a bright arc with
    round ends that turns from frame to frame. Tk draws no smooth circles on a canvas, so the pictures are made here
    (9 samples per pixel) and the canvas only shows them one after the other.

    @param root   a Tk widget (to turn the color names into numbers)
    @param fg     color of the ring and the arc
    @param bg     color of the disc behind the ring
    @param size   width and height of a frame in pixels
    @param steps  number of frames for one turn
    @return       list of tk.PhotoImage, the arc turns clockwise from one to the next
    """
    fg = tuple(c // 257 for c in root.winfo_rgb(fg))
    bg = tuple(c // 257 for c in root.winfo_rgb(bg))
    mid, radius, width = size / 2.0, size / 2.0 - 5.0, 3.6
    extent, sub = 100.0, 3
    frames = []
    for step in range(steps):
        start = -step * 360.0 / steps                           # degrees, counter clockwise from the right (like Tk)
        ends = [(mid + radius * math.cos(math.radians(a)), mid - radius * math.sin(math.radians(a)))
                for a in (start, start + extent)]
        data = bytearray()
        for py in range(size):
            for px in range(size):
                acc = [0.0, 0.0, 0.0, 0.0]                      # premultiplied R, G, B and alpha
                for sy in range(sub):
                    for sx in range(sub):
                        x, y = px + (sx + 0.5) / sub, py + (sy + 0.5) / sub
                        dx, dy = x - mid, mid - y
                        dist = math.hypot(dx, dy)
                        layers = []
                        if dist <= mid - 1.0:
                            layers.append((bg, 0.62))
                        if abs(dist - radius) <= width / 2.0:
                            layers.append((fg, 0.28))
                            angle = (math.degrees(math.atan2(dy, dx)) - start) % 360.0
                            if angle <= extent:
                                layers.append((fg, 1.0))
                        if any(math.hypot(x - ex, y - ey) <= width / 2.0 for ex, ey in ends):
                            layers.append((fg, 1.0))             # the round ends of the arc
                        r = g = b = a = 0.0
                        for color, alpha in layers:             # each layer over the ones below it
                            r, g, b = (r * (1 - alpha) + color[0] * alpha, g * (1 - alpha) + color[1] * alpha,
                                       b * (1 - alpha) + color[2] * alpha)
                            a = a + alpha * (1 - a)
                        acc[0] += r * a
                        acc[1] += g * a
                        acc[2] += b * a
                        acc[3] += a
                total = sub * sub
                alpha = acc[3] / total
                if acc[3] > 0:
                    data += bytes((int(round(acc[0] / acc[3])), int(round(acc[1] / acc[3])),
                                   int(round(acc[2] / acc[3])), int(round(alpha * 255))))
                else:
                    data += b"\x00\x00\x00\x00"
        frames.append(tk.PhotoImage(master=root, format="png",
                                    data=base64.b64encode(_png(size, size, bytes(data))).decode("ascii")))
    return frames


def detach_console(script):
    """
    Windows: when started with python.exe (console window), start the same program with
    pythonw.exe (no console) and exit. CHLOE_KEEP_CONSOLE=1 keeps the console, e.g. to read errors.

    @param script  path of the main script to start again
    """
    if not IS_WIN or os.environ.get("CHLOE_KEEP_CONSOLE"):
        return
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.basename(exe).lower() != "python.exe" or not os.path.exists(pyw):
        return
    try:
        subprocess.Popen([pyw, os.path.abspath(script)] + sys.argv[1:], creationflags=0x00000008, close_fds=True)
    except OSError:
        return
    sys.exit(0)


def win_setup():
    """Windows: DPI aware (sharp window) and an own taskbar identity. Call before Tk()."""
    if not IS_WIN:
        return
    import ctypes
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ChloeVideoeditor")
    except Exception:
        pass


def bind_shortcut(widget, char, command):
    """
    Run `command` when Ctrl+<char> (Cmd on macOS) is pressed while `widget` has the keyboard focus, with Caps Lock on
    or off. The key does not reach the widget's own bindings.

    @param widget   widget to bind
    @param char     one letter
    @param command  function without arguments
    """
    def run(_event):
        command()
        return "break"

    for key in {char.lower(), char.upper()}:
        widget.bind("<%s-%s>" % (MOD, key), run)


def clipboard_text(widget):
    """@return the text on the clipboard; empty if there is none (or it is no text)"""
    try:
        return widget.clipboard_get()
    except tk.TclError:
        return ""


def set_clipboard(widget, text):
    """Replace the content of the clipboard by `text`."""
    widget.clipboard_clear()
    widget.clipboard_append(text)


def text_or(key, fallback):
    """@return the language text for key, or fallback if the language files have none"""
    text = t(key)
    return fallback if text == key else text


def effect_name(eff):
    return text_or("effect." + eff.id, eff.name)


def param_label(eff, param):
    return text_or("effect.%s.%s" % (eff.id, param.key), param.label)


def group_label(eff, group):
    """@return the text of a parameter group of an effect (lang key effect.<id>.group.<group>, else the group name)"""
    return text_or("effect.%s.group.%s" % (eff.id, group), group)


def choice_label(eff, param, value):
    return text_or("choice.%s.%s.%s" % (eff.id, param.key, value), str(value))


def category_label(category):
    return text_or("category." + category, category.capitalize())


def fmt_time(seconds):
    """@return seconds as mm:ss.mmm"""
    seconds = max(seconds, 0.0)
    return "%02d:%06.3f" % (int(seconds // 60), seconds % 60)


def fmt_frame_name(media_name, seconds, fps, now):
    """
    @param media_name  file name of the video, with extension
    @param seconds     position of the frame in the video
    @param fps         frame rate of the project
    @param now         datetime of the export
    @return            <media_name>-[yyyy.mm.dd-hh.mm.ss]-<mm.ss>-<frame number>- (hh. is added to the position from
                       one hour on)
    """
    total = int(max(seconds, 0.0))
    position = "%02d.%02d" % (total // 60 % 60, total % 60)
    if total >= 3600:
        position = "%02d.%s" % (total // 3600, position)
    return "%s-[%s]-%s-%d-" % (media_name, now.strftime("%Y.%m.%d-%H.%M.%S"), position, round(seconds * fps))


def parse_time(text):
    """
    @param text  seconds ("90"), minutes and seconds ("1:30.5") or hours, minutes and seconds ("1:02:03.5");
                 a comma works as the decimal point
    @return      the time in seconds; None if the text is not a time
    """
    text = text.strip().replace(",", ".")
    parts = text.split(":")
    if not text or len(parts) > 3:
        return None
    seconds = 0.0
    for part in parts:
        try:
            value = float(part)
        except ValueError:
            return None
        if not 0.0 <= value < float("inf"):                 # also rejects nan and negative numbers
            return None
        seconds = seconds * 60 + value
    return seconds


def fmt_number(value, digits=2):
    """@return a number without useless trailing zeros"""
    text = "%.*f" % (digits, value)
    if "." in text:                                 # trim zeros only after a decimal point ("100" must stay "100")
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def mix_color(widget, c0, c1, p):
    """@return color c0 moved the fraction p (0..1) towards c1, as #rrggbb"""
    a, b = widget.winfo_rgb(c0), widget.winfo_rgb(c1)
    return "#%02x%02x%02x" % tuple(int((x + (y - x) * p) / 257) for x, y in zip(a, b))


def highlight_fade(label, entry, hold=100, fade=350, step=30):
    """
    Show that a value was changed by the program (a reset): the label lights up in the theme's selection
    colors and fades back to normal, the number text fades from the accent color. A running fade is restarted.

    @param label  ttk.Label of the field
    @param entry  ttk.Entry showing the value (None = label only)
    @param hold   milliseconds at full color
    @param fade   milliseconds of the fade out
    @param step   milliseconds between two frames
    """
    style = ttk.Style(label)
    base_bg = style.lookup("TLabel", "background") or "#d9d9d9"
    base_fg = style.lookup("TLabel", "foreground") or "#000000"
    accent = (resolve_color(label, "palette", "select_bg") or style.lookup("TEntry", "selectbackground")
              or "#3584e4")
    on_accent = (resolve_color(label, "palette", "select_fg") or style.lookup("TEntry", "selectforeground")
                 or "#ffffff")
    try:
        for color in (base_bg, base_fg, accent, on_accent):
            label.winfo_rgb(color)
    except tk.TclError:
        return                                  # a color Tk does not know: no highlight rather than an error

    old = getattr(label, "_fade_job", None)
    if old:
        label.after_cancel(old)
    frames = max(1, fade // step)

    def paint(i):
        if not label.winfo_exists():
            return
        p = 0.0 if i < 0 else min(1.0, (i + 1) / frames)       # i < 0: the hold phase, full color
        label.configure(background=mix_color(label, accent, base_bg, p), foreground=mix_color(label, on_accent, base_fg, p))
        if entry is not None and entry.winfo_exists():
            entry.configure(foreground=mix_color(label, accent, base_fg, p))
        if p >= 1.0:
            label._fade_job = None
            label.configure(background="", foreground="")      # back to the theme
            if entry is not None and entry.winfo_exists():
                entry.configure(foreground="")
            return
        label._fade_job = label.after(hold if i < 0 else step, paint, i + 1)

    paint(-1)


def popup(parent, text, where, wrap=0):
    """
    Show a small borderless window with a text.

    @param parent  widget the window belongs to
    @param text    text of the window
    @param where   function(width, height) -> (x, y): screen position of the window's top left corner; the window is
                   moved back onto the screen if that puts it partly outside
    @param wrap    width in pixels at which the text is wrapped; 0 = never
    @return        the window; the caller destroys it
    """
    win = tk.Toplevel(parent)
    win.wm_overrideredirect(True)
    tk.Label(win, text=text, relief="solid", bd=1, padx=5, pady=2, wraplength=wrap, justify="left").pack()
    win.update_idletasks()
    width, height = win.winfo_reqwidth(), win.winfo_reqheight()
    x, y = where(width, height)
    win.wm_geometry("+%d+%d" % (max(0, min(x, win.winfo_screenwidth() - width)),
                                max(0, min(y, win.winfo_screenheight() - height))))
    return win


def above(widget):
    """@return a `where` for popup(): above the widget, below it if there is no room above"""
    def where(_width, height):
        y = widget.winfo_rooty() - height - 4
        return widget.winfo_rootx(), y if y >= 0 else widget.winfo_rooty() + widget.winfo_height() + 4
    return where


def at_pointer(widget):
    """@return a `where` for popup(): just above and right of the mouse pointer (below it at the top of the screen)"""
    x, y = widget.winfo_pointerxy()

    def where(_width, height):
        above_pointer = y - height - POINTER_GAP_Y
        return x + POINTER_GAP_X, above_pointer if above_pointer >= 0 else y + POINTER_CURSOR_HEIGHT
    return where


class Tip:
    """Small tooltip above a widget; the text is the language entry `key` (long text wraps, "\\n" = new line)."""

    WRAP = 320
    """Width in pixels at which a long tooltip text is wrapped."""

    def __init__(self, widget, key):
        self.widget, self.key, self.job, self.win = widget, key, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _enter(self, _event):
        self._hide()
        self.job = self.widget.after(600, self._show)

    def _show(self):
        self.job = None
        if not self.widget.winfo_exists():                    # the panel was rebuilt while the timer ran
            return
        self.win = popup(self.widget, t(self.key), above(self.widget), self.WRAP)

    def _hide(self, _event=None):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.win:
            self.win.destroy()
            self.win = None


class Toast:
    """A short message next to the mouse pointer that closes itself; a new message replaces the one that is still showing."""

    DURATION = 1500
    """Milliseconds a message stays."""

    def __init__(self, master):
        """@param master  main window: it owns the message and its timer"""
        self.master, self.job, self.win = master, None, None

    def show(self, text):
        """Show the message next to the mouse pointer."""
        self.hide()
        self.win = popup(self.master, text, at_pointer(self.master))
        self.job = self.master.after(self.DURATION, self.hide)

    def hide(self):
        if self.job:
            self.master.after_cancel(self.job)
            self.job = None
        if self.win:
            self.win.destroy()
            self.win = None


def center_on(win, parent):
    """Place a dialog in the middle of its parent window."""
    win.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_reqwidth()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_reqheight()) // 2
    win.geometry("+%d+%d" % (max(x, 0), max(y, 0)))


def ok_cancel(parent, ok, cancel, ok_key="btn.ok"):
    """@return a frame (not yet placed) with the OK and Cancel buttons at its right edge"""
    row = ttk.Frame(parent)
    ttk.Button(row, text=t("btn.cancel"), command=cancel).pack(side="right")
    ttk.Button(row, text=t(ok_key), command=ok).pack(side="right", padx=3)
    return row


def buttons_row(parent, *buttons):
    """
    @param buttons  (text key, command) pairs; the first one is the left-most. All are packed at the right edge.
    @return a frame (not yet placed) with the buttons
    """
    row = ttk.Frame(parent)
    for key, command in reversed(buttons):
        ttk.Button(row, text=t(key), command=command).pack(side="right", padx=3)
    return row


class Dialog(tk.Toplevel):
    """Modal dialog base. Build the widgets, then call show(); .result is None if cancelled."""

    def __init__(self, parent, title_key):
        super().__init__(parent)
        self.withdraw()
        self.title(t(title_key))
        self.transient(parent)
        self.parent, self.result = parent, None

    def show(self, focus=None, ok=None):
        """
        Center, show and wait until the dialog closes.

        @param focus  widget to focus first
        @param ok     function for the Return key
        """
        if ok:
            self.bind("<Return>", lambda e: ok())
        for seq in ("<Escape>", "<Control-w>", "<Control-W>") + (("<Command-w>",) if sys.platform == "darwin" else ()):
            self.bind(seq, lambda e: self.destroy())
        center_on(self, self.parent)
        self.deiconify()
        try:
            self.wait_visibility()
            self.grab_set()
        except tk.TclError:
            pass
        (focus or self).focus_set()
        self.wait_window(self)
