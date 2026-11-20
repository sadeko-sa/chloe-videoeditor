"""Backend of Chloe Videoeditor: no Tk in here, only the project model, the FFmpeg filter graph and processes."""

import os
import subprocess

APP_NAME = "Chloe Videoeditor"


def _read_version() -> str:
    """
    Program version from git: the VERSION file of an export (git describe, written by the export tools), else
    `git describe --tags --always` of a checkout, else "dev".
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
    except OSError:
        pass
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        out = subprocess.run(["git", "describe", "--tags", "--always"], cwd=root, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, creationflags=flags)
        text = out.stdout.decode("utf-8", "replace").strip()
        if out.returncode == 0 and text:
            return text
    except (OSError, subprocess.SubprocessError):
        pass
    return "dev"


VERSION = _read_version()
"""Program version, e.g. v0.1.0-20-g1f5de1e; see _read_version()."""
