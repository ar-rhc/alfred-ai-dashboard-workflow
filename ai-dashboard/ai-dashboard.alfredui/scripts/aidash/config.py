"""Settings: fixed values and what the Workflow Configuration can change.

Other modules read these as config.NAME when they run, so configure() applies everywhere."""
import os
import re
import tempfile
from pathlib import Path


HOME = Path.home()
CLAUDE_HOME = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
CODEX_HOME = Path(os.environ.get("CODEX_HOME") or HOME / ".codex")
CACHE_DIR = Path(os.environ.get("alfred_workflow_cache") or Path(tempfile.gettempdir()) / "ai-dashboard")
CACHE_VERSION = 3
LIMITS_FILE = "ai-dashboard-rate-limits.json"  # written by the user's Claude Code status line
LIST_LIMIT = 60          # newest sessions listed per tool
STATS_DAYS = 7
ARCHIVE_DIR = "ai-dashboard-archive"  # beside projects/, so Claude Code does not list it
LSOF = "/usr/sbin/lsof"  # part of macOS
# Alfred starts scripts with a minimal PATH; these are the usual places package managers use.
BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", str(Path.home() / ".local/bin"),
            "/run/current-system/sw/bin")
RECENT_WRITE_SECONDS = 60
FULL_ID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
PREVIEW_MESSAGES = 6
PREVIEW_CHARS = 700
ENABLED = ("claude", "codex")
LIMIT_STYLE = "left"      # or "used"
REFRESH_SECONDS = 30      # 0 turns automatic refresh off
ALLOW_PURGE = True
TERMINAL_APP = ""         # empty: whatever opens .command files; "custom": TERMINAL_COMMAND
TERMINAL_COMMAND = ""
TMUX_MODE = "auto"        # "auto": use tmux when it is on screen; "always"; "never"
TMUX_ARGS = ()            # extra tmux arguments, such as a test socket
EXTRA_ARGS = {"claude": "", "codex": ""}
ACTIVE_SECONDS = 180
SESSION_ID = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{7,63}$")
PLACEHOLDER = re.compile(r"\{(?=(?:var|const|file|fileraw|value|data|param):)")
TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex"}
TOOL_COLOURS = {"claude": "systemOrangeColor", "codex": "systemTealColor"}


def number(text, default, low, high):
    try:
        return min(high, max(low, int(str(text).strip())))
    except ValueError:
        return default


def configure(env):
    """Apply Workflow Configuration values; anything missing or blank keeps its default."""
    global CLAUDE_HOME, CODEX_HOME, LIST_LIMIT, PREVIEW_MESSAGES
    global ENABLED, LIMIT_STYLE, REFRESH_SECONDS, ALLOW_PURGE, TERMINAL_APP, EXTRA_ARGS, TMUX_MODE, TERMINAL_COMMAND
    value = lambda name: (env.get(name) or "").strip()
    if value("claude_folder"):
        CLAUDE_HOME = Path(os.path.expanduser(value("claude_folder")))
    if value("codex_folder"):
        CODEX_HOME = Path(os.path.expanduser(value("codex_folder")))
    LIST_LIMIT = number(value("list_limit"), LIST_LIMIT, 5, 500)
    PREVIEW_MESSAGES = number(value("preview_count"), PREVIEW_MESSAGES, 0, 30)
    REFRESH_SECONDS = number(value("refresh_seconds"), REFRESH_SECONDS, 0, 1000)
    ENABLED = tuple(t for t in ("claude", "codex") if value("show_" + t) != "0")
    LIMIT_STYLE = "used" if value("limit_style") == "used" else "left"
    ALLOW_PURGE = value("allow_purge") != "0"
    TERMINAL_APP = value("terminal_app")
    TERMINAL_COMMAND = value("terminal_command")
    TMUX_MODE = value("tmux_mode") if value("tmux_mode") in ("always", "never") else "auto"
    EXTRA_ARGS = {"claude": value("claude_args"), "codex": value("codex_args")}
