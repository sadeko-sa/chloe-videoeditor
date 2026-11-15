"""Tests of the output naming: the format with tokens, the folder and the numbering (no FFmpeg, no Tk)."""

import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import encode as enc                                  # noqa: E402
from engine import naming                                         # noqa: E402
from engine.config import load_config                             # noqa: E402
from engine.project import MediaLayer, Project                    # noqa: E402

NOW = datetime.datetime(2026, 11, 15, 14, 30, 5)


def _ctx(options=None, source="clip", project="proj"):
    return naming.NameContext(source, project, NOW, options or enc.ExportOptions(), 1920, 1080, 30.0)


def test_tokens():
    o = enc.ExportOptions(video="x265", crf=25, height=720)
    text = naming.render("{source}_{project}_{date}_{time}_{codec}_{quality}_{width}x{height}_{fps}", _ctx(options=o))
    assert text == "clip_proj_2026.11.15_14.30.05_h265_crf25_1280x720_30"


def test_bitrate_and_same_size():
    o = enc.ExportOptions(rate=enc.RATE_BITRATE, bitrate=6000)
    assert naming.render("{quality}-{width}x{height}", _ctx(options=o)) == "6000k-1920x1080"


def test_unknown_token_and_forbidden_characters():
    assert naming.render("{source}-{nope}", _ctx()) == "clip-{nope}"
    assert naming.render('a/b:c*"d', _ctx()) == "a_b_c__d"
    assert naming.render("{project}. ", _ctx(project="")) == "video"


def test_append_token():
    assert naming.append_token("{source}-", "date") == "{source}-{date}"


def test_unique_path():
    used = {"a.mp4", "a (2).mp4"}
    assert naming.unique_path("b.mp4", used.__contains__) == "b.mp4"
    assert naming.unique_path("a.mp4", used.__contains__) == "a (3).mp4"


def test_folder_and_input(tmp_path):
    media = tmp_path / "in"
    media.mkdir()
    project = Project()
    project.layers.append(MediaLayer("v", path=str(media / "v.mp4")))
    assert naming.output_folder("", project) == str(media)
    assert naming.output_folder(str(tmp_path), project) == str(tmp_path)
    assert naming.output_folder(str(tmp_path / "gone"), project) == str(media)
    assert naming.is_input(str(media / "v.mp4"), project)
    assert not naming.is_input(str(media / "w.mp4"), project)
    assert naming.context_for(project, "", enc.ExportOptions(), NOW).source == "v"


def test_config_values_checked(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.config.config_path", lambda: str(tmp_path / "c.json"))
    (tmp_path / "c.json").write_text('{"on_exists": "x", "name_format": " ", "output_dir": 3}')
    cfg = load_config()
    assert (cfg["on_exists"], cfg["name_format"], cfg["output_dir"]) == ("ask", naming.DEFAULT_FORMAT, "")
