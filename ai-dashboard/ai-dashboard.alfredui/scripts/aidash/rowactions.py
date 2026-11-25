"""What the buttons and menu items do."""
import os
import shlex
import subprocess
from pathlib import Path

from . import archiving, config, helpers, quota, replies, resume


def usable_path(path):
    """An absolute path without control characters. Paths come from session logs."""
    return os.path.isabs(path) and not any(ord(c) < 32 or ord(c) == 127 for c in path)


# Folder extensions that macOS treats as something to run or install rather than browse.
BUNDLE_SUFFIXES = {".action", ".alfredui", ".app", ".appex", ".bundle", ".component", ".driver", ".kext", ".mailbundle",
                   ".mdimporter", ".mpkg", ".osax", ".pkg", ".plugin", ".prefpane", ".qlgenerator", ".saver",
                   ".scptd", ".service", ".wdgt", ".workflow"}


def folder_command(folder):
    """Open a folder in Finder. An app or other bundle is only revealed, because opening it can run it."""
    real = Path(os.path.realpath(folder))
    bundle = (real.suffix.lower() in BUNDLE_SUFFIXES or (real / "Contents/Info.plist").exists()
              or (real / "Contents/MacOS").exists())
    return ["/usr/bin/open"] + (["-R"] if bundle else []) + [str(real)]


def action(name, uid, answer=""):
    cache = helpers.read_json(config.CACHE_DIR / "index.json", {})
    if name == "limits-setup":
        return {} if cache.get("demo") else quota.limits_help()
    session = cache.get("sessions", {}).get(uid)
    if not session:
        return replies.message("Select a session first", True)
    # The two context-dependent buttons mean different things in the Archived view.
    if name == "primary":
        name = "restore" if session.get("archived") else "resume"
    if name == "remove":
        name = "purge" if session.get("archived") else "delete"
    if name in ("restore", "delete", "purge", "delete-confirmed", "purge-confirmed"):
        return archiving.change_action(name, uid, session, cache, answer)
    if session.get("archived") and name in ("resume", "copycmd"):
        return replies.message("Restore this session first")
    if name in ("resume", "copycmd", "folder") and session["cwd"] and not usable_path(session["cwd"]):
        return replies.message("This session records an unusual folder path, so it was not used", True)
    if name == "copyid":
        return dict(replies.message("Copied session ID"), copy={"type": "text", "src": session["id"]})
    if name == "copycmd":
        command = " ".join(shlex.quote(p) for p in resume.resume_command(session))
        if session["cwd"]:
            command = "cd %s && %s" % (shlex.quote(session["cwd"]), command)
        return dict(replies.message("Copied resume command"), copy={"type": "text", "src": helpers.clean(command)})
    if cache.get("demo"):
        return replies.message("Demo data: opening and resuming are turned off")
    if name == "reveal":
        if not (usable_path(session["path"]) and os.path.isfile(session["path"])):
            return replies.message("Session log no longer exists", True)
        subprocess.run(["/usr/bin/open", "-R", session["path"]], check=True)
        return replies.message("Revealed session log in Finder")
    if not os.path.isdir(session["cwd"]):
        return replies.message("Project folder no longer exists: %s" % helpers.short_folder(session["cwd"]), True)
    if name == "folder":
        subprocess.run(folder_command(session["cwd"]), check=True)
        return replies.message("Opened project folder")
    if name == "resume":
        script = config.CACHE_DIR / ("resume-%s.command" % session["id"])
        helpers.write_private(script, resume.resume_script(session), 0o700)
        resume.open_session(session, script)
        return {"finish": {}}
    return replies.message("Unknown action", True)
