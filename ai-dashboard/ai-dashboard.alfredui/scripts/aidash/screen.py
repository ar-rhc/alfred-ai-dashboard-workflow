"""What the window shows: layout, cards, toolbar and session rows."""
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import collect, config, helpers, listupdate, logparse, preview, quota, sample


def row_for(session):
    uid = "%s:%s" % (session["tool"], session["id"])
    folder = Path(session["cwd"]).name if session["cwd"] else "no folder"
    active = time.time() - session["updated"] < config.ACTIVE_SECONDS
    title = helpers.one_line(session["title"])
    info = " · ".join(x for x in (session["model"], helpers.tokens(session["tokens"]) + " tokens",
                                  helpers.ago(session["updated"])) if x)
    place = helpers.short_folder(session["cwd"]) or "Folder unknown"
    if session["branch"] and session["branch"] != "HEAD":
        place += "  ⎇ " + session["branch"]
    return {
        "uid": uid,
        "key": uid,
        "rowTitle.text": helpers.clean(("● " if active else "") + title),
        "rowTool.text": config.TOOL_NAMES[session["tool"]],
        "rowTool.colour": config.TOOL_COLOURS[session["tool"]],
        "rowMeta.text": helpers.clean("%s · %s" % (folder, helpers.ago(session["updated"]))),
        "search": helpers.clean(" ".join((config.TOOL_NAMES[session["tool"]], session["title"][:200], session["cwd"],
                                  session["branch"], session["model"]))),
        "title": helpers.clean(title),
        "place": helpers.clean(place),
        "info": helpers.clean(info),
        "preview": helpers.clean(preview.page(session)),
    }


def usage_text(data):
    if not data["found"]:
        return "Not found on this Mac", ""
    return ("Today %s · 7 days %s tokens" % (helpers.tokens(data["days"][-1]), helpers.tokens(sum(data["days"]))),
            "%s  %d session%s this week" % (helpers.sparkline(data["days"]), data["week"], "" if data["week"] == 1 else "s"))


def expand_templates(ui):
    """ui.json with each template use replaced by the component it stands for.

    For code that reads the layout. As in Alfred, a use's own id and constraints win.
    """
    templates = ui.get("templates", {})

    def fill(node, params):
        if isinstance(node, dict):
            return {key: fill(value, params) for key, value in node.items()}
        if isinstance(node, list):
            return [fill(value, params) for value in node]
        if isinstance(node, str):
            for name, value in params.items():
                node = node.replace("{param:%s}" % name, value)
        return node

    def expand(node):
        if isinstance(node, list):
            return [expand(value) for value in node]
        if not isinstance(node, dict):
            return node
        if node.get("type") == "template":
            built = fill(templates[node["src"]], node.get("params", {}))
            built.update({key: node[key] for key in ("id", "constraints") if key in node})
            return expand(built)
        return {key: expand(value) for key, value in node.items()}

    return {key: expand(value) for key, value in ui.items() if key != "templates"}


def layout():
    return expand_templates(helpers.read_json(Path(__file__).resolve().parents[2] / "ui.json", {}))


def walk(node):
    """Every object in a piece of layout."""
    if isinstance(node, dict):
        yield node
    for child in (node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()):
        yield from walk(child)


def component(name):
    return next(n for n in walk(layout()) if n.get("id") == name)


def sized_list(updates):
    """The list with the configured row height, already filled.

    Alfred ignores a variable as a cell height and cannot update it, so the list is replaced.
    """
    rows = component("list")
    rows["cell"]["height"] = config.ROW_HEIGHT
    given = updates.pop("list")
    rows["data"]["src"], rows["select"] = given["data"]["src"], given["select"]
    return rows


def single_card(tool, updates):
    """The remaining card at full width. A grid keeps its column count, so the row is replaced."""
    card = component(tool + "Card")
    for node in walk(card):
        node.update(updates.pop(node.get("id"), {}))
    return {"id": "cards", "type": "view", "layout": "grid", "cols": 1, "rows": 1, "components": [card]}


def collect_data(archived):
    """Both tools' usage, the sessions to list (live or archived) and any index error."""
    empty = {"sessions": [], "days": [0] * config.STATS_DAYS, "week": 0, "found": False}
    old = {"claude": [], "codex": []}
    error = ""
    if sample.is_demo():
        claude, codex = sample.demo_data()
        old = {"claude": claude["sessions"], "codex": codex["sessions"]}
    else:
        parser = logparse.Parser()
        claude = collect.collect_claude(parser) if "claude" in config.ENABLED else empty
        codex = collect.collect_codex(parser) if "codex" in config.ENABLED else empty
        if archived and "claude" in config.ENABLED:
            old["claude"] = collect.archived_claude(parser)
        if archived and "codex" in config.ENABLED:
            try:
                old["codex"] = collect.codex_sessions(parser, collect.codex_threads(archived=True))
            except sqlite3.Error as exc:
                error = "Codex index unavailable (%s)" % exc
        parser.save()
    claude = claude if "claude" in config.ENABLED else empty
    codex = codex if "codex" in config.ENABLED else empty
    listed = old if archived else {"claude": claude["sessions"], "codex": codex["sessions"]}
    sessions = [s for tool in config.ENABLED for s in listed[tool][:config.LIST_LIMIT]]
    sessions.sort(key=lambda s: s["updated"], reverse=True)
    return claude, codex, sessions, error


def save_index(sessions, archived):
    """What the row actions need later, looked up by row ID."""
    index = {"%s:%s" % (s["tool"], s["id"]): dict({k: s[k] for k in ("tool", "id", "cwd", "path", "title")},
                                                   archived=archived) for s in sessions}
    helpers.write_private(config.CACHE_DIR / "index.json",
                          json.dumps({"demo": sample.is_demo(), "sessions": index}))


