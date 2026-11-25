"""Sessions and usage for each tool."""
import bisect
import glob
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from . import config, helpers, logparse, quota


def summarise(usage):
    """Fresh tokens on each recent day."""
    starts = helpers.day_starts()
    days = [0] * (len(starts) - 1)
    for stamp, fresh, _cached in usage.values():
        if starts[0] <= stamp < starts[-1]:
            days[bisect.bisect_right(starts, stamp) - 1] += fresh
    return days


def week_cutoff():
    """Logs unchanged since this cannot hold usage from the days counted."""
    return time.time() - (config.STATS_DAYS + 1) * 86400


def claude_logs(root):
    """The session logs under root, newest first, and the subagent logs by (project, session)."""
    pattern = glob.escape(str(root))
    top = sorted(glob.glob(pattern + "/*/*.jsonl"), key=helpers.mtime, reverse=True)
    subagents = {}
    for path in glob.glob(pattern + "/*/*/subagents/**/*.jsonl", recursive=True):
        parts = Path(path).relative_to(root).parts
        subagents.setdefault((parts[0], parts[1]), []).append(path)
    return top, subagents


def recent_usage(parser, paths, is_subagent):
    """Usage by message id from the logs changed recently enough to count."""
    cutoff, usage = week_cutoff(), {}
    for path in paths:
        if helpers.mtime(path) >= cutoff:
            state = parser.parse(path, "claude", "", is_subagent)
            if state:
                usage.update(state["usage"])
    return usage


def claude_session(path, state, modified, usage):
    return {
        "tool": "claude", "id": Path(path).stem, "path": path, "cwd": state["cwd"],
        "title": state["custom_title"] or state["title"] or state["first_prompt"] or "Untitled session",
        "branch": state["branch"], "model": state["model"], "updated": modified,
        "tokens": sum(u[1] for u in usage.values()), "messages": state["messages"],
    }


def scan_claude(parser, root):
    """The newest sessions under root, the usage found by message id and how many were used this week."""
    top, subagents = claude_logs(root)
    cutoff = week_cutoff()
    usage, sessions, week = {}, [], 0
    for path in top:
        project, session = Path(path).parent.name, Path(path).stem
        children = subagents.pop((project, session), [])
        modified = max([helpers.mtime(path)] + [helpers.mtime(c) for c in children])
        if len(sessions) >= config.LIST_LIMIT and modified < cutoff:
            continue
        state = parser.parse(path, "claude", project)
        if state is None:
            continue
        own = dict(state["usage"])
        for child in children:
            child_state = parser.parse(child, "claude", project, True)
            if child_state:
                own.update(child_state["usage"])
        usage.update(own)  # keyed by message id, so copied history is counted once
        if state["entrypoint"].startswith("sdk") or not state["talked"]:
            continue
        if modified >= cutoff:
            week += 1
        if len(sessions) < config.LIST_LIMIT and config.SESSION_ID.match(session):
            sessions.append(claude_session(path, state, modified, own))
    orphans = [path for paths in subagents.values() for path in paths]  # their session log is gone
    usage.update(recent_usage(parser, orphans, True))
    return sessions, usage, week


def collect_claude(parser):
    root = config.CLAUDE_HOME / "projects"
    sessions, usage, week = scan_claude(parser, root)
    # Archiving a session does not undo the usage it already consumed.
    top, subagents = claude_logs(config.CLAUDE_HOME / config.ARCHIVE_DIR)
    usage.update(recent_usage(parser, top, False))
    usage.update(recent_usage(parser, [path for paths in subagents.values() for path in paths], True))
    return {"sessions": sessions, "days": summarise(usage), "week": week,
            "limits": quota.claude_limits(), "found": root.is_dir()}


def archived_claude(parser):
    """The sessions in the archive, for the Archived view."""
    return scan_claude(parser, config.CLAUDE_HOME / config.ARCHIVE_DIR)[0]


