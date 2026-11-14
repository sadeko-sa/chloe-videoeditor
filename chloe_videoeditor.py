#!/usr/bin/env python3
"""
chloe_videoeditor.py - Chloe Videoeditor, a small cross platform video editor (Windows, Linux, macOS)
that uses FFmpeg for all picture and sound processing. Needs Python 3.8+, Tkinter and FFmpeg 4.4+.

    python chloe_videoeditor.py [project (.cvproj), video or image]
    python chloe_videoeditor.py -i input -o output [options]      (no window: cvecli.py, see --help)

See README.md. Effects are plugins in the effect/ folder, texts in lang/, colors in theme/.
"""

import sys

import cvecli
from engine import effect
from engine.config import load_config


def main(argv):
    """@param argv  command line without the program name; an optional project or media file, or options for cvecli"""
    if cvecli.wants_cli(argv):                # first: no Tk needed, and detach_console() would take the console away
        sys.exit(cvecli.main(argv))
    from ui.common import detach_console, win_setup     # imports Tkinter
    detach_console(__file__)
    cfg = load_config()
    effect.discover()
    win_setup()
    from ui.app import App                      # after win_setup(): DPI settings must come before Tk
    App(cfg).run(argv[0] if argv else None)


if __name__ == "__main__":
    main(sys.argv[1:])
