"""Tests of the user data folder: the OS default, the always present userdata.json and the folders that follow it."""

import json
import os
from pathlib import Path

import pytest

from engine import config, preset, preview, userdata
from engine import effect as fx


@pytest.fixture
def home(monkeypatch, tmp_path):
    """
    A fake user profile: HOME / APPDATA / XDG_CONFIG_HOME point into tmp_path and default_dir() is the real one.

    @param monkeypatch  pytest fixture that undoes the changes after the test
    @param tmp_path     pytest fixture: a folder of its own for the test
    @return             the folder that acts as the user's home
    """
    monkeypatch.undo()                                       # drop the autouse isolation: default_dir() is tested itself
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return tmp_path


@pytest.fixture
def clean_registry(monkeypatch):
    """
    An empty effect registry for a test; the real one comes back afterwards.

    @param monkeypatch  pytest fixture that undoes the changes after the test
    """
    monkeypatch.setattr(fx, "_effects", {})
    monkeypatch.setattr(fx, "_load_errors", [])
    monkeypatch.setattr(fx, "_overrides", [])


def _write_effect(folder, file_name, effect_id, name="X"):
    """
    Write a one-effect plugin file.

    @param folder     folder of the file (made if needed)
    @param file_name  file name with .py
    @param effect_id  id of the effect in the file
    @param name       name of the effect
    """
    os.makedirs(str(folder), exist_ok=True)
    (folder / file_name).write_text(
        "from engine.effect import ChainEffect, register\n\n\n@register\nclass E(ChainEffect):\n"
        "    id = %r\n    name = %r\n    def filters(self, v, ctx):\n        return ['negate']\n" % (effect_id, name),
        encoding="utf-8")


def test_default_dir_is_the_settings_folder_of_the_os(monkeypatch, home):
    monkeypatch.setattr(userdata, "IS_WIN", True)
    monkeypatch.setattr(userdata, "IS_MAC", False)
    assert userdata.default_dir() == os.path.join(str(home / "roaming"), "ChloeVideoeditor")
    monkeypatch.setattr(userdata, "IS_WIN", False)
    monkeypatch.setattr(userdata, "IS_MAC", True)
    assert userdata.default_dir() == os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                                                  "ChloeVideoeditor")
    monkeypatch.setattr(userdata, "IS_MAC", False)
    assert userdata.default_dir() == os.path.join(os.path.expanduser("~"), ".config", "ChloeVideoeditor")


