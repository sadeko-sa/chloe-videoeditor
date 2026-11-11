# Chloe Videoeditor
Small cross platform video editor (Windows, Linux, macOS) in Python / Tkinter. All picture and sound processing is done by FFmpeg.

## Install and run

    python chloe_videoeditor.py [project (.cvproj), video or image]

* Python 3.8+ with Tkinter. Drag and drop of media / projects onto the window needs `pip install tkinterdnd2` (optional; Settings > About shows whether it is on).
* FFmpeg 4.4 or newer with `ffmpeg` and `ffprobe`:
  Windows `winget install ffmpeg` (or unpack it into `ffmpeg/bin` next to the program), macOS `brew install ffmpeg`,
  Debian / Ubuntu `sudo apt install ffmpeg python3-tk`, Fedora `sudo dnf install ffmpeg python3-tkinter`.
* It is found in the program folder (`ffmpeg/`, `ffmpeg/bin/`, `bin/`), on PATH and in the usual install folders. Or set it in Settings > General.
* Windows: `pythonw chloe_videoeditor.py` starts without a console window (python.exe restarts itself that way).

## Using it

1. **File > Add media** (Ctrl+I): videos and images become media layers, the first one sets the frame size and rate (Edit > Project settings changes them).
2. **+ Adjustment** adds an adjustment layer: its effects change everything beneath it. Layers are drawn bottom to top; the list shows the top layer first.
3. Select a layer, press **Add** in the Effects box and move the sliders. Effects are listed like layers in Photoshop: the bottom row runs first, the top row last; **Add** puts the new one above the selected effect. Select several effects like layers in Photoshop (Ctrl+click toggles one, Shift+click selects a range, the last one clicked is the active one); a preset selects all the effects it adds. Delete, move up / down and the check mark work on the whole selection (one undo step); the parameters show when one effect is selected. Double click a parameter name to reset it. Click the check mark to switch a layer or an effect off.
4. Move through the video with the slider, the arrow buttons or the Left / Right keys (Shift = one second, Home / End) in the preview area. **Edit > Go to time / frame** (Ctrl+G) jumps to an exact time (seconds, mm:ss or hh:mm:ss) or frame number; the two fields follow each other.
5. Hold **Original** under the preview to see the picture without effects.
6. **File > Export video** (Ctrl+E): the dialog works like HandBrake. Pick a preset or set everything by hand: container (MP4, MKV, WebM), video codec (H.264, H.265, VP9, AV1 with SVT-AV1 or libaom, as far as your FFmpeg has them), encoder speed, tune, profile, level, 8 or 10 bit, constant quality (CRF) or average bitrate (two-pass for H.264 and VP9), output height and frame rate, audio codec (AAC, MP3, AC-3, Opus, Vorbis, FLAC or none), audio bitrate, channels and sample rate, MP4 fast start and extra FFmpeg options. The last choices are remembered. AV1 needs FFmpeg 5.0 or newer. The export is added to the **job queue** (File > Job queue, Ctrl+Q) and rendered by a background worker, so you can keep editing. The queue window shows every worker and its job, every job with a progress bar and a colored status (waiting, rendering, paused, done, failed, stopped), and a details panel for the selected job with a button that copies its FFmpeg command; jobs can be paused and resumed, stopped, deleted, and finished ones cleared. A finished or failed job rings the system bell. Settings > Worker sets the number of workers (default 1) and the thread limit of each worker (0 = automatic). **Export frame** (Ctrl+Shift+E) writes the picture at full size as JPEG (default) or PNG; the suggested name is the media file name, the export date and time and the position in the video and the frame number, like `clip.mp4-[2026.11.09-03.13.07]-02.11-3945-.jpg`.

Media file: a media layer shows its file in the *File* field. If the file was moved or deleted, the layer is shown in red with a warning sign in the layer list and the field turns red; click *Pick...* (or type a path and press Enter) to point it to the new location. Timing and effects stay. The check is repeated when the window gets the focus again.

Layer timing: *Start* is where the layer begins on the timeline, *In point* the second of the file it starts at, *Duration* how long it shows. Adjustment layers can have a time range too (0 = to the end) and an opacity (the strength of the effect). Placement: *Fit inside* keeps the whole picture, *Fill frame* crops. The **Crop** effect (Transform) cuts pixels from each edge, like the crop of HandBrake, of a layer or of everything beneath an adjustment layer. The pixels are those of the project frame, counted from its edges (the preview shows the same cut at a smaller size; a clip that does not fill the frame has an empty margin that counts as already cut). The mode decides what happens to the rest: *Fit* (black bars), *Fill* (trimmed), *Stretch*, or *Mask* (edges painted black, size kept). The **Crop assist...** button under the values opens a window like the crop of a phone gallery: the picture of the preview with a box; drag its corners or edges to cut, drag inside it to move it (arrow keys 1 px, Shift 10 px), pick a ratio (Free, Original, 1:1, 5:4, 4:3, 3:2, 16:9, 21:9) and switch between portrait and landscape (Rotate). A ratio keeps the box where it is (the largest box of that shape inside it, same centre) and is remembered for the next time the window is opened. The picture is made without the cut of this effect (other effects stay on it). *Apply* writes the four cuts into the effect as one undo step. Projects are saved as `.cvproj` (JSON); the first save suggests the name of the first media file. **File > Open recent** lists the last 10 projects opened or saved (missing ones are dropped when chosen; *Clear list* empties it). Ctrl+Z / Ctrl+Y undo and redo (Cmd on macOS).

