"""ui/common.py - Small helpers shared by the windows: text lookups, tooltip, modal dialog base, Windows setup."""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

from lang import t
from theme import resolve_color

from engine.ffmpeg import IS_WIN


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


def text_or(key, fallback):
    """@return the language text for key, or fallback if the language files have none"""
    text = t(key)
    return fallback if text == key else text


def effect_name(eff):
    return text_or("effect." + eff.id, eff.name)


def param_label(eff, param):
    return text_or("effect.%s.%s" % (eff.id, param.key), param.label)


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
    text = ("%.*f" % (digits, value)).rstrip("0").rstrip(".")
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


class Tip:
    """Small tooltip for a widget; the text is the language entry `key`."""

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
        self.win = tk.Toplevel(self.widget)
        self.win.wm_overrideredirect(True)
        tk.Label(self.win, text=t(self.key), relief="solid", bd=1, padx=5, pady=2).pack()
        self.win.wm_geometry("+%d+%d" % (self.widget.winfo_rootx(), max(self.widget.winfo_rooty() - 30, 0)))

    def _hide(self, _event=None):
        if self.job:
            self.widget.after_cancel(self.job)
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
