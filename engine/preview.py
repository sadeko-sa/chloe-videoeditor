"""
preview.py - Single frame preview with a cache, plus saving the current frame at full size.

The preview never plays the timeline: it renders ONE picture at one second, at the size of the
preview area. Two things keep it quick:
  * Every finished picture is cached (by time, size and the layers that make it). Going back to a
    moment you saw, or toggling a layer back, costs nothing.
  * While you work on one layer (the "focus" layer) the picture of everything beneath it is cached
    too. Moving a slider of the top adjustment layer then only runs FFmpeg on one small PNG plus that
    layer's effects, without opening or decoding the video again.
Requests are answered by one worker thread; a newer request kills the one still running.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
from collections import OrderedDict
from typing import Callable, List, Optional, Tuple

from .compiler import CompileError, compile_frame
from .ffmpeg import APP_DIR, FFmpegError, Tools, _base, _tail, popen, render_frame
from .project import Layer, MediaLayer, Project, even

CACHE_ENTRIES = 80
"""Pictures kept in the cache (each is one PNG of preview size)."""


FULL_FRAMES = 2
"""Whole frames kept for zoomed views (uncompressed, so big); older ones are deleted."""

VIEW_SCALES = (0.25, 0.5, 0.75, 1.0)
"""Sizes (of the project size) in which the frame behind a zoomed view is rendered; the smallest one that is not
below the zoom is used, so a zoom below 100% does not render the full size."""


RUN_MARK = ".chloe_run"
"""Empty file inside every run_ folder made by this program. In a folder the user picked, only run_ folders
with this mark are removed as leftovers, so nothing else that lives there is ever touched."""


def default_root() -> str:
    """@return the cache folder next to the program (used when the user picked none, or the picked one fails)"""
    return os.path.join(APP_DIR, "cache")


def cache_root(root_dir: str = "") -> str:
    """
    @param root_dir  folder picked by the user ("~" allowed); empty = the default
    @return          absolute path of the folder in which the run_ folders are made
    """
    return os.path.abspath(os.path.expanduser(root_dir)) if root_dir else default_root()


def _make_folder(root_dir: str = "") -> str:
    """
    @param root_dir  folder picked by the user to hold the cache; empty = cache/ in the program folder
    @return a new empty cache folder (run_...) inside that folder, after removing the leftovers of crashed
            runs; if the picked folder cannot be used the program folder (cache/) is tried, and the system
            temp folder (AppData on Windows) is only the last fallback
    """
    roots = [cache_root(root_dir)]
    if roots[0] != default_root():
        roots.append(default_root())
    for root in roots:
        try:
            os.makedirs(root, exist_ok=True)
            for name in os.listdir(root):
                old = os.path.join(root, name)
                # the program's own folder only holds our run_ folders; elsewhere only marked ones are ours
                if name.startswith("run_") and (root == default_root() or os.path.exists(os.path.join(old, RUN_MARK))):
                    shutil.rmtree(old, ignore_errors=True)
            folder = tempfile.mkdtemp(prefix="run_", dir=root)
            with open(os.path.join(folder, RUN_MARK), "w"):
                pass                                                # empty file, only its name matters
            return folder
        except OSError:
            continue                                                # next root (the program folder)
    return tempfile.mkdtemp(prefix="chloe_preview_")


class FrameCache:
    """PNG files in a temporary folder, oldest removed first."""

    def __init__(self, limit: int = CACHE_ENTRIES, root: str = ""):
        """
        @param limit  most pictures kept
        @param root   folder picked by the user to hold the cache; empty = cache/ in the program folder
        """
        self.limit = limit
        self.folder = _make_folder(root)
        self._items: "OrderedDict[str, str]" = OrderedDict()

    def path(self, key: str) -> str:
        """@return where the picture for this key is (to be) stored"""
        return os.path.join(self.folder, key + ".png")

    def get(self, key: str) -> Optional[str]:
        """@return the file of a cached picture (marked as recently used), None if not cached"""
        path = self._items.get(key)
        if path and os.path.exists(path):
            self._items.move_to_end(key)
            return path
        self._items.pop(key, None)
        return None

    def add(self, key: str) -> None:
        """Register the file written to path(key); drop the oldest pictures over the limit."""
        self._items[key] = self.path(key)
        self._items.move_to_end(key)
        while len(self._items) > self.limit:
            _old, path = self._items.popitem(last=False)
            try:
                os.remove(path)
            except OSError:
                pass

    def close(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)


def _stamp(layer: Layer):
    """@return what identifies the media file of a layer (changes when the file is replaced)"""
    if isinstance(layer, MediaLayer):
        try:
            st = os.stat(layer.path)
            return [st.st_mtime_ns, st.st_size]
        except OSError:
            return None
    return None


def frame_key(project: Project, layers: List[Layer], t: float, size: Tuple[int, int]) -> str:
    """@return cache key of the picture made by `layers` at second t in this size"""
    data = {"p": [project.width, project.height, project.fps], "t": round(t, 4), "s": list(size),
            "l": [[l.render_state(), _stamp(l)] for l in layers]}
    return hashlib.sha1(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest()


def active_layers(project: Project, t: float) -> List[Layer]:
    """@return the layers that are enabled and on at second t, bottom to top"""
    return [l for l in project.layers if l.enabled and l.active_at(t, project)]


class PreviewEngine:
    """
    Worker for preview requests.

        engine = PreviewEngine(tools, deliver, cache_dir)   # deliver(seq, png path or None, error or None)
        seq = engine.request(project, t, (w, h), focus_layer_id)
        seq = engine.request(project, t, (w, h), focus_layer_id, view=(x, y, w, h, zoom))   # zoomed: only that part
        engine.set_cache_dir(other_folder)                  # the cache moves, no restart needed
        engine.close()

    `deliver` runs in the worker thread, and is skipped for a request that a newer one replaced.
    """

    def __init__(self, tools: Optional[Tools], deliver: Callable[[int, Optional[str], Optional[str]], None],
                 cache_dir: str = ""):
        """
        @param tools      the FFmpeg programs, None if not found
        @param deliver    called in the worker thread with the answer of a request
        @param cache_dir  folder picked by the user to hold the cache; empty = cache/ in the program folder
        """
        self.tools, self.deliver = tools, deliver
        self.cache_dir = cache_dir                                  # as picked by the user (see set_cache_dir)
        self.cache = FrameCache(root=cache_dir)
        self._cond = threading.Condition()
        self._pending = None
        self._seq = 0
        self._proc = None
        self._closed = False
        self._full: "OrderedDict[str, str]" = OrderedDict()        # whole frames behind zoomed views (.bmp, few)
        self.fit_size: Optional[Tuple[int, int]] = None             # set by the caller: size of the fit picture
        self._fit_size = None                                       # size of the last whole-frame picture (the fit one)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def request(self, project: Project, t: float, size: Tuple[int, int], focus_id: Optional[str],
                original: bool = False, view: Optional[Tuple[int, int, int, int, float]] = None) -> int:
        """
        Ask for a picture; the project is copied, so the caller may keep editing it.

        @param focus_id  id of the layer being edited (its lower layers get cached), or None
        @param original  True = the picture without any effect (to compare with the edited one)
        @param view      None = the whole frame at `size`. Else (x, y, w, h, zoom): only that part of the project
                         picture (project pixels), delivered as a picture of round(w*zoom) x round(h*zoom) pixels
        @return          number of the request, given back to `deliver`
        """
        with self._cond:
            self._seq += 1
            shown = copy.deepcopy(project)
            if original:
                for layer in shown.layers:
                    layer.effects = []
            self._pending = (self._seq, shown, t, size, focus_id, view)
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
            self._cond.notify()
            return self._seq

    def set_cache_dir(self, cache_dir: str) -> bool:
        """
        Move the cache to another folder. The pictures cached so far are dropped, the next request renders again.

        @param cache_dir  folder picked by the user; empty = cache/ in the program folder
        @return           True if the cache was moved, False if that folder is the one in use already
        """
        with self._cond:
            if cache_root(cache_dir) == cache_root(self.cache_dir):
                return False
            old, self.cache, self.cache_dir = self.cache, FrameCache(root=cache_dir), cache_dir
            self._full.clear()
            self._seq += 1                                          # an answer still on its way is outdated
            if self._proc and self._proc.poll() is None:
                self._proc.kill()                                   # it writes into the old folder
        old.close()
        return True

    def close(self) -> None:
        with self._cond:
            self._closed = True
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
            self._cond.notify()
        self._thread.join(timeout=3)
        self.cache.close()

    def _on_start(self, proc) -> None:
        with self._cond:
            self._proc = proc
            if self._pending is not None:                           # replaced while starting
                proc.kill()

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                seq, project, t, size, focus_id, view = self._pending
                self._pending = None
            path, error = None, None
            try:
                if view:
                    early = self._interim(project, t, size, view, focus_id)           # smooth picture from the fit one, at once
                    if early:
                        with self._cond:
                            current = seq == self._seq and not self._closed
                        if current:
                            self.deliver(seq, early, None)
                    path = self._render_view(project, t, size, focus_id, view)
                else:
                    self._fit_size = size
                    path = self._render(project, t, size, focus_id)
            except (FFmpegError, CompileError) as exc:
                error = str(exc)
            with self._cond:
                current = seq == self._seq and not self._closed
            if current:
                self.deliver(seq, path, error)

    @staticmethod
    def _view_frame(project: Project, size: Tuple[int, int], zoom: float) -> Tuple[int, int]:
        """@return size of the whole frame that is rendered behind a view at this zoom"""
        scale = next((b for b in VIEW_SCALES if b >= zoom - 1e-9), 1.0)     # fixed steps: zoom steps share a frame
        return (even(project.width * scale), even(project.height * scale)) if scale < 1.0 else size

    def _interim(self, project: Project, t: float, size: Tuple[int, int],
                 view: Tuple[int, int, int, int, float], focus_id: Optional[str] = None) -> Optional[str]:
        """
        A quick, soft picture of a view made from the fit picture (already cached), scaled smoothly instead of in
        blocks. Only made while the sharp frame behind the view is not cached yet.

        @return the picture file, None if there is nothing to make it from (or it is not needed)
        """
        fit = self.fit_size or self._fit_size
        if self.tools is None or not fit:
            return None
        x, y, w, h, zoom = view
        cache, stack = self.cache, active_layers(project, t)
        full = self._view_frame(project, size, zoom)
        box, out = self._view_box(project, full, view)
        held = self._full.get(frame_key(project, stack, t, full))
        if cache.get(self._view_key(project, stack, t, full, box, out)) or (held and os.path.exists(held)):
            return None                                             # the sharp one is quick now
        src = cache.get(frame_key(project, stack, t, fit))
        if not src:                                                 # no fit picture yet: it is small, so render it first
            try:
                src = self._render(project, t, fit, focus_id)
            except (FFmpegError, CompileError):
                return None
        fw, fh = fit
        sx, sy = fw / float(project.width), fh / float(project.height)
        bx, by = min(int(math.floor(x * sx)), fw - 1), min(int(math.floor(y * sy)), fh - 1)
        bw = max(1, min(fw - bx, int(math.ceil((x + w) * sx)) - bx))
        bh = max(1, min(fh - by, int(math.ceil((y + h) * sy)) - by))
        kx, ky = zoom / sx, zoom / sy                               # fit pixels -> screen pixels
        mw, mh = max(1, int(round(bw * kx))), max(1, int(round(bh * ky)))
        ox, oy = int(round((x * sx - bx) * kx)), int(round((y * sy - by) * ky))
        ow, oh = max(1, min(mw - ox, int(round(w * zoom)))), max(1, min(mh - oy, int(round(h * zoom))))
        key = hashlib.sha1(("soft|%s|%s" % (os.path.basename(src), [bx, by, bw, bh, mw, mh, ox, oy, ow, oh]))
                           .encode("utf-8")).hexdigest()
        hit = cache.get(key)
        if hit:
            return hit
        vf = "crop=%d:%d:%d:%d,scale=%d:%d:flags=lanczos,crop=%d:%d:%d:%d" % (bw, bh, bx, by, mw, mh, ow, oh, ox, oy)
        try:
            proc = popen(_base(self.tools) + ["-i", src, "-vf", vf, "-frames:v", "1", "-update", "1", cache.path(key)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except OSError:
            return None
        self._on_start(proc)
        proc.communicate()
        out = cache.path(key)
        if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) == 0:
            return None
        cache.add(key)
        return out

    @staticmethod
    def _view_box(project: Project, full: Tuple[int, int], view: Tuple[int, int, int, int, float]):
        """@return (x, y, w, h) of the view in the pixels of the `full` frame, and (w, h) of the picture to deliver"""
        x, y, w, h, zoom = view
        sx, sy = full[0] / float(project.width), full[1] / float(project.height)
        cx, cy = min(int(round(x * sx)), full[0] - 1), min(int(round(y * sy)), full[1] - 1)
        cw, ch = max(1, min(full[0] - cx, int(round(w * sx)))), max(1, min(full[1] - cy, int(round(h * sy))))
        return (cx, cy, cw, ch), (max(1, int(round(cw / sx * zoom))), max(1, int(round(ch / sy * zoom))))

    def _view_key(self, project: Project, stack: List[Layer], t: float, full: Tuple[int, int], box, out) -> str:
        return hashlib.sha1(("view|%s|%s|%s" % (frame_key(project, stack, t, full), list(box), list(out)))
                            .encode("utf-8")).hexdigest()

    def _keep_full(self, key: str, path: str) -> None:
        """Remember a whole frame (the newest FULL_FRAMES stay, the older files are removed)."""
        self._full[key] = path
        self._full.move_to_end(key)
        while len(self._full) > FULL_FRAMES:
            _old, gone = self._full.popitem(last=False)
            try:
                os.remove(gone)
            except OSError:
                pass

    def _render_view(self, project: Project, t: float, size: Tuple[int, int], focus_id: Optional[str],
                     view: Tuple[int, int, int, int, float]) -> str:
        """
        Zoomed picture. The first time, ONE FFmpeg run renders the frame, cuts the visible part out of it and also
        keeps the whole frame (uncompressed, so it is written at once). Moving around afterwards only cuts a new
        part out of that frame, which takes a fraction of a second. Effects run on the whole frame (at the size the
        zoom needs), so the result is what the export shows.
        """
        if self.tools is None:
            raise FFmpegError("FFmpeg not found")
        zoom = view[4]
        cache, stack = self.cache, active_layers(project, t)
        full = self._view_frame(project, size, zoom)
        box, out = self._view_box(project, full, view)
        key = self._view_key(project, stack, t, full, box, out)
        hit = cache.get(key)
        if hit:
            return hit
        fkey = frame_key(project, stack, t, full)
        src = self._full.get(fkey)
        if src and not os.path.exists(src):
            self._full.pop(fkey, None)
            src = None
        flags = "neighbor" if out[0] >= box[2] * 3 else "bicubic"           # big zoom: hard pixels, else smooth
        cut = "crop=%d:%d:%d:%d,scale=%d:%d:flags=%s" % (box[2], box[3], box[0], box[1], out[0], out[1], flags)
        if src:                                                             # moving around: cut from the kept frame
            try:
                proc = popen(_base(self.tools) + ["-i", src, "-vf", cut, "-frames:v", "1", "-update", "1",
                                                  cache.path(key)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            except OSError as exc:
                raise FFmpegError(str(exc))
            self._on_start(proc)
            err = proc.communicate()[1].decode("utf-8", "replace")
            done = cache.path(key)
            if proc.returncode != 0 or not os.path.exists(done) or os.path.getsize(done) == 0:
                raise FFmpegError(_tail(err))
        else:
            job = compile_frame(project, stack, t, full)
            job.graph += ";[%s]split[zfull][zraw];[zraw]%s[zview]" % (job.video, cut)
            job.video, job.prefix = "zview", "zfull"
            keep = os.path.join(cache.folder, fkey + ".bmp")
            render_frame(self.tools, job, cache.path(key), keep, self._on_start)
            self._keep_full(fkey, keep)
        cache.add(key)
        return cache.path(key)

    def _render(self, project: Project, t: float, size: Tuple[int, int], focus_id: Optional[str]) -> str:
        if self.tools is None:
            raise FFmpegError("FFmpeg not found")
        cache = self.cache                                          # kept: set_cache_dir() may swap self.cache
        stack = active_layers(project, t)
        final_key = frame_key(project, stack, t, size)
        hit = cache.get(final_key)
        if hit:
            return hit
        focus = next((i for i, l in enumerate(stack) if l.id == focus_id), 0)
        layers, base, tap, prefix_key = stack, None, None, None
        if focus > 0:
            prefix_key = frame_key(project, stack[:focus], t, size)
            base = cache.get(prefix_key)
            if base:
                layers, prefix_key = stack[focus:], None
            else:
                tap = focus
        job = compile_frame(project, layers, t, size, base=base, tap=tap)
        render_frame(self.tools, job, cache.path(final_key),
                     cache.path(prefix_key) if prefix_key else None, self._on_start)
        cache.add(final_key)
        if prefix_key:
            cache.add(prefix_key)
        return cache.path(final_key)


def save_frame(tools: Tools, project: Project, t: float, path: str) -> None:
    """
    Render the picture at second t in full project size to an image file (.png or .jpg).

    @raise FFmpegError, CompileError on failure
    """
    job = compile_frame(project, active_layers(project, t), t, (project.width, project.height))
    render_frame(tools, job, path)
