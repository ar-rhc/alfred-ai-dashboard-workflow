"""Sessions and usage for each tool."""
import glob
import json
import os
import re
import sqlite3
import time
from pathlib import Path

from . import config, helpers, logparse, quota


def summarise(usage):
    days = dict.fromkeys(helpers.recent_days(), 0)
    for stamp, fresh, _cached in usage.values():
        day = helpers.day_of(stamp)
        if day in days:
            days[day] += fresh
    return list(days.values())


def collect_claude(parser, archived=False):
    root = config.CLAUDE_HOME / (config.ARCHIVE_DIR if archived else "projects")
    pattern = glob.escape(str(root))
    top = sorted(glob.glob(pattern + "/*/*.jsonl"), key=helpers.mtime, reverse=True)
    subagents = glob.glob(pattern + "/*/*/subagents/**/*.jsonl", recursive=True)
    cutoff = time.time() - (config.STATS_DAYS + 1) * 86400
    by_session = {}
    for path in subagents:
        parts = Path(path).relative_to(root).parts
        by_session.setdefault((parts[0], parts[1]), []).append(path)

    usage, sessions, week = {}, [], 0
    for path in top:
        project, session = Path(path).parent.name, Path(path).stem
        children = by_session.pop((project, session), [])
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
            sessions.append({
                "tool": "claude", "id": session, "path": path, "cwd": state["cwd"],
                "title": state["custom_title"] or state["title"] or state["first_prompt"] or "Untitled session",
                "branch": state["branch"], "model": state["model"], "updated": modified,
                "tokens": sum(u[1] for u in own.values()), "messages": state["messages"],
            })
    for children in by_session.values():  # subagents whose parent log is gone
        for child in children:
            if helpers.mtime(child) >= cutoff:
                state = parser.parse(child, "claude", "", True)
                if state:
                    usage.update(state["usage"])
    if not archived:
        # Archiving a session does not undo the usage it already consumed.
        stored = glob.escape(str(config.CLAUDE_HOME / config.ARCHIVE_DIR))
        for path in glob.glob(stored + "/*/*.jsonl") + glob.glob(stored + "/*/*/subagents/**/*.jsonl", recursive=True):
            if helpers.mtime(path) >= cutoff:
                state = parser.parse(path, "claude", "", "subagents" in Path(path).parts)
                if state:
                    usage.update(state["usage"])
    return {"sessions": sessions, "days": summarise(usage), "week": week,
            "limits": quota.claude_limits(), "found": root.is_dir() or (config.CLAUDE_HOME / "projects").is_dir()}


def codex_threads(archived=False):
    """Read the interactive thread index (newest state database), tolerating schema changes."""
    databases = glob.glob(glob.escape(str(config.CODEX_HOME)) + "/state_*.sqlite")
    if not databases:
        return []
    newest = max(databases, key=lambda p: int(re.sub(r"\D", "", Path(p).stem) or 0))
    try:
        return query_threads(Path(newest).as_uri() + "?mode=ro", archived)
    except sqlite3.OperationalError:
        # Once Codex closes the database its write-ahead log is gone, and a read-only connection
        # cannot create one. The file is then complete, so it is safe to read as it stands.
        if os.path.exists(newest + "-wal"):
            raise
        return query_threads(Path(newest).as_uri() + "?mode=ro&immutable=1", archived)


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
    cutoff = time.time() - (config.STATS_DAYS + 1) * 86400
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
