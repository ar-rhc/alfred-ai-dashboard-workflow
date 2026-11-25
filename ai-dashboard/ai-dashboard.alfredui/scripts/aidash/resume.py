"""Resuming a session in a terminal or tmux."""
import os
import shlex
import subprocess
from pathlib import Path

from . import config, helpers


def resume_command(session):
    if not config.SESSION_ID.match(session["id"]):
        raise ValueError("Unexpected session ID")
    tool = session["tool"]
    extra = shlex.split(config.EXTRA_ARGS.get(tool, ""))
    if tool == "claude":
        return ["claude"] + extra + ["--resume", session["id"]]
    return ["codex", "resume"] + extra + [session["id"]]


def folder_exports(tool):
    """Point the CLI at a non-default data folder, matching what the dashboard listed."""
    name, folder, usual = (("CLAUDE_CONFIG_DIR", config.CLAUDE_HOME, config.HOME / ".claude") if tool == "claude"
                           else ("CODEX_HOME", config.CODEX_HOME, config.HOME / ".codex"))
    return "" if folder.resolve() == usual.resolve() else "export %s=%s\n" % (name, shlex.quote(str(folder)))


def shell_script(*lines):
    return "\n".join(("#!%s -il" % helpers.user_shell(),) + lines) + "\n"


def resume_script(session):
    command = " ".join(shlex.quote(part) for part in resume_command(session))
    exports = folder_exports(session["tool"])
    found = helpers.find_binary(session["tool"])
    folders = list(config.BIN_DIRS) + ([os.path.dirname(found)] if found else [])
    return shell_script(
        'rm -f -- "$0"',
        'export PATH="$PATH":%s' % shlex.quote(os.pathsep.join(dict.fromkeys(folders))),
        # Claude Code looks up its saved sign-in by user name.
        'export USER="${USER:-$(/usr/bin/id -un)}"',
        'export LOGNAME="${LOGNAME:-$USER}"',
        *(([exports.rstrip("\n")] if exports else []) + [
            "cd -- %s || exit 1" % shlex.quote(session["cwd"]),
            command,
            # Stay in a shell afterwards, so errors remain readable.
            'exec "${SHELL:-%s}" -il' % helpers.user_shell()]))


def open_in_terminal(script):
    if config.TERMINAL_APP == "custom":
        parts = shlex.split(config.TERMINAL_COMMAND)
        if not parts:
            raise ValueError("Enter a custom terminal command in the workflow configuration")
        # Detached, because a terminal started directly keeps running until its window closes.
        subprocess.Popen([os.path.expanduser(p) for p in parts] + [str(script)], env=outside_tmux(),
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return
    if config.TERMINAL_APP == "Ghostty":
        # -e replaces any configured startup command; the extra instance quits with its window.
        command = ["/usr/bin/open", "-na", "Ghostty", "--args", "--quit-after-last-window-closed=true",
                   "-e", str(script)]
    elif config.TERMINAL_APP:
        command = ["/usr/bin/open", "-a", config.TERMINAL_APP, str(script)]
    else:
        command = ["/usr/bin/open", str(script)]
    subprocess.run(command, check=True, env=outside_tmux())


def outside_tmux():
    """The environment without tmux variables, which would point at a particular server or pane."""
    return {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}


def tmux_binary():
    return helpers.find_binary("tmux")


def tmux(*args):
    """Run a tmux command; None if there is no server or the command failed."""
    binary = tmux_binary()
    if not binary:
        return None
    # Without a UTF-8 locale tmux replaces the tab separators in its output.
    result = subprocess.run([binary, "-u"] + list(config.TMUX_ARGS) + list(args), capture_output=True, text=True, timeout=10,
                            env=outside_tmux())
    return result.stdout if result.returncode == 0 else None


def newest(listing):
    rows = [line.split("\t") for line in (listing or "").splitlines() if line]
    return sorted(rows, key=lambda r: int(r[0] or 0), reverse=True)


def app_of(pid):
    """The macOS app bundle a process is running inside, found through its parents."""
    for _ in range(12):
        result = subprocess.run(["/bin/ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True)
        parts = result.stdout.strip().split(None, 1)
        if len(parts) != 2:
            return ""
        if ".app/Contents/MacOS/" in parts[1]:
            return parts[1].split(".app/Contents/MacOS/")[0] + ".app"
        pid = parts[0]
        if pid in ("0", "1"):
            return ""
    return ""


def visible_client():
    """(session id, app) of the most recently used tmux client in the chosen terminal."""
    listing = tmux("list-clients", "-F", "#{client_activity}\t#{client_pid}\t#{session_id}")
    for _activity, pid, session in newest(listing):
        app = app_of(pid)
        if config.TERMINAL_APP in ("", "custom") or Path(app).stem.lower() == config.TERMINAL_APP.lower():
            return session, app
    return "", ""


def open_session(session, script):
    """Run the resume script in tmux or in a terminal window, as configured."""
    client, app = visible_client() if config.TMUX_MODE != "never" else ("", "")
    if config.TMUX_MODE == "never" or (config.TMUX_MODE == "auto" and not client):
        return open_in_terminal(script)
    binary = tmux_binary()
    if not binary:
        raise ValueError("tmux was not found. Set tmux to Never in the workflow configuration.")
    target = client
    if not target:
        sessions = newest(tmux("list-sessions", "-F", "#{session_activity}\t#{session_id}"))
        target = sessions[0][1] if sessions else ""
    base = [binary] + list(config.TMUX_ARGS)
    join = lambda parts: "exec " + " ".join(shlex.quote(str(p)) for p in parts)
    if target:
        # The folder is not passed to tmux, which would expand "#{...}" in its name; the script changes to it.
        if tmux("new-window", "-t", target + ":", "-n", session["tool"], shlex.quote(str(script))) is None:
            raise ValueError("tmux could not open a new window")
        if client and app:
            subprocess.run(["/usr/bin/open", "-a", app], check=True)  # bring it to the front
            return None
        follow = join(base + ["attach-session", "-t", target])
    else:
        follow = join(base + ["new-session", shlex.quote(str(script))])
    # Starting tmux from a real terminal gives it the normal environment.
    opener = script.with_name("tmux-" + script.name)
    helpers.write_private(opener, shell_script('rm -f -- "$0"', "unset TMUX TMUX_PANE", follow), 0o700)
    return open_in_terminal(opener)
