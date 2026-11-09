"""ui/panels.py - The three panels of the window: layer list, layer properties, effect chain with its parameters."""

import tkinter as tk
from tkinter import ttk

from lang import t

from engine import effect as fx
from engine.project import FITS, MediaLayer
from ui.common import Tip, category_label, choice_label, effect_name, fmt_number, highlight_fade, param_label

ON, OFF = "\u2611", "\u2610"
WARN = "\u26A0 "
"""Put in front of the name of a media layer whose file is missing (the color alone is easy to overlook)."""


def _snap(value, step, lo, hi, as_int):
    """@return value rounded to the slider step and limited to lo..hi"""
    if step:
        value = round(value / step) * step
    value = max(lo, min(hi, value))
    return int(round(value)) if as_int else round(value, 4)


class LayerPanel(ttk.Frame):
    """The layer list (top layer first) with its buttons. The App does the work; this only shows it."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app, self._busy = app, False
        top = ttk.Frame(self)
        top.pack(fill="x", pady=(0, 4))
        ttk.Button(top, text=t("btn.add_media"), command=app.add_media_dialog).pack(side="left")
        ttk.Button(top, text=t("btn.add_adjustment"), command=app.add_adjustment).pack(side="left", padx=4)
        self.tree = ttk.Treeview(self, columns=("on", "name", "kind"), show="headings", selectmode="browse", height=8)
        for col, key, width, stretch in (("on", "", 30, False), ("name", "col.layer", 130, True),
                                         ("kind", "col.type", 80, False)):
            self.tree.heading(col, text=t(key) if key else "", anchor="w")      # header text left, like the cells below
            self.tree.column(col, width=width, stretch=stretch, anchor="center" if col == "on" else "w")
        self.tree.tag_configure("missing", foreground=app.col_err)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<Delete>", lambda e: app.remove_layer())
        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(4, 0))
        for text, tip, cmd in (("\u25B2", "tip.layer_up", lambda: app.move_layer(1)),
                               ("\u25BC", "tip.layer_down", lambda: app.move_layer(-1)),
                               ("\u29C9", "tip.layer_duplicate", app.duplicate_layer),
                               ("\u2715", "tip.layer_remove", app.remove_layer)):
            btn = ttk.Button(bottom, text=text, width=3, command=cmd)
            btn.pack(side="left", padx=(0, 4))
            Tip(btn, tip)

    def refresh(self, project, selected, missing=()):
        """Show the layers of the project and select one. `missing` = ids of layers whose media file is gone."""
        self._busy = True
        self.tree.delete(*self.tree.get_children())
        for layer in reversed(project.layers):
            gone = layer.id in missing
            self.tree.insert("", "end", iid=layer.id, tags=("missing",) if gone else (),
                             values=(ON if layer.enabled else OFF, (WARN + layer.name) if gone else layer.name,
                                     t("kind." + layer.kind)))
        if selected and self.tree.exists(selected):
            self.tree.selection_set(selected)
            self.tree.see(selected)
        self._busy = False

    def _selected(self, _event):
        """Tk sends <<TreeviewSelect>> after refresh() returned, so only a real change of selection counts."""
        sel = self.tree.selection()
        wanted = sel[0] if sel else None
        if wanted and wanted != self.app.sel_layer:
            self.app.select_layer(wanted)

    def _click(self, event):
        """A click on the check mark switches the layer on / off."""
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
            iid = self.tree.identify_row(event.y)
            if iid:
                self.app.toggle_layer(iid)
                return "break"
        return None


class PropertiesPanel(ttk.LabelFrame):
    """Name, timing, opacity and so on of the selected layer."""

    def __init__(self, parent, app):
        super().__init__(parent, text=t("group.layer"), padding=8)
        self.app = app
        self.columnconfigure(1, weight=1)

    def show(self, layer):
        """Rebuild the fields for this layer (None = empty)."""
        for child in self.winfo_children():
            child.destroy()
        if layer is None:
            return
        app = self.app
        self._row = 0
        name = tk.StringVar(value=layer.name)
        ttk.Label(self, text=t("field.name")).grid(row=0, column=0, sticky="w", pady=2)
        entry = ttk.Entry(self, textvariable=name)
        entry.grid(row=0, column=1, sticky="we", padx=(8, 0))
        entry.bind("<Return>", lambda e: app.set_layer(layer.id, "name", name.get().strip() or layer.name, "name"))
        entry.bind("<FocusOut>", lambda e: app.set_layer(layer.id, "name", name.get().strip() or layer.name, "name"))
        self._row = 1
        is_media = isinstance(layer, MediaLayer)
        if is_media:
            self._file_row(layer)
        self._number(layer, "start", "field.start", 0, 36000, 0.1, 3)
        if is_media:
            self._number(layer, "in_point", "field.in_point", 0, max(layer.source_duration, 36000), 0.1, 3)
        self._number(layer, "duration", "field.duration" if is_media else "field.duration_adj",
                     0.04 if is_media else 0, 36000, 0.1, 3)
        self._number(layer, "opacity", "field.opacity", 0, 100, 5, 0, scale=100)
        if is_media:
            if layer.has_audio:
                self._number(layer, "volume", "field.volume", 0, 200, 5, 0, scale=100)
            ttk.Label(self, text=t("field.fit")).grid(row=self._row, column=0, sticky="w", pady=2)
            fit = ttk.Combobox(self, state="readonly", width=12, values=[t("fit." + f) for f in FITS])
            fit.current(FITS.index(layer.fit))
            fit.grid(row=self._row, column=1, sticky="w", padx=(8, 0))
            fit.bind("<<ComboboxSelected>>", lambda e: app.set_layer(layer.id, "fit", FITS[max(fit.current(), 0)]))

    def _file_row(self, layer):
        """The media file of the layer: path field (Enter applies it), Pick... button, red when the file is missing."""
        app, row = self.app, self._row
        missing = layer.is_missing()
        ttk.Label(self, text=t("field.file")).grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=layer.path)
        entry = ttk.Entry(self, textvariable=var, **({"foreground": app.col_err} if missing else {}))
        entry.grid(row=row, column=1, sticky="we", padx=(8, 0))
        entry.xview_moveto(1.0)                     # a long path: show its end, the file name
        for seq in ("<Return>", "<KP_Enter>"):      # not on focus out: a failed probe opens a dialog, which moves the focus
            entry.bind(seq, lambda e: app.set_media_path(layer.id, var.get()))
        ttk.Button(self, text=t("btn.pick"), width=8,
                   command=lambda: app.relink_layer(layer.id)).grid(row=row, column=2, padx=(4, 0))
        self._row += 1
        if missing:
            ttk.Label(self, text=t("msg.file_missing"), foreground=app.col_err, wraplength=260,
                      justify="left").grid(row=self._row, column=0, columnspan=3, sticky="w", pady=(0, 4))
            self._row += 1

    def _number(self, layer, attr, key, lo, hi, step, digits, scale=1):
        """One spin box row. `scale` = shown value / stored value (opacity is stored 0..1, shown in %)."""
        app, row = self.app, self._row
        var = tk.StringVar(value=fmt_number(getattr(layer, attr) * scale, digits))
        ttk.Label(self, text=t(key)).grid(row=row, column=0, sticky="w", pady=2)

        def commit(_event=None):
            try:
                shown = float(var.get().replace(",", "."))
            except ValueError:
                shown = getattr(layer, attr) * scale
            shown = max(lo, min(hi, shown))
            app.set_layer(layer.id, attr, shown / scale, attr)
            var.set(fmt_number(getattr(layer, attr) * scale, digits))

        spin = ttk.Spinbox(self, textvariable=var, from_=lo, to=hi, increment=step, width=10, command=commit)
        spin.grid(row=row, column=1, sticky="w", padx=(8, 0))
        for seq in ("<Return>", "<KP_Enter>", "<FocusOut>"):
            spin.bind(seq, commit)
        self._row += 1


class ParamEditor(ttk.Frame):
    """Sliders / check boxes / lists for the parameters of one effect instance (built from its Param list)."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.columnconfigure(1, weight=1)

    def show(self, layer, index):
        """Rebuild for layer.effects[index] (None = empty)."""
        for child in self.winfo_children():
            child.destroy()
        if layer is None or index is None or not 0 <= index < len(layer.effects):
            return
        inst = layer.effects[index]
        eff = fx.get(inst.effect_id)
        if eff is None:
            ttk.Label(self, text=t("msg.effect_missing", id=inst.effect_id), wraplength=260).grid(row=0, column=0)
            return
        values = eff.normalize(inst.values)
        for row, param in enumerate(eff.params):
            label = ttk.Label(self, text=param_label(eff, param), wraplength=110, justify="left")
            label.grid(row=row, column=0, sticky="w", pady=3)
            send = lambda key, value, _l=layer.id, _i=index: self.app.set_param(_l, _i, key, value)
            if param.kind in ("float", "int"):
                self._slider(row, label, param, values[param.key], send)
            elif param.kind == "bool":
                var = tk.BooleanVar(value=values[param.key])
                ttk.Checkbutton(self, variable=var,
                                command=lambda p=param, v=var: send(p.key, bool(v.get()))).grid(row=row, column=1, sticky="w")
            else:
                box = ttk.Combobox(self, state="readonly", width=16,
                                   values=[choice_label(eff, param, c) for c in param.choices])
                box.current(param.choices.index(values[param.key]))
                box.grid(row=row, column=1, columnspan=2, sticky="w")
                box.bind("<<ComboboxSelected>>",
                         lambda e, p=param, b=box: send(p.key, p.choices[max(b.current(), 0)]))

    def _slider(self, row, label, param, value, send):
        """Slider + typed value. Double click on the label resets the parameter to its default."""
        as_int = param.kind == "int"
        digits = 0 if as_int else (2 if param.step < 1 else 1)
        scale_var, text_var = tk.DoubleVar(value=value), tk.StringVar(value=fmt_number(value, digits))

        def apply(raw):
            v = _snap(float(raw), param.step, param.min, param.max, as_int)
            text_var.set(fmt_number(v, digits))
            send(param.key, v)
            return v

        def typed(_event=None):
            try:
                v = apply(float(text_var.get().replace(",", ".")))
            except ValueError:
                v = scale_var.get()
            scale_var.set(v)
            text_var.set(fmt_number(v, digits))

        ttk.Scale(self, from_=param.min, to=param.max, variable=scale_var, command=apply,
                  orient="horizontal").grid(row=row, column=1, sticky="we", padx=6)
        entry = ttk.Entry(self, textvariable=text_var, width=6, justify="right")
        entry.grid(row=row, column=2)
        for seq in ("<Return>", "<KP_Enter>", "<FocusOut>"):
            entry.bind(seq, typed)

        def reset(_event=None):
            target = _snap(param.default, param.step, param.min, param.max, as_int)
            if _snap(scale_var.get(), param.step, param.min, param.max, as_int) == target:
                return                              # already at the default: nothing changed, no highlight
            scale_var.set(param.default)
            apply(param.default)
            highlight_fade(label, entry)

        label.bind("<Double-Button-1>", reset)


