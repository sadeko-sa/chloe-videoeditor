"""Shared fixtures of the tests."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def isolated_userdata(monkeypatch, tmp_path_factory, tmp_path):
    """
    Every test gets its own default user data folder, so no test reads or writes the real AppData / ~/.config,
    and a program folder without the config.json of an old layout (the checkout may have one), so init() never
    mistakes the checkout for an old installation.

    @param monkeypatch       pytest fixture that undoes the changes after the test
    @param tmp_path_factory  pytest fixture that makes folders outside tmp_path (a test may list tmp_path)
    @param tmp_path          pytest fixture: a folder of its own for the test
    """
    from engine import userdata
    default = tmp_path_factory.mktemp("userdata") / "default"      # not made yet, like a first start
    monkeypatch.setattr(userdata, "default_dir", lambda: str(default))
    monkeypatch.setattr(userdata, "APP_DIR", str(tmp_path / "no_program_folder"))