## How it works

* `engine/compiler.py` turns the project into ONE FFmpeg filter graph. Nothing is processed in Python.
* The preview renders a single frame: media files are opened with a seek and decoded for one picture, layers that are off at that second are left out. Pictures are cached; while you edit a layer the picture beneath it is cached too, so moving a slider only runs the effects of that layer on one small PNG. A newer request kills the render still running.
* The export uses the same layer code for the whole timeline. Each queued job is a snapshot of the project at that moment (later edits do not change it). `engine/encode.py` holds the tables of what each container and codec allows and builds the encoder arguments. The thread limit is passed to FFmpeg as `-threads:v` for decoding and encoding and `-filter_complex_threads` for the filters.

## Adding an effect

Drop a module into `effect/` (every module not starting with `_` is imported at start). No other file changes:

    from engine.effect import ChainEffect, Param, register, num

    @register
    class Gamma(ChainEffect):
        id = "gamma"
        name = "Gamma"
        category = "color"
        params = (Param("value", "Gamma", "float", default=1.0, min=0.2, max=3.0, step=0.05),)

        def filters(self, v, ctx):
            return ["eq=gamma=%s" % num(v["value"])] if v["value"] != 1.0 else []

`filters()` returns FFmpeg filter strings (`[]` = no change). `ctx.px()` converts pixel sizes for the smaller preview. Parameter kinds: float, int, bool, choice, curve (a tone curve: a list of `[input, output]` points 0..1; all curve parameters of an effect share one curve editor, see `effect/curves.py`). Optional translations: `effect.<id>`, `effect.<id>.<param>`, `choice.<id>.<param>.<value>`, `category.<category>` in `lang/*.json`; without them the English texts are used. An effect can name an assist window (`assist = "crop"`, see `App.ASSISTS`) that gets a button under its parameters, and give a pixel parameter `Param(..., extent="width")` (or `"height"`) so that its slider runs over the project size. A broken plugin is skipped and listed in Settings > About.

An effect that needs branches (for example a blurred copy that is blended back) extends `Effect` instead and implements `is_neutral(v, ctx)` and `apply(g, source, v, ctx)`. `g` is a `GraphBuilder` (`chain()` adds a chain of filters, `split()` copies a picture), `source` the label of the picture and the result is the label of the changed one. `Clarity` in `effect/detail.py` is an example. An effect that changes every pixel by a rule that depends only on that pixel's own color (hue and saturation by color range) extends `LatticeEffect` and implements `is_neutral(v, ctx)` and `lattice_filters(v, ctx)`: the rule runs once on a small lattice of all colors and `haldclut` looks every pixel up in it, so even a slow `geq` rule costs about as much as a lookup per frame. `HueSaturation` in `effect/hue_saturation.py` is an example.

## Languages and themes

`lang/<code>.json` and `theme/<code>.json`, same format as Chloe Photoviewer (English and Japanese included, six themes). Choose the language in the Language root menu (or Settings > Language) and the theme in the Settings menu > Theme (or Settings > Appearance; both used from the next start; languages are listed as English name - native name, e.g. Japanese - 日本語); *Settings > Theme > Edit theme...* edits a theme file. The Settings dialog is in its own Settings menu (Ctrl+,).

## Files

    chloe_videoeditor.py   entry point
    lang.py, theme.py   language and theme support (shared with Chloe Photoviewer)
    engine/   effect.py (effect interfaces + registry), curve.py (tone curve maths), cropbox.py (crop box geometry), project.py, selection.py (effect selection), compiler.py, ffmpeg.py, jobs.py (job queue + workers), preview.py, config.py  (no Tk)
    ui/       app.py, panels.py, curve_editor.py, crop_assist.py (crop assist window), dialogs.py, jobs.py (job queue window), common.py
    effect/  color.py, crop.py, curves.py, detail.py, hue_saturation.py, style.py
    lang/  theme/

The config `config.json` and, by default, the preview cache (`cache/`, emptied on exit) are saved next to the program; nothing is written to AppData or the home folder. Only `config.json` is read (an old `chloe_videoeditor.json` is ignored, so copy its content over if you want to keep it). The cache can be moved to any folder in Settings > General > Cache folder (empty = `cache/` next to the program); it is used at once and only `run_` folders made by this program are ever removed there.
