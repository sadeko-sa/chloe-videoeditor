"""
Tests of the project crop (Edit > Crop video): the numbers, the filter graph (the cut comes first and the frame
follows it), the real pixels (against FFmpeg), the command line (--crop, --auto-crop) and the black bar detection.
"""

import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cvecli                                                     # noqa: E402
from engine import autocrop                                       # noqa: E402
from engine import effect as fx                                   # noqa: E402
from engine import ffmpeg as ff                                   # noqa: E402
from engine.compiler import compile_export, compile_frame         # noqa: E402
from engine.project import MIN_FRAME, MediaInfo, Project, normalize_cuts  # noqa: E402

fx.discover()
TOOLS = ff.locate("")
needs_ffmpeg = pytest.mark.skipif(TOOLS is None, reason="FFmpeg not found")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _make_image(path, width, height):
    """Write a test picture (colour bars, a moving diagonal and noise patches) of the given size."""
    subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=%dx%d:d=1" % (width, height),
                    "-frames:v", "1", path], check=True)


def _project_of(path, width, height):
    """@return a project with one image layer that has the size of the file (so it sets the frame)"""
    project = Project()
    project.add_media(MediaInfo(path, width, height, 30.0, 0.0, False, True))
    return project


def _render(project, size, path):
    """Render the first second of the project at `size` into `path` (a PNG) and return the path."""
    ff.render_frame(TOOLS, compile_frame(project, project.layers, 0.0, size), path)
    return path


def _rgb(path):
    """@return the picture as raw rgb24 bytes"""
    out = subprocess.run([TOOLS.ffmpeg, "-v", "error", "-i", path, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, check=True)
    return out.stdout


def _cut_of(src, dst, left, top, width, height):
    """Cut a rectangle out of a finished picture with FFmpeg (the reference the real result is compared with)."""
    subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", "-i", src, "-vf",
                    "crop=%d:%d:%d:%d:exact=1" % (width, height, left, top), "-frames:v", "1", dst], check=True)
    return dst


def _classes(buf):
    """@return one 3 bit class (which of R, G, B is above half) per pixel; blind to small colour shifts"""
    return [(buf[i] > 127) | ((buf[i + 1] > 127) << 1) | ((buf[i + 2] > 127) << 2) for i in range(0, len(buf), 3)]


def _mismatch(a, b):
    """@return part (0..1) of the pixels that are of another colour class in the two pictures (same size)"""
    ca, cb = _classes(_rgb(a)), _classes(_rgb(b))
    assert len(ca) == len(cb), "the pictures have different sizes"
    return sum(1 for x, y in zip(ca, cb) if x != y) / float(len(ca))


# --------------------------------------------------------------------------- #
# The numbers
# --------------------------------------------------------------------------- #


def test_cuts_are_made_even_and_never_negative():
    assert normalize_cuts((3, 5, 7, 9), 1920, 1080) == (2, 4, 6, 8)
    assert normalize_cuts((-4, 0, 1, 2), 1920, 1080) == (0, 0, 0, 2)
    assert normalize_cuts(("x", None, 2.9, True), 1920, 1080) == (0, 0, 2, 0)


@pytest.mark.parametrize("cuts", [(1000, 1000, 600, 600), (5000, 1, 5000, 1), (1904, 0, 0, 1064)])
def test_a_picture_is_always_left(cuts):
    left, right, top, bottom = normalize_cuts(cuts, 1920, 1080)
    assert 1920 - left - right >= MIN_FRAME and 1080 - top - bottom >= MIN_FRAME
    assert (1920 - left - right) % 2 == 0 and (1080 - top - bottom) % 2 == 0


def test_not_four_numbers_is_no_cut():
    assert normalize_cuts([1, 2, 3], 640, 480) == (0, 0, 0, 0)
    assert normalize_cuts(None, 640, 480) == (0, 0, 0, 0)


def test_picture_size_follows_the_crop():
    project = Project(1920, 1080, 30.0)
    assert (project.out_width, project.out_height) == (1920, 1080)
    project.set_crop(140, 140, 0, 0)
    assert (project.out_width, project.out_height) == (1640, 1080)
    project.set_crop(0, 0, 138, 138)
    assert (project.out_width, project.out_height) == (1920, 804)


def test_crop_is_saved_only_when_there_is_one_and_comes_back():
    project = Project(1280, 720, 30.0)
    assert "crop" not in project.to_dict()
    project.set_crop(10, 20, 30, 40)
    data = json.loads(json.dumps(project.to_file_dict()))
    assert data["crop"] == [10, 20, 30, 40]
    loaded = Project.from_dict(data)
    assert loaded.cuts() == (10, 20, 30, 40) and (loaded.out_width, loaded.out_height) == (1250, 650)
    assert loaded == Project.from_dict(data)                      # the crop is part of the edit (undo sees it)


def test_a_broken_crop_in_a_file_is_no_crop():
    data = Project(640, 480, 30.0).to_file_dict()
    for bad in ("x", [1, 2], {"a": 1}, [None, "a", 1.5, {}]):
        data["crop"] = bad
        assert Project.from_dict(data).cuts() == (0, 0, 0, 0)


