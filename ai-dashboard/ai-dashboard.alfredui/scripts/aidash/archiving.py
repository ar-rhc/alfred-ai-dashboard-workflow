"""Archiving, restoring and permanently deleting sessions."""
import glob
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, helpers, replies, screen


def running(pid):
    try:
        os.kill(int(pid), 0)
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    return True


def held_open(paths):
    """Whether any process has one of these files open. Raises if that cannot be established."""
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return False
    result = subprocess.run([config.LSOF, "-t", "--"] + paths, capture_output=True, text=True, timeout=15)
    if result.stdout.strip():
        return True
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise OSError("could not check for open files")
    return False


def open_reason(session):
    """Why a session must not be removed right now, or an empty string."""
    if session["tool"] == "claude":
        # Claude Code lists each running process, with its session, in sessions/<pid>.json.
        for record in glob.glob(glob.escape(str(config.CLAUDE_HOME / "sessions")) + "/*.json"):
            data = helpers.read_json(record, {})
            if data.get("sessionId") == session["id"] and running(data.get("pid")):
                return "This session is open in Claude Code. Exit it first."
    else:
        lock = str(config.CODEX_HOME / "thread-writer-locks" / (session["id"] + ".lock"))
        if held_open([lock, session["path"]]):
            return "Codex still has this session open. Close it (or quit Codex) first."
    if time.time() - helpers.mtime(session["path"]) < config.RECENT_WRITE_SECONDS:
        return "This session changed in the last minute. Try again shortly."
    return ""


def claude_files(session):
    """The log and per-session folder, refusing anything outside the live or archive project folders."""
    log = Path(session["path"])
    root = config.CLAUDE_HOME / (config.ARCHIVE_DIR if session.get("archived") else "projects")
    if (log.name != session["id"] + ".jsonl" or log.is_symlink() or not log.is_file()
            or log.parent.resolve().parent != root.resolve()):
        raise ValueError("Unexpected session log location")
    folder = log.parent / session["id"]
    return ([folder] if folder.is_dir() and not folder.is_symlink() else []) + [log]


def move_claude(session, destination):
    """Move a session between the live and archive folders, all of it or none of it."""
    files = claude_files(session)
    target = destination / Path(session["path"]).parent.name
    if any(os.path.lexists(str(target / f.name)) for f in files):
        raise ValueError("A session with this ID is already there")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    moved = []
    try:
        for path in files:
            os.rename(str(path), str(target / path.name))
            moved.append(path)
    except OSError:
        for path in moved:
            os.rename(str(target / path.name), str(path))
        raise


def run_codex(*args):
    binary = helpers.find_binary("codex")
    if not binary:
        raise ValueError("The codex command was not found")
    environment = dict(os.environ, CODEX_HOME=str(config.CODEX_HOME))
    environment["PATH"] = os.pathsep.join((os.path.dirname(binary), environment.get("PATH", "/usr/bin:/bin"))
                                          + config.BIN_DIRS)
    result = subprocess.run([binary] + list(args), capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL, env=environment)
    if result.returncode:
        print(result.stderr.strip(), file=sys.stderr)
        raise ValueError("Codex could not %s this session" % args[0])


def change_session(session, change):
    """change is "archive", "restore" or "purge" (permanent)."""
    if not config.FULL_ID.match(session["id"]):
        raise ValueError("Unexpected session ID")
    if session["tool"] == "codex":
        command = {"archive": ("archive",), "restore": ("unarchive",), "purge": ("delete", "--force")}[change]
        run_codex(*command, session["id"])
    elif change == "purge":
        for path in claude_files(session):
            if path.is_dir():
                shutil.rmtree(str(path))
            else:
                path.unlink()
    else:
        move_claude(session, config.CLAUDE_HOME / ("projects" if change == "restore" else config.ARCHIVE_DIR))
    verb = {"archive": "Archived", "restore": "Restored", "purge": "Permanently deleted"}[change]
    return "%s %s session" % (verb, config.TOOL_NAMES[session["tool"]])


def confirm_sheet(session, uid, permanent):
    claude = session["tool"] == "claude"
    if permanent:
        heading, button, mode = "Delete permanently?", "Delete Permanently", "purge-confirmed"
        detail = ("The conversation log and its session folder are erased. Prompt history and file "
                  "checkpoints stay." if claude else "Runs codex delete.") + " This cannot be undone."
    else:
        heading, button, mode = "Archive session?", "Archive", "delete-confirmed"
        detail = "It leaves the list. Restore it any time from the Archived view."
    close = replies.close_popup("confirmDelete")
    # The session is fixed here, so a list refresh cannot change what gets removed.
    proceed = [close, {"type": "script", "src": "scripts/run", "args": ["action", mode, uid]}]
    return replies.popup("confirmDelete", [
        replies.label(heading, font={"size": "large", "weight": "semibold"}, constraints={"size": {"width": 420}}),
        replies.label("%s: %s" % (config.TOOL_NAMES[session["tool"]], helpers.one_line(session.get("title"), 55)),
                      linebreak="truncatetail"),
        replies.label(helpers.short_folder(session["cwd"]), colour="secondaryLabelColor", linebreak="truncatemiddle"),
        replies.label(detail, colour="systemRedColor" if permanent else "secondaryLabelColor"),
        {"type": "view", "layout": "border", "components": [
            replies.button("Cancel", close, shortcut="↩", constraints={"region": "left"}),
            replies.button(button, proceed, constraints={"region": "right"}),
        ]},
    ])


def change_action(name, uid, session, cache):
    """Archive and permanent deletion ask first; restoring does not remove anything, so it just runs."""
    change = "restore" if name == "restore" else "purge" if name.startswith("purge") else "archive"
    if change == "purge" and not config.ALLOW_PURGE:
        return replies.message("Permanent deletion is turned off in the workflow configuration", True)
    if change == "restore" and not session.get("archived"):
        return replies.message("This session is not archived")
    if change == "archive" and session.get("archived"):
        return replies.message("This session is already archived")
    demo = cache.get("demo")
    if change != "restore" and not demo:
        reason = open_reason(session)
        if reason:
            return replies.message(reason, True)
    if change != "restore" and not name.endswith("-confirmed"):
        return confirm_sheet(session, uid, change == "purge")
    if demo:
        return replies.message("Demo data: nothing was changed")
    done = change_session(session, change)
    # Keep the place in the list: select whatever followed the session that left it.
    order = list(cache["sessions"])
    place = order.index(uid)
    following = (order[place + 1:] + order[:place][::-1] + [""])[0]
    os.environ["selected"] = following
    reply = screen.load(force=True)
    reply["variables"]["selected"] = following
    reply["update"]["ids"]["status"] = {"text": done, "colour": "secondaryLabelColor"}
    return reply
