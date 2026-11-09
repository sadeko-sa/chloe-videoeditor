"""ui/preset.py - Name prompt and the effect preset manager (import, export, rename, duplicate, delete, order)."""

import os
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

from lang import t
from theme import resolve_color

from engine import effect as fx
from engine import preset as pr
from ui.common import Dialog, effect_name, fmt_number, ok_cancel, param_label

JSON_TYPE = [("JSON", "*.json")]
DETAIL_WIDTH = 300


class NameDialog(Dialog):
    """Ask for a preset name; .result is the text, None if cancelled."""

    def __init__(self, parent, name=""):
        super().__init__(parent, "dlg.preset_name")
        frm = ttk.Frame(self, padding=14)
        frm.pack(fill="both", expand=True)
        self.var = tk.StringVar(value=name)
        ttk.Label(frm, text=t("preset.name")).grid(row=0, column=0, sticky="w")
        self.entry = ttk.Entry(frm, textvariable=self.var, width=34)
        self.entry.grid(row=0, column=1, padx=(12, 0))
        ok_cancel(frm, self._ok, self.destroy).grid(row=1, column=0, columnspan=2, sticky="we", pady=(12, 0))
        self.entry.selection_range(0, "end")
        self.show(self.entry, self._ok)

    def _ok(self):
        name = self.var.get().strip()
        if not name:
            self.bell()
            return
        self.result = name
        self.destroy()


def _value(value):
    if isinstance(value, bool):
        return t("preset.on") if value else t("preset.off")
    return fmt_number(value) if isinstance(value, (int, float)) else str(value)


def describe(preset):
    """@return [(effect title, [(parameter label, value text)])], top row of the effect list first"""
    result = []
    for inst in reversed(preset.effect):
        eff = fx.get(inst.effect_id)
        if eff is None:
            result.append((t("preset.missing", effect=inst.effect_id), []))
            continue
        title = effect_name(eff) + ("" if inst.enabled else " (%s)" % t("preset.off"))
        result.append((title, [(param_label(eff, p), _value(inst.values[p.key])) for p in eff.params
                               if p.key in inst.values]))
    return result