class EffectPanel(ttk.LabelFrame):
    """
    The effect chain of the selected layer and the parameters of the selected effect. Rows are shown like layers in
    Photoshop: the bottom row is applied first, the top row last (on top of the others).
    """

    def __init__(self, parent, app):
        super().__init__(parent, text=t("group.effects"), padding=8)
        self.app, self._busy = app, False
        bar = ttk.Frame(self)
        bar.pack(fill="x")
        self.add_btn = ttk.Button(bar, text=t("btn.add_effect") + " \u25BE", command=self._add_menu)
        self.add_btn.pack(side="left")
        for text, cmd in (("\u25B2", lambda: app.move_effect(1)),      # up = applied later
                          ("\u25BC", lambda: app.move_effect(-1))):   # down = applied earlier
            ttk.Button(bar, text=text, width=3, command=cmd).pack(side="left", padx=(4, 0))
        remove = ttk.Button(bar, text="\u2715", width=3, command=app.remove_effect)
        remove.pack(side="left", padx=(4, 0))
        Tip(remove, "tip.effect_remove")
        box = ttk.Frame(self)                       # holds the list and its scrollbar side by side
        box.pack(fill="x", pady=6)
        # show="" = no header row and no tree column: the group is already called "Effects", so a header
        # "Effect" above the rows would only say it twice.
        self.tree = ttk.Treeview(box, columns=("on", "name"), show="", selectmode="browse", height=5)
        self.tree.column("on", width=30, stretch=False, anchor="center")
        self.tree.column("name", width=200, stretch=True, anchor="w")
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="x", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.tree.bind("<Button-1>", self._click)
        self.tree.bind("<Delete>", lambda e: app.remove_effect())
        self.params = ParamEditor(self, app)
        self.params.pack(fill="x")

    def _add_menu(self):
        """Pop up the list of all effects, one sub menu per category."""
        menu = tk.Menu(self, tearoff=0)
        for category, effects in fx.categories():
            sub = tk.Menu(menu, tearoff=0)
            for eff in effects:
                sub.add_command(label=effect_name(eff), command=lambda e=eff: self.app.add_effect(e.id))
            menu.add_cascade(label=category_label(category), menu=sub)
        x, y = self.add_btn.winfo_rootx(), self.add_btn.winfo_rooty() + self.add_btn.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def refresh(self, layer, selected):
        """Show the effects of the layer (None = none) and select the one at index `selected`."""
        self._busy = True
        self.tree.delete(*self.tree.get_children())
        effects = layer.effects if layer else []
        for i in reversed(range(len(effects))):     # like Photoshop layers: the top row is applied last, on top of the rest
            inst = effects[i]
            eff = fx.get(inst.effect_id)
            self.tree.insert("", "end", iid=str(i),     # iid = index in layer.effects (the order of application)
                             values=(ON if inst.enabled else OFF, effect_name(eff) if eff else "? " + inst.effect_id))
        if selected is not None and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))            # scrolls the list to the selected effect
        self._busy = False
        self.params.show(layer, selected)

    def _selected(self, _event):
        """Tk sends <<TreeviewSelect>> after refresh() returned, so only a real change of selection counts."""
        sel = self.tree.selection()
        wanted = int(sel[0]) if sel else None
        if wanted is not None and wanted != self.app.sel_effect:
            self.app.select_effect(wanted)

    def _click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and self.tree.identify_column(event.x) == "#1":
            iid = self.tree.identify_row(event.y)
            if iid:
                self.app.toggle_effect(int(iid))
                return "break"
        return None
