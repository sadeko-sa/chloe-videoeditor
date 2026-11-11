"""Tests of the crop: the box geometry and the filters (against real FFmpeg)."""

import os
import random
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import effect as fx                                   # noqa: E402
from engine import ffmpeg as ff                                   # noqa: E402
from engine.compiler import compile_frame                         # noqa: E402
from engine.cropbox import ZONES, CropBox                         # noqa: E402
from engine.project import AdjustmentLayer, EffectInstance, Project  # noqa: E402

fx.discover()
TOOLS = ff.locate("")
needs_ffmpeg = pytest.mark.skipif(TOOLS is None, reason="FFmpeg not found")


def _valid(box):
    ratio = box.ratio()
    return (box.x0 >= -1e-6 and box.y0 >= -1e-6 and box.x1 <= box.width + 1e-6 and box.y1 <= box.height + 1e-6
            and box.w >= box.min_side - 1e-6 and box.h >= box.min_side - 1e-6
            and (ratio is None or abs(box.w / box.h - ratio) < 1e-6 * max(1.0, ratio)))


@pytest.mark.parametrize("frame", [(1920, 1080), (1080, 1920), (100, 40), (4000, 300)])
@pytest.mark.parametrize("ratio", [None, 1.0, 4 / 3.0, 16 / 9.0])
def test_box_stays_valid(frame, ratio):
    rnd, box = random.Random(5), CropBox(*frame)
    box.set_ratio(ratio)
    for _ in range(300):
        zone = rnd.choice(ZONES)
        x, y = rnd.uniform(-0.2, 1.2) * box.width, rnd.uniform(-0.2, 1.2) * box.height
        box.begin(zone, x, y)
        box.drag_to(x + rnd.uniform(-1.5, 1.5) * box.width, y + rnd.uniform(-1.5, 1.5) * box.height)
        box.end()
        if rnd.random() < 0.2:
            box.flip()
        assert _valid(box), (zone, box.x0, box.y0, box.x1, box.y1)


def test_ratio_stays_inside_the_box():
    box = CropBox(1920, 1080)
    box.set_cuts(400, 200, 100, 300)                                            # x 400..1720, y 100..780
    centre = ((box.x0 + box.x1) / 2.0, (box.y0 + box.y1) / 2.0)
    box.set_ratio(1.0)
    assert (box.x0, box.y0, box.x1, box.y1) == (720.0, 100.0, 1400.0, 780.0)   # 680 x 680 around the same centre
    assert ((box.x0 + box.x1) / 2.0, (box.y0 + box.y1) / 2.0) == centre
    box.set_ratio(None)
    assert (box.x0, box.y0, box.x1, box.y1) == (720.0, 100.0, 1400.0, 780.0)   # free keeps the box


def test_ratio_of_a_box_that_has_it_is_only_locked():
    box = CropBox(1920, 1080)
    box.set_cuts(240, 239, 0, 0)                                                # 1441 x 1080, 1 px off 4:3
    box.set_ratio(4 / 3.0)
    assert box.locked and box.cuts() == (240, 239, 0, 0)


def test_flip_of_a_locked_box_turns_it_in_place():
    box = CropBox(1920, 1080)
    box.set_cuts(480, 480, 0, 0)                                                # 960 x 1080
    box.set_ratio(8 / 9.0)
    box.flip()
    assert abs(box.w / box.h - 9 / 8.0) < 1e-9 and box.w <= 1920 and box.h <= 1080
    assert abs((box.x0 + box.x1) / 2.0 - 960) < 1e-9


def test_reset_does_not_touch_the_ratio():
    box = CropBox(1920, 1080)
    box.set_cuts(300, 300, 100, 100)
    box.set_ratio(1.0)
    box.reset()
    assert box.locked and box.ratio() == 1.0 and box.cuts() == (420, 420, 0, 0)    # still 1:1, the largest in the frame
    box.set_ratio(16 / 9.0)
    box.set_cuts(300, 300, 100, 100)
    box.set_ratio(None)
    box.reset()
    assert not box.locked and box.cuts() == (0, 0, 0, 0)                          # still free


def test_box_orientation():
    portrait = CropBox(1080, 1920)
    portrait.set_ratio(16 / 9.0)
    assert not portrait.landscape and portrait.cuts() == (0, 0, 0, 0)          # a tall frame gets the tall form
    box = CropBox(1920, 1080)
    box.set_ratio(4 / 3.0)
    assert box.cuts() == (240, 240, 0, 0)
    box.flip()
    assert abs(box.w / box.h - 3 / 4.0) < 1e-9 and not box.landscape


def _filters(**values):
    eff = fx.get("crop")
    return eff.filters(eff.normalize(values), fx.RenderContext(640, 360, 1.0))


def test_neutral_and_limits():
    assert _filters() == []
    assert fx.get("crop").is_neutral(fx.get("crop").normalize({}), fx.RenderContext(640, 360, 1.0))
    assert _filters(left_px=600, right_px=600, mode="stretch")                  # sum above the limit still builds


def _grey_pixels(project, size):
    """Render the frame and return its luma bytes (FFmpeg writes raw gray, no image library needed)."""
    job = compile_frame(project, project.layers, 0.0, size)
    args = [TOOLS.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"] + job.input_args() + [
        "-filter_complex", job.graph, "-map", "[%s]" % job.video, "-frames:v", "1", "-pix_fmt", "gray",
        "-f", "rawvideo", "-"]
    return subprocess.run(args, stdout=subprocess.PIPE, check=True).stdout


@needs_ffmpeg
def test_mask_cuts_exact_pixels(tmp_path):
    white = str(tmp_path / "white.png")
    subprocess.run([TOOLS.ffmpeg, "-loglevel", "error", "-f", "lavfi", "-i", "color=c=white:s=640x360", "-frames:v", "1",
                    white], check=True)
    project = Project(640, 360, 30.0)
    project.add_media(ff.probe(TOOLS, white))
    inst = EffectInstance.create("crop")
    inst.values.update(left_px=100, top_px=50, mode="mask")
    layer = AdjustmentLayer("a")
    layer.effects.append(inst)
    project.layers.append(layer)
    for size in ((640, 360), (320, 180)):                                      # the export size and a half size preview
        data, k = _grey_pixels(project, size), size[0] / 640.0
        at = lambda x, y: data[int(y * k) * size[0] + int(x * k)]               # noqa: E731  (frame pixel x, y)
        assert at(99.5, 200) < 40 and at(101, 200) > 215                         # 100 px cut from the left
        assert at(300, 49.5) < 40 and at(300, 52) > 215                          # 50 px cut from the top
        assert at(600, 300) > 215                                                # the rest stays