def test_default_dir_uses_xdg_only_when_it_is_absolute(monkeypatch, home):
    monkeypatch.setattr(userdata, "IS_WIN", False)
    monkeypatch.setattr(userdata, "IS_MAC", False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    assert userdata.default_dir() == os.path.join(str(home / "xdg"), "ChloeVideoeditor")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    assert userdata.default_dir() == os.path.join(os.path.expanduser("~"), ".config", "ChloeVideoeditor")


def test_without_a_choice_everything_is_in_the_default_folder():
    base = userdata.default_dir()
    assert userdata.custom_dir() == "" and userdata.userdata_dir() == base
    assert config.config_path() == os.path.join(base, "config.json")
    assert preset.preset_dir() == os.path.join(base, "preset")
    assert fx.effect_dir() == os.path.join(base, "effect")
    assert preview.default_root() == base


def test_init_writes_userdata_json_in_the_default_folder():
    assert userdata.init() == userdata.default_dir()
    with open(userdata.bootstrap_path(), encoding="utf-8") as f:
        data = json.load(f)
    assert data == {"userdata_dir": "", "effective_dir": userdata.default_dir(), "default_dir": userdata.default_dir()}


def test_a_custom_folder_is_remembered_in_the_default_folder_and_used(tmp_path):
    custom = str(tmp_path / "mine")
    assert userdata.set_userdata_dir(custom) == custom
    with open(userdata.bootstrap_path(), encoding="utf-8") as f:       # still in the DEFAULT folder
        data = json.load(f)
    assert data["userdata_dir"] == custom and data["effective_dir"] == custom
    assert data["default_dir"] == userdata.default_dir()
    assert userdata.custom_dir() == custom and userdata.userdata_dir() == custom
    assert config.config_path() == os.path.join(custom, "config.json")
    assert preset.preset_dir() == os.path.join(custom, "preset")
    assert fx.effect_dir() == os.path.join(custom, "effect")
    assert preview.default_root() == custom


def test_an_empty_choice_goes_back_to_the_default_folder(tmp_path):
    userdata.set_userdata_dir(str(tmp_path / "mine"))
    assert userdata.set_userdata_dir("") == userdata.default_dir()
    assert userdata.read_bootstrap()["userdata_dir"] == "" and userdata.custom_dir() == ""


def test_save_config_makes_the_folder_and_the_config_is_read_back(tmp_path):
    userdata.set_userdata_dir(str(tmp_path / "deep" / "er"))
    cfg = config.load_config()
    cfg["last_dir"] = "somewhere"
    config.save_config(cfg)
    assert os.path.isfile(str(tmp_path / "deep" / "er" / "config.json"))
    assert config.load_config()["last_dir"] == "somewhere"


def test_bad_userdata_json_means_the_default_folder():
    os.makedirs(userdata.default_dir())
    for text in ("{broken", "[1, 2]", '{"userdata_dir": 5}'):
        with open(userdata.bootstrap_path(), "w", encoding="utf-8") as f:
            f.write(text)
        assert userdata.custom_dir() == "" and userdata.userdata_dir() == userdata.default_dir()


def test_init_keeps_an_old_layout_next_to_the_program(monkeypatch, tmp_path):
    program = tmp_path / "program"
    program.mkdir()
    (program / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(userdata, "APP_DIR", str(program))
    assert userdata.init() == str(program)
    assert userdata.read_bootstrap()["userdata_dir"] == str(program)
    (program / "config.json").unlink()                               # the choice is made once, not on every start
    assert userdata.init() == str(program)


def test_init_never_replaces_a_choice_the_user_made(tmp_path):
    userdata.set_userdata_dir(str(tmp_path / "mine"))
    assert userdata.init() == str(tmp_path / "mine")


def test_init_survives_a_default_folder_that_cannot_be_written(monkeypatch, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where a folder should be", encoding="utf-8")
    monkeypatch.setattr(userdata, "default_dir", lambda: str(blocker / "sub"))
    assert userdata.init() == str(blocker / "sub")                   # no exception: the program still starts


def test_error_log_goes_to_the_user_data_folder(tmp_path):
    common = pytest.importorskip("ui.common")
    userdata.set_userdata_dir(str(tmp_path / "logs"))
    common.write_error_log("boom")
    with open(str(tmp_path / "logs" / "error.log"), encoding="utf-8") as f:
        assert "boom" in f.read()


def test_error_log_follows_the_cache_folder(tmp_path):
    common = pytest.importorskip("ui.common")
    try:
        common.set_log_folder(str(tmp_path / "cachedir"))
        common.write_error_log("in cache")
        assert "in cache" in (tmp_path / "cachedir" / "error.log").read_text(encoding="utf-8")
        common.set_log_folder("")
        common.write_error_log("in userdata")
        assert "in userdata" in open(os.path.join(userdata.userdata_dir(), "error.log"), encoding="utf-8").read()
    finally:
        common.set_log_folder("")


def test_cache_cleanup_in_the_user_data_folder_removes_only_marked_run_folders(tmp_path):
    root = tmp_path / "ud"
    (root / "run_mine").mkdir(parents=True)
    (root / "run_mine" / preview.RUN_MARK).write_text("", encoding="utf-8")
    (root / "run_users").mkdir()
    (root / "preset").mkdir()
    userdata.set_userdata_dir(str(root))
    folder = preview._make_folder("")
    assert not (root / "run_mine").exists() and (root / "run_users").exists() and (root / "preset").exists()
    assert os.path.dirname(folder) == str(root)


def test_the_program_folder_is_no_longer_the_place_of_the_user_files():
    assert config.config_path() != os.path.join(userdata.APP_DIR, "config.json")


def test_effect_override_is_a_bool_in_the_config(tmp_path):
    userdata.set_userdata_dir(str(tmp_path / "mine"))
    cfg = config.load_config()
    assert cfg["effect_override"] is False
    cfg["effect_override"] = True
    config.save_config(cfg)
    assert config.load_config()["effect_override"] is True
    (tmp_path / "mine" / "config.json").write_text('{"effect_override": "yes"}', encoding="utf-8")
    assert config.load_config()["effect_override"] is False


def test_user_effect_is_added(tmp_path, clean_registry):
    _write_effect(Path(userdata.default_dir()) / "effect", "mine.py", "mine", "Mine")
    assert fx.discover_user() == 1
    assert fx.get("mine").name == "Mine" and fx.load_errors() == []


def test_user_effect_with_a_used_id_is_rejected_unless_override_is_on(tmp_path, clean_registry):
    _write_effect(tmp_path / "builtin", "base.py", "flip", "Built-in")
    fx.discover_folder(str(tmp_path / "builtin"))
    _write_effect(Path(userdata.default_dir()) / "effect", "mine.py", "flip", "Mine")
    fx.discover_user()
    assert fx.get("flip").name == "Built-in" and len(fx.load_errors()) == 1 and fx.overrides() == []
    fx.discover_user(override=True)
    assert fx.get("flip").name == "Mine" and fx.overrides() == ["flip"]


def test_a_broken_user_file_is_reported_and_takes_nothing_with_it(tmp_path, clean_registry):
    folder = Path(userdata.default_dir()) / "effect"
    _write_effect(folder, "a_good.py", "good")
    (folder / "b_bad.py").write_text(
        "from engine.effect import ChainEffect, register\n\n\n@register\nclass E(ChainEffect):\n"
        "    id = 'half'\n    name = 'Half'\n    def filters(self, v, ctx):\n        return []\n\n\nraise ValueError('boom')\n",
        encoding="utf-8")
    _write_effect(folder, "_helper.py", "helper")
    fx.discover_user()
    assert fx.get("good") is not None and fx.get("half") is None and fx.get("helper") is None
    assert len(fx.load_errors()) == 1 and "boom" in fx.load_errors()[0]


def test_the_program_effect_folder_is_not_loaded_twice(monkeypatch, tmp_path, clean_registry):
    program = tmp_path / "program"
    _write_effect(program / "effect", "mine.py", "mine")
    (program / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(userdata, "APP_DIR", str(program))
    assert userdata.init() == str(program)                           # old layout: effect_dir() is the built-in package
    assert fx.effect_dir() == str(program / "effect")
    assert fx.discover_user() == 0 and fx.load_errors() == []