def test_layer_size_is_kept_in_the_file():
    project = Project()
    project.add_media(MediaInfo("a.png", 640, 480, 30.0, 0.0, False, True))
    data = json.loads(json.dumps(project.to_file_dict()))
    assert (data["layers"][0]["width"], data["layers"][0]["height"]) == (640, 480)
    layer = Project.from_dict(data).layers[0]
    assert (layer.width, layer.height) == (640, 480)
    del data["layers"][0]["height"]                               # an old or damaged file: both or none
    layer = Project.from_dict(data).layers[0]
    assert (layer.width, layer.height) == (0, 0)


def test_changing_the_frame_keeps_the_crop_usable():
    project = Project(1920, 1080, 30.0)
    project.set_crop(900, 900, 0, 0)
    project.width = 1000                                          # the frame got smaller than the cuts
    assert project.out_width >= MIN_FRAME and project.out_width % 2 == 0


# --------------------------------------------------------------------------- #
# The graph
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_no_crop_no_crop_filter(tmp_path):
    src = str(tmp_path / "a.png")
    _make_image(src, 320, 240)
    graph = compile_export(_project_of(src, 320, 240)).graph
    assert "crop=" not in graph and "pad=" not in graph


@needs_ffmpeg
def test_cut_comes_before_the_scaler_and_the_canvas_has_the_new_size(tmp_path):
    src = str(tmp_path / "a.png")
    _make_image(src, 320, 240)
    project = _project_of(src, 320, 240)
    project.set_crop(40, 20, 10, 30)
    graph = compile_export(project).graph
    assert graph.count("crop=") == 1
    assert graph.index("crop=260:200:40:10") < graph.index("scale=260:200")       # cut first, scale (if any) after
    assert "s=260x200" in graph                                                   # the canvas follows the crop
    assert "pad=" not in graph                                                    # a file of the frame's size needs none


@needs_ffmpeg
def test_file_of_another_size_is_fitted_then_cut_with_transparent_margins(tmp_path):
    src = str(tmp_path / "b.png")
    _make_image(src, 640, 480)
    project = _project_of(src, 640, 480)
    project.width, project.height = 1280, 720                     # a 4:3 file in a 16:9 frame
    project.set_crop(300, 100, 20, 60)
    graph = compile_export(project).graph
    assert graph.index("scale=1280:720") < graph.index("pad=1280:720") < graph.index("crop=880:640:300:20")
    assert "black@0" in graph


@needs_ffmpeg
def test_preview_scales_the_cut_with_the_picture(tmp_path):
    src = str(tmp_path / "a.png")
    _make_image(src, 640, 480)
    project = _project_of(src, 640, 480)
    project.set_crop(100, 60, 40, 20)                             # picture 480 x 420
    graph = compile_frame(project, project.layers, 0.0, (240, 210)).graph
    # a preview is not the file's size, so the file is fitted to the (half size) frame 320x240 and then cut: every cut
    # is halved (50, 30, 20, 10), the window is 240x210 at 50,20; the file has the frame's shape, so no padding
    assert "scale=320:240" in graph and "min(240,iw-50)" in graph and "min(210,ih-20)" in graph
    assert "pad=" not in graph


# --------------------------------------------------------------------------- #
# The pixels
# --------------------------------------------------------------------------- #


@needs_ffmpeg
@pytest.mark.parametrize("cuts", [(40, 20, 10, 30), (2, 0, 0, 0), (0, 0, 100, 100), (158, 0, 118, 0)])
def test_file_of_the_frame_size_is_cut_exactly(tmp_path, cuts):
    src = str(tmp_path / "a.png")
    _make_image(src, 320, 240)
    project = _project_of(src, 320, 240)
    whole = _render(project, (320, 240), str(tmp_path / "whole.png"))
    project.set_crop(*cuts)
    left, _right, top, _bottom = project.cuts()
    got = _render(project, (project.out_width, project.out_height), str(tmp_path / "got.png"))
    want = _cut_of(whole, str(tmp_path / "want.png"), left, top, project.out_width, project.out_height)
    assert _rgb(got) == _rgb(want)                                # not one pixel differs


@needs_ffmpeg
@pytest.mark.parametrize("cuts", [(100, 0, 0, 0), (0, 300, 0, 40), (300, 100, 20, 60), (500, 200, 0, 0), (0, 0, 80, 0)])
def test_file_with_margins_lands_in_the_right_place(tmp_path, cuts):
    """A 4:3 file in a 16:9 frame has empty margins; whichever edges are cut it must stay where it was."""
    src = str(tmp_path / "b.png")
    _make_image(src, 640, 480)
    project = _project_of(src, 640, 480)
    project.width, project.height = 1280, 720
    whole = _render(project, (1280, 720), str(tmp_path / "whole.png"))
    project.set_crop(*cuts)
    left, _right, top, _bottom = project.cuts()
    got = _render(project, (project.out_width, project.out_height), str(tmp_path / "got.png"))
    want = _cut_of(whole, str(tmp_path / "want.png"), left, top, project.out_width, project.out_height)
    shifted = _cut_of(whole, str(tmp_path / "shifted.png"), min(left + 2, 1280 - project.out_width), top,
                      project.out_width, project.out_height)
    # colours differ a little between the two ways of laying a smaller picture over the canvas, so compare the colour
    # classes: a few edge pixels may differ, a shift of two pixels must be clearly worse
    assert _mismatch(got, want) < 0.03
    if left + 2 <= 1280 - project.out_width and project.out_width < 1280:
        assert _mismatch(got, want) <= _mismatch(got, shifted)


