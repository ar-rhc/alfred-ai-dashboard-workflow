"""Text and time formatting, safe file access and finding tools."""
import functools
import json
import os
import pwd
import shlex
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from datetime import timedelta

from . import config


def clean(text):
    """Stop log text from being expanded as an Alfred UI placeholder."""
    return config.PLACEHOLDER.sub("{​", text or "")


def one_line(text, limit=90):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clip(text):
    text = (text or "").strip().replace("![", "[")
    if len(text) > config.PREVIEW_CHARS:
        text = text[:config.PREVIEW_CHARS].rstrip() + " …"
    if text.count("```") % 2:
        text += "\n```"
    return text


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def day_starts():
    """Local midnight starting each day counted for usage, oldest first, and then tomorrow's."""
    today = datetime.now().date()
    return [datetime.combine(today - timedelta(days=n), datetime.min.time()).timestamp()
            for n in range(config.STATS_DAYS - 1, -2, -1)]


def tokens(n):
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if n >= size:
            return "%.1f%s" % (n / size, suffix)
    return str(int(n))


def ago(stamp):
    seconds = max(0, time.time() - stamp)
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    if seconds < 86400 * 14:
        return "%dd ago" % (seconds // 86400)
    return datetime.fromtimestamp(stamp).strftime("%-d %b")


def clock(stamp):
    when = datetime.fromtimestamp(stamp)
    if when.date() == datetime.now().date():
        return when.strftime("%H:%M")
    return when.strftime("%a %-d %b %H:%M")


def earlier_day(stamp):
    """The date of a time before today, such as "Wed 25 Nov" (with the year if not this one), else ""."""
    when, today = datetime.fromtimestamp(stamp).date(), datetime.now().date()
    if when >= today:
        return ""
    return when.strftime("%a %-d %b" if when.year == today.year else "%a %-d %b %Y")


def reset_time(stamp):
    """Short enough for a card: the weekday alone is clear within the coming six days."""
    when = datetime.fromtimestamp(stamp)
    if when.date() == datetime.now().date():
        return when.strftime("%H:%M")
    if 0 < stamp - time.time() < 6 * 86400:
        return when.strftime("%a %H:%M")
    return when.strftime("%-d %b %H:%M")


def sparkline(values):
    top = max(values) if values else 0
    if not top:
        return "▁" * len(values)
    return "".join("▁▂▃▄▅▆▇█"[min(7, int(v / top * 7 + 0.5))] for v in values)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_private(path, text, mode=0o600):
    """Atomic write readable only by the current user (the cache holds message excerpts)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@functools.lru_cache(maxsize=None)
def find_binary(name):
    """Locate a command-line tool however it was installed, or None."""
    candidates = [shutil.which(name)] + [os.path.join(folder, name) for folder in config.BIN_DIRS]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    try:  # version managers and custom prefixes are only on the login shell's PATH
        result = subprocess.run([user_shell(), "-ilc", "command -v " + shlex.quote(name)], capture_output=True,
                                text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("/") and os.access(line, os.X_OK):
            return line
    return None


def mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def short_folder(cwd):
    home = str(config.HOME)
    return "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + os.sep) else cwd


def user_shell():
    """The login shell, so resuming sees the same PATH as a normal terminal."""
    try:
        shell = pwd.getpwuid(os.getuid()).pw_shell
    except KeyError:
        shell = ""
    return shell if os.path.basename(shell) in ("zsh", "bash") and os.access(shell, os.X_OK) else "/bin/zsh"
