"""ui/common.py - Small helpers shared by the windows: text lookups, tooltip, modal dialog base, Windows setup."""

import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk

from chloe_lang import t

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


def fmt_number(value, digits=2):
    """@return a number without useless trailing zeros"""
    text = ("%.*f" % (digits, value)).rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


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
        for seq in ("<Escape>", "<Control-w>", "<Control-W>"):
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
