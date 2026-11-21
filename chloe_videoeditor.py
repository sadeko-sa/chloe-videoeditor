#!/usr/bin/env python3
"""
chloe_videoeditor.py - Chloe Videoeditor, a small cross platform video editor (Windows, Linux, macOS)
that uses FFmpeg for all picture and sound processing. Needs Python 3.8+, Tkinter and FFmpeg 4.4+.

    python chloe_videoeditor.py [project (.cvproj), video or image]
    python chloe_videoeditor.py -i input -o output [options]      (no window: cvecli.py, see --help)

See README.md. Effects are plugins in the effect/ folder (and in effect/ of the user data folder), texts in lang/, colors
in theme/. Config, presets, user effects, and by default the preview cache and error.log are in the user data folder (engine/userdata.py;
Settings > General > User data folder).
"""

import sys

import cvecli
from engine import effect, preview, userdata
from engine.config import load_config


def main(argv):
    """@param argv  command line without the program name; an optional project or media file, or options for cvecli"""
    if cvecli.wants_cli(argv):                # first: no Tk needed, and detach_console() would take the console away
        sys.exit(cvecli.main(argv))           # cvecli.main() calls userdata.init() itself
    from ui.common import detach_console, install_error_log, set_log_folder, win_setup     # imports Tkinter
    detach_console(__file__)
    userdata.init()                             # userdata.json in the default folder; before config and error.log
    install_error_log()
    cfg = load_config()
    set_log_folder(preview.cache_root(cfg["cache_dir"]))     # error.log lives in the cache folder
    effect.discover()
    effect.discover_user(cfg["effect_override"])
    win_setup()
    from ui.app import App                      # after win_setup(): DPI settings must come before Tk
    App(cfg).run(argv[0] if argv else None)


if __name__ == "__main__":
    main(sys.argv[1:])
