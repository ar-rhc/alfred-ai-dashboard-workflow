#!/usr/bin/env python3
"""AI Dashboard: local Claude Code and Codex sessions for Alfred UI.

Prints Alfred UI JSON actions. Reads local session logs, plus the cached usage
figures in Claude Code's .claude.json; makes no network requests and never opens
credential files. Must stay Python 3.9 compatible.

Modes: appearance <argument> | init | load [force] | filter <all|claude|codex> | view toggle | action <name> <uid> [dont-ask-again]
Sessions are removed after a confirmation, unless the user turned it off.
"""
import json
import os
import re
import sys

from aidash import config, replies, rowactions, screen


def window_appearance(background):
    """"dark" or "light" to match the Alfred theme's background colour, such as rgba(0,0,0,0.20)."""
    parts = re.findall(r"[\d.]+", background or "")
    try:
        red, green, blue = (float(p) for p in parts[:3])
    except ValueError:
        return "automatic"  # fewer than three numbers: leave it to macOS
    return "dark" if 0.2126 * red + 0.7152 * green + 0.0722 * blue < 128 else "light"


def main(argv):
    mode = argv[1] if len(argv) > 1 else "load"
    if mode == "appearance":
        # Run by the workflow before the separate window opens; not a UI action reply.
        look = window_appearance(os.environ.get("alfred_theme_background"))
        print(json.dumps({"alfredworkflow": {"arg": argv[2] if len(argv) > 2 else "",
                                             "variables": {"window_appearance": look}}}))
        return
    try:
        config.configure(os.environ)
        if mode == "init":
            reply = screen.load(force=True, first=True)
        elif mode == "load":
            reply = screen.load(force=argv[2:] == ["force"])
        elif mode == "filter":
            chosen = argv[2] if len(argv) > 2 and argv[2] in config.TOOL_NAMES else "all"
            os.environ["filter"] = chosen
            reply = screen.load(force=True)
            reply["variables"]["filter"] = chosen
        elif mode == "view":
            os.environ["view"] = "active" if os.environ.get("view") == "archived" else "archived"
            reply = screen.load(force=True)
            reply["variables"]["view"] = os.environ["view"]
        elif mode == "action":
            reply = rowactions.action(*argv[2:5])
        else:
            reply = replies.message("Unknown mode", True)
    except Exception as exc:  # surface failures in the window instead of a blank dashboard
        print("%s: %s" % (type(exc).__name__, exc), file=sys.stderr)
        # ValueError messages are written for the user.
        reply = replies.message(str(exc) if isinstance(exc, ValueError) else "Dashboard error: %s" % type(exc).__name__, True)
    print(json.dumps(reply))


if __name__ == "__main__":
    main(sys.argv)