def codex_threads(archived=False):
    """Read the interactive thread index (newest state database), tolerating schema changes."""
    databases = glob.glob(glob.escape(str(config.CODEX_HOME)) + "/state_*.sqlite")
    if not databases:
        return []
    newest = max(databases, key=lambda p: int(re.sub(r"\D", "", Path(p).stem) or 0))
    # Once Codex closes the database its write-ahead log is gone and the file is complete, so it is
    # read as it stands. A plain read-only connection would fail, or with newer SQLite add files.
    mode = "?mode=ro" if os.path.exists(newest + "-wal") else "?mode=ro&immutable=1"
    return query_threads(Path(newest).as_uri() + mode, archived)


def query_threads(uri, archived):
    connection = sqlite3.connect(uri, uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        columns = {r[1] for r in connection.execute("PRAGMA table_info(threads)")}
        where = ["1"]
        if "archived" in columns:
            where.append("archived = %d" % archived)
        elif archived:
            where.append("0")
        if "attached_thread_parent_id" in columns:
            where.append("attached_thread_parent_id IS NULL")
        if "preview" in columns:
            where.append("preview <> ''")
        if "thread_source" in columns:
            where.append("COALESCE(thread_source, 'user') = 'user'")
        if "source" in columns:
            where.append("source IN ('cli', 'vscode')")
        order = "updated_at DESC"
        query = "SELECT * FROM threads WHERE %s ORDER BY %s" % (" AND ".join(where), order)
        return [dict(r) for r in connection.execute(query)]
    finally:
        connection.close()


def codex_names():
    names = {}
    try:
        with open(config.CODEX_HOME / "session_index.jsonl", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    names[row["id"]] = row.get("thread_name") or ""
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return names


def codex_title(thread, indexed):
    first = (thread.get("first_user_message") or "").strip()
    stored = (thread.get("title") or "").strip()
    if not config.PREVIEW_MESSAGES:
        # Codex often stores the opening message as the title; that is message text too.
        first_line, start = " ".join(first.split()), " ".join(stored.rstrip("….").split())
        if start and first_line.startswith(start):
            stored = ""
        first = ""
    return indexed or thread.get("name") or stored or first or "Untitled session"


def codex_sessions(parser, threads):
    names = codex_names()
    sessions = []
    for thread in threads:
        if len(sessions) >= config.LIST_LIMIT:
            break
        if not config.SESSION_ID.match(str(thread.get("id") or "")):
            continue
        path = thread.get("rollout_path") or ""
        if path and not os.path.exists(path) and os.path.exists(path + ".zst"):
            path += ".zst"
        state = parser.parse(path, "codex") if path else None
        state = state or logparse.new_state()
        title = codex_title(thread, names.get(thread["id"]))
        sessions.append({
            "tool": "codex", "id": thread["id"], "path": path, "cwd": thread.get("cwd") or "",
            "title": title, "branch": thread.get("git_branch") or "",
            "model": thread.get("model") or state["model"], "updated": thread.get("updated_at") or 0,
            "tokens": thread.get("tokens_used") or 0, "messages": state["messages"],
        })
    return sessions


def collect_codex(parser):
    cutoff = week_cutoff()
    rollouts = []
    for folder in ("sessions", "archived_sessions"):
        for pattern in ("/**/*.jsonl", "/**/*.jsonl.zst"):
            rollouts += glob.glob(glob.escape(str(config.CODEX_HOME / folder)) + pattern, recursive=True)
    rollouts.sort(key=helpers.mtime, reverse=True)

    usage, limits = {}, {}
    # Recent logs give usage; the newest few also hold the last known limits.
    for index, path in enumerate(rollouts):
        if index >= 5 and helpers.mtime(path) < cutoff:
            break
        state = parser.parse(path, "codex")
        if not state:
            continue
        usage.update(state["usage"])
        for key, reading in state["limits"].items():
            if key not in limits or reading[0] > limits[key][0]:
                limits[key] = reading

    error = ""
    try:
        threads = codex_threads()
    except sqlite3.Error as exc:
        threads, error = [], "Codex index unavailable (%s)" % exc
    week = sum((t.get("updated_at") or 0) >= cutoff for t in threads)
    sessions = codex_sessions(parser, threads)
    reading = limits.get("codex") or (max(limits.values(), key=lambda r: r[0]) if limits else None)
    return {"sessions": sessions, "days": summarise(usage), "week": week, "limits": reading,
            "found": config.CODEX_HOME.is_dir(), "error": error}
