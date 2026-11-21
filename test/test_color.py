"""Tests of the Highlights effect and its Color slider."""

import os
import shutil
import subprocess

import pytest

from engine import effect as fx


def _vf(amount, color):
    """
    @param amount  value of the amount slider
    @param color   value of the color slider
    @return        the filters of Highlights joined with commas
    """
    fx.discover()
    return ",".join(fx.get("highlights").filters({"amount": amount, "color": color}, None))


def _pixel(amount, color, rgb=(230, 128, 51)):
    """
    @param amount  value of the amount slider
    @param color   value of the color slider
    @param rgb     colour of the test picture
    @return        the r, g, b bytes FFmpeg makes of that colour with Highlights applied
    """
    colour = "0x%02X%02X%02X" % rgb
    run = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=%s:s=16x16:d=0.1" % colour,
                          "-vf", _vf(amount, color) + ",format=rgb24", "-frames:v", "1", "-f", "rawvideo", "-"],
                         capture_output=True)
    assert run.returncode == 0, run.stderr
    return tuple(run.stdout[:3])


def test_color_defaults_to_zero_and_amount_zero_is_neutral():
    fx.discover()
    color = [p for p in fx.get("highlights").params if p.key == "color"][0]
    assert color.default == 0 and color.min == 0 and color.max == 100
    assert fx.get("highlights").filters({"amount": 0, "color": 100}, None) == []


def test_color_zero_keeps_the_filter_of_before():
    assert "1+" in _vf(60, 0) and "(1-" in _vf(60, 0)
    assert _vf(60, 0) != _vf(60, 100)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_color_100_is_the_legacy_highlights():
    assert _pixel(60, 100) == (242, 163, 73)           # amount 60 of the older versions, same input colour


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg is not installed")
def test_color_100_is_half_the_amount_of_color_0_on_gray():
    assert _pixel(60, 0) == (254, 142, 56)                # hue kept: the three channels get one gain
    gray = (128, 128, 128)                                 # the older highlights used half of today's amount scale
    assert _pixel(60, 100, gray) == _pixel(30, 0, gray)
