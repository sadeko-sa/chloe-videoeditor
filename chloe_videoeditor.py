#!/usr/bin/env python3
"""
chloe_videoeditor.py - Chloe Videoeditor, a small cross platform video editor (Windows, Linux, macOS)
that uses FFmpeg for all picture and sound processing. Needs Python 3.8+, Tkinter and FFmpeg 4.4+.

    python chloe_videoeditor.py [project (.cvproj), video or image]

See README.md. Effects are plugins in the effects/ folder, texts in lang/, colors in theme/.
"""

import sys

from engine import effect
from engine.config import load_config
from ui.common import detach_console, win_setup


def main(argv):
    """@param argv  command line without the program name; an optional project or media file"""
    detach_console(__file__)
    cfg = load_config()
    effect.discover()
    win_setup()
    from ui.app import App                      # after win_setup(): DPI settings must come before Tk
    App(cfg).run(argv[0] if argv else None)


if __name__ == "__main__":
    main(sys.argv[1:])