def fingerprint(sessions, chosen, archived):
    """Changes only when the list should be redrawn. Relative times ("5m ago") are left out."""
    parts = [(chosen, archived, config.LIMIT_STYLE, config.PREVIEW_MESSAGES, s["tool"], s["id"], s["updated"],
              s["tokens"], s["title"], time.time() - s["updated"] < config.ACTIVE_SECONDS) for s in sessions]
    return hashlib.sha1(json.dumps(parts).encode()).hexdigest()


def card_widgets(claude, codex):
    claude_line1, claude_line2 = usage_text(claude)
    codex_line1, codex_line2 = usage_text(codex)
    updates = {
        "claudeUsage": {"text": claude_line1}, "claudeTrend": {"text": claude_line2},
        "codexUsage": {"text": codex_line1}, "codexTrend": {"text": codex_line2},
    }
    updates.update(quota.limit_widgets(codex.get("limits")))
    updates.update(quota.claude_limit_widgets(claude.get("limits")))
    # A switched-off tool has no card to update.
    gone = []
    if "claude" not in config.ENABLED:
        gone += ["claudeUsage", "claudeTrend", "claudeLimit"]
    if "codex" not in config.ENABLED:
        gone += ["codexUsage", "codexTrend", "limit"]
    return {k: v for k, v in updates.items() if not k.startswith(tuple(gone))} if gone else updates


def status_text(count, archived, error):
    if not config.ENABLED:
        return "Both tools are switched off. Turn one on in the workflow configuration."
    if error:
        return error
    text = "%d %s · updated %s" % (count, "archived" if archived else "shown", datetime.now().strftime("%H:%M:%S"))
    return "Demo data · " + text if sample.is_demo() else text


def toolbar_widgets(chosen, archived):
    """Filter highlighting and the controls that differ in the Archived view."""
    tint = lambda active: {"contenttintcolour": "controlAccentColor" if active else "secondaryLabelColor"}
    buttons = ("all",) + config.ENABLED if len(config.ENABLED) > 1 else ()
    updates = {"filter_" + name: tint(name == chosen) for name in buttons}
    updates.update({
        "view_archived": tint(archived),
        "btnPrimary": {"text": "Restore" if archived else "Resume"},
        # Archived sessions cannot be copied or archived again; ⌘⌫ deletes them instead.
        "btnCopy": {"enabled": not archived},
        "btnCopyKey": {"text": "" if archived else "⇧⌘C"},
        "btnRemove": {"enabled": not archived},
        "btnRemoveKey": {"text": "" if archived else "⌘⌫"},
        "btnDelete": {"enabled": config.ALLOW_PURGE},
        "btnDeleteKey": {"text": ("⌘⌫" if archived else "⇧⌘⌫") if config.ALLOW_PURGE else ""},
        "search": {"placeholder": "Search archived sessions…  (↩ restores)" if archived
                   else "Search titles, folders, branches, models…  (↩ resumes)"},
    })
    return updates


def first_run(reply):
    """Once, when the window opens: set the refresh rate and drop switched-off tools."""
    demo = sample.is_demo()
    reply["pulse"] = {"interval": 0 if demo else config.REFRESH_SECONDS}  # keep sample data still
    if demo:
        reply["variables"]["view"] = os.environ.get("view") or "active"
    hidden = [t for t in config.TOOL_NAMES if t not in config.ENABLED]
    removed = [t + "Card" for t in hidden] if not config.ENABLED else []
    removed += ["col_filter_" + t for t in hidden]
    if len(config.ENABLED) < 2:
        removed += ["col_filter_" + t for t in ("all",) + config.ENABLED] + ["view_separator"]
    if removed:
        reply["remove"] = {"ids": removed}
    replaced = {}
    if len(config.ENABLED) == 1:
        replaced["cards"] = single_card(config.ENABLED[0], reply["update"]["ids"])
    if config.ROW_HEIGHT != component("list")["cell"]["height"]:
        replaced["list"] = sized_list(reply["update"]["ids"])
    if replaced:
        reply["replace"] = {"ids": replaced}


def load(force=False, first=False, removed=""):
    """Everything the window shows, as Alfred UI actions. removed is the row that just left the list."""
    opening = first and sample.is_demo()
    if opening and "archived" in sample.demo_words():
        os.environ["view"] = "archived"
    # Sample data always opens unfiltered, without changing the saved filter.
    chosen = "all" if opening else os.environ.get("filter") or "all"
    if chosen not in config.ENABLED or len(config.ENABLED) < 2:
        chosen = "all"
    archived = os.environ.get("view") == "archived"

    claude, codex, sessions, error = collect_data(archived)
    save_index(sessions, archived)
    listed = [s for s in sessions if chosen in ("all", s["tool"])]
    stamp = fingerprint(sessions, chosen, archived)

    updates = card_widgets(claude, codex)
    updates.update(toolbar_widgets(chosen, archived))
    status = status_text(len(listed), archived, error or codex.get("error"))
    updates["status"] = {"text": helpers.clean(status), "colour": "secondaryLabelColor"}
    reply = {"variables": {"stamp": stamp}, "update": {"ids": updates}}
    # Leave the list alone when nothing changed, so scrolling and reading are not interrupted.
    if force or stamp != os.environ.get("stamp"):
        context = [chosen, archived, config.LIMIT_STYLE, config.PREVIEW_MESSAGES, sample.is_demo()]
        # Rows and their previews are built only now: most refreshes send no list.
        listupdate.send_list(reply, [row_for(s) for s in listed], stamp, context, force, removed)
    if first:
        first_run(reply)
    return reply