@needs_ffmpeg
def test_unknown_file_size_is_still_right(tmp_path):
    """Older project files do not know the size of their files: the result must be the same, only less direct."""
    src = str(tmp_path / "a.png")
    _make_image(src, 320, 240)
    project = _project_of(src, 320, 240)
    whole = _render(project, (320, 240), str(tmp_path / "whole.png"))
    project.set_crop(40, 20, 10, 30)
    project.layers[0].width = project.layers[0].height = 0
    got = _render(project, (project.out_width, project.out_height), str(tmp_path / "got.png"))
    want = _cut_of(whole, str(tmp_path / "want.png"), 40, 10, project.out_width, project.out_height)
    assert _mismatch(got, want) < 0.03


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #


def test_crop_option_is_top_bottom_left_right_like_handbrake():
    assert cvecli.parse_crop("10:20:30:40") == (30, 40, 10, 20)     # returned as left, right, top, bottom
    for bad in ("1:2:3", "a:b:c:d", "1:2:3:4:5", "-1:0:0:0", ""):
        with pytest.raises(Exception):
            cvecli.parse_crop(bad)


@needs_ffmpeg
def test_export_has_the_size_that_is_left(tmp_path):
    """The whole way: command line, filter graph, encoder, file. No size is set by hand."""
    clip = str(tmp_path / "clip.mp4")
    subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=10",
                    "-pix_fmt", "yuv420p", clip], check=True)
    out = str(tmp_path / "out.mp4")
    code = cvecli.main(["-i", clip, "-o", out, "--crop", "10:20:30:40", "--quiet"])
    assert code == 0
    info = ff.probe(TOOLS, out)
    assert (info.width, info.height) == (320 - 30 - 40, 240 - 10 - 20)


@needs_ffmpeg
def test_project_crop_is_used_and_the_command_line_replaces_it(tmp_path):
    clip = str(tmp_path / "clip.mp4")
    subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=10",
                    "-pix_fmt", "yuv420p", clip], check=True)
    project = Project()
    project.add_media(ff.probe(TOOLS, clip))
    project.set_crop(20, 20, 0, 0)
    path = str(tmp_path / "p.cvproj")
    from engine.project import save_project
    save_project(project, path)
    saved, replaced = str(tmp_path / "saved.mp4"), str(tmp_path / "replaced.mp4")
    assert cvecli.main(["-i", path, "-o", saved, "--quiet"]) == 0
    assert ff.probe(TOOLS, saved).width == 280
    assert cvecli.main(["-i", path, "-o", replaced, "--crop", "0:0:0:0", "--quiet"]) == 0
    assert ff.probe(TOOLS, replaced).width == 320


# --------------------------------------------------------------------------- #
# Black bars
# --------------------------------------------------------------------------- #


def _letterboxed(tmp_path):
    """@return a 640x360 clip whose picture is 640x270 with a black bar above (45 px) and below (45 px)"""
    clip = str(tmp_path / "bars.mp4")
    subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=s=640x270:d=3:r=10,pad=640:360:0:45:black,format=yuv420p", clip], check=True)
    return clip


@needs_ffmpeg
def test_auto_crop_finds_the_bars(tmp_path):
    project = Project()
    project.add_media(ff.probe(TOOLS, _letterboxed(tmp_path)))
    left, right, top, bottom = autocrop.detect(TOOLS, project)
    assert (left, right) == (0, 0)
    assert 44 <= top <= 46 and 44 <= bottom <= 46                 # 45 rounded to even, never into the picture by much
    project.set_crop(left, right, top, bottom)
    assert project.out_height in (268, 270, 272)


@needs_ffmpeg
def test_auto_crop_finds_nothing_where_there_are_no_bars(tmp_path):
    clip = str(tmp_path / "plain.mp4")
    subprocess.run([TOOLS.ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1:r=10",
                    "-pix_fmt", "yuv420p", clip], check=True)
    project = Project()
    project.add_media(ff.probe(TOOLS, clip))
    assert autocrop.detect(TOOLS, project) == (0, 0, 0, 0)


@needs_ffmpeg
def test_auto_crop_ignores_the_crop_the_project_already_has(tmp_path):
    project = Project()
    project.add_media(ff.probe(TOOLS, _letterboxed(tmp_path)))
    project.set_crop(100, 100, 0, 0)
    first = autocrop.detect(TOOLS, project)
    assert project.cuts() == (100, 100, 0, 0)                     # the project is not changed
    assert first[0] == 0 and first[1] == 0                        # the bars are looked for in the whole frame


def test_sample_times_are_in_the_middle_of_equal_parts():
    assert autocrop.sample_times(8.0, 4) == [1.0, 3.0, 5.0, 7.0]