class PresetManager(Dialog):
    """Manage the saved effect preset. Every change is saved at once; .result is the preset to apply, or None."""

    def __init__(self, app, can_apply):
        super().__init__(app.root, "dlg.preset")
        self.preset = pr.load()
        self.shown = []                                     # the preset of the rows, in row order
        self.geometry("760x460")
        self.minsize(620, 340)
        frm = ttk.Frame(self, padding=10)
        frm.pack(fill="both", expand=True)

        bottom = ttk.Frame(frm)
        bottom.pack(side="bottom", fill="x", pady=(10, 0))
        self.apply_btn = ttk.Button(bottom, text=t("preset.apply"), command=self._apply,
                                    state="normal" if can_apply else "disabled")
        self.apply_btn.pack(side="left")
        self.buttons = {}
        for key, cmd in (("rename", self._rename), ("duplicate", self._duplicate), ("delete", self._delete),
                         ("up", lambda: self._move(-1)), ("down", lambda: self._move(1))):
            self.buttons[key] = ttk.Button(bottom, text=t("preset." + key), command=cmd)
            self.buttons[key].pack(side="left", padx=(6 if key in ("rename", "up") else 3, 0))
        ttk.Button(bottom, text=t("preset.close"), command=self.destroy).pack(side="right")
        ttk.Button(bottom, text=t("preset.export"), command=self._export).pack(side="right", padx=(0, 6))
        ttk.Button(bottom, text=t("preset.import"), command=self._import).pack(side="right", padx=(0, 3))

        panes = ttk.PanedWindow(frm, orient="horizontal")
        panes.pack(fill="both", expand=True)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=1)

        self.filter_var = tk.StringVar()
        self.hint = False                                   # True while the entry shows the grey hint instead of a search text
        self.quiet = False                                  # True while the hint is put in or taken out (the list stays)
        self.filter_var.trace_add("write", lambda *a: None if self.quiet else self._fill())
        self.filter_entry = ttk.Entry(left, textvariable=self.filter_var)
        self.filter_entry.pack(fill="x", pady=(0, 6))
        self.text_color = ttk.Style(self).lookup("TEntry", "foreground") or "black"
        self.hint_color = resolve_color(self, "palette", "fg_disabled") or "gray50"
        self.filter_entry.bind("<FocusIn>", lambda e: self._hide_hint())
        self.filter_entry.bind("<FocusOut>", lambda e: self._show_hint())
        box = ttk.Frame(left)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=("name", "count"), show="headings", selectmode="extended")
        self.tree.heading("name", text=t("preset.col_name"), anchor="w")
        self.tree.heading("count", text=t("preset.col_effect"), anchor="w")
        self.tree.column("name", width=200, anchor="w")
        self.tree.column("count", width=60, stretch=False, anchor="w")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._selected())
        self.tree.bind("<Double-1>", lambda e: self._apply())
        self.tree.bind("<Delete>", lambda e: self._delete())

        self.bold = tkfont.nametofont("TkDefaultFont").copy()
        self.bold.configure(weight="bold")
        self.detail = ttk.LabelFrame(right, text=t("detail.title"), padding=8)
        self.detail.pack(fill="both", expand=True, padx=(10, 0))
        self._show_hint()
        self._fill()
        self.show(self.tree, self._apply)

    # ------------------------------------------------------------------ #

    def _needle(self):
        """@return the search text in lower case; empty while the hint is shown"""
        return "" if self.hint else self.filter_var.get().strip().lower()

    def _show_hint(self):
        if not self.hint and not self.filter_var.get():
            self.hint, self.quiet = True, True
            self.filter_var.set(t("preset.search"))
            self.quiet = False
            self.filter_entry.configure(foreground=self.hint_color)

    def _hide_hint(self):
        if self.hint:
            self.hint, self.quiet = False, True
            self.filter_var.set("")
            self.quiet = False
            self.filter_entry.configure(foreground=self.text_color)

    def _fill(self, select=()):
        """Show the preset that match the search text and select the ones in `select` (preset objects)."""
        needle = self._needle()
        self.tree.delete(*self.tree.get_children())
        self.shown = [p for p in self.preset if needle in p.name.lower()]
        for i, p in enumerate(self.shown):
            self.tree.insert("", "end", iid=str(i), values=(p.name, len(p.effect)))
        picked = [str(i) for i, p in enumerate(self.shown) if any(p is s for s in select)]
        if picked:
            self.tree.selection_set(picked)
            self.tree.see(picked[0])
        self._selected()

    def _chosen(self):
        """@return the selected preset objects in list order"""
        return [self.shown[int(i)] for i in sorted(self.tree.selection(), key=int)]

    def _selected(self):
        chosen = self._chosen()
        one = len(chosen) == 1
        self.buttons["rename"].configure(state="normal" if one else "disabled")
        self.buttons["duplicate"].configure(state="normal" if chosen else "disabled")
        self.buttons["delete"].configure(state="normal" if chosen else "disabled")
        move = "normal" if one and not self._needle() else "disabled"
        self.buttons["up"].configure(state=move)
        self.buttons["down"].configure(state=move)
        for child in self.detail.winfo_children():
            child.destroy()
        if not one:
            ttk.Label(self.detail, text=t("preset.empty") if not self.preset else t("preset.select"),
                      wraplength=DETAIL_WIDTH - 24, justify="left").pack(anchor="w")
            return
        grid = ttk.Frame(self.detail)
        grid.pack(fill="x", anchor="n")
        grid.columnconfigure(1, weight=1)
        row = 0
        for title, rows in describe(chosen[0]):
            ttk.Label(grid, text=title, font=self.bold, wraplength=DETAIL_WIDTH - 24, justify="left").grid(
                row=row, column=0, columnspan=2, sticky="nw", pady=(0 if row == 0 else 8, 2))
            row += 1
            for label, value in rows:
                ttk.Label(grid, text=label, wraplength=140, justify="left").grid(row=row, column=0, sticky="nw", pady=1)
                ttk.Label(grid, text=value, wraplength=DETAIL_WIDTH - 190, justify="left").grid(
                    row=row, column=1, sticky="nw", padx=(10, 0), pady=1)
                row += 1

    def _store(self):
        """Save the list; @return False (after telling the user) if that failed"""
        try:
            pr.save(self.preset)
        except pr.PresetError as exc:
            messagebox.showerror(t("dlg.error"), t("preset.save_failed", error=exc), parent=self)
            return False
        return True

    def _apply(self):
        chosen = self._chosen()
        if len(chosen) == 1 and str(self.apply_btn.cget("state")) == "normal":
            self.result = chosen[0]
            self.destroy()

    def _rename(self):
        chosen = self._chosen()
        if len(chosen) != 1:
            return
        name = NameDialog(self, chosen[0].name).result
        if name:
            chosen[0].name = pr.unique_name(name, [p.name for p in self.preset if p is not chosen[0]])
            self._store()
            self._fill([chosen[0]])

    def _duplicate(self):
        copies = []
        for p in self._chosen():
            copy = pr.EffectPreset.from_dict(p.to_dict())
            copy.name = pr.unique_name(p.name, [q.name for q in self.preset])
            self.preset.insert(self.preset.index(p) + 1, copy)
            copies.append(copy)
        if copies:
            self._store()
            self._fill(copies)

    def _delete(self):
        chosen = self._chosen()
        if not chosen or not messagebox.askyesno(t("dlg.preset"), t("preset.confirm_delete", count=len(chosen)),
                                                 parent=self):
            return
        self.preset = [p for p in self.preset if not any(p is c for c in chosen)]
        self._store()
        self._fill()

    def _move(self, delta):
        chosen = self._chosen()
        if len(chosen) != 1:
            return
        i = self.preset.index(chosen[0])
        if 0 <= i + delta < len(self.preset):
            self.preset[i], self.preset[i + delta] = self.preset[i + delta], self.preset[i]
            self._store()
            self._fill(chosen)

    def _import(self):
        path = filedialog.askopenfilename(parent=self, title=t("preset.import"), filetypes=JSON_TYPE)
        if not path:
            return
        try:
            new = pr.read_file(path)
        except pr.PresetError as exc:
            messagebox.showerror(t("dlg.error"), t("preset.import_failed", file=os.path.basename(path), error=exc),
                                 parent=self)
            return
        for p in new:                                       # a name already in use gets a number, nothing is overwritten
            p.name = pr.unique_name(p.name, [q.name for q in self.preset])
            self.preset.append(p)
        if new:
            self._hide_hint()
            self.filter_var.set("")
            self.filter_entry.configure(foreground=self.text_color)
            self._show_hint()
            self._store()
            self._fill(new)

    def _export(self):
        chosen = self._chosen() or list(self.preset)        # nothing selected = all of them
        if not chosen:
            return
        path = filedialog.asksaveasfilename(parent=self, title=t("preset.export"), defaultextension=".json",
                                            filetypes=JSON_TYPE, initialfile=t("preset.export_name") + ".json")
        if not path:
            return
        try:
            pr.write_file(path, chosen)
        except pr.PresetError as exc:
            messagebox.showerror(t("dlg.error"), t("preset.save_failed", error=exc), parent=self)
