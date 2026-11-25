"""What the window's session list and preview are sent, and what is selected there."""
import hashlib
import json
import os

from . import config, helpers

DETAIL_FIELDS = {"title": "detailTitle", "place": "detailPlace", "info": "detailInfo", "preview": "detailPreview"}


def next_selection(uids, removed):
    """What to select when a row leaves: the next one the tool filter shows, else the one before it."""
    tool = os.environ.get("filter")
    order = [u for u in uids if u == removed or tool not in config.TOOL_NAMES or u.startswith(tool + ":")]
    place = order.index(removed)
    return (order[place + 1:] + order[:place][::-1] + [""])[0]


def digest(value):
    return hashlib.sha1(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def selected_row(rows):
    """The selected session's row if it is still listed, else the first."""
    selected = os.environ.get("selected", "")
    return next((r for r in rows if r["uid"] == selected), rows[0] if rows else None)


def detail_widgets(row):
    row = row or {"title": "No session selected", "place": "", "info": "", "preview": ""}
    return {
        "detailTitle": {"text": row["title"]}, "detailPlace": {"text": row["place"]},
        "detailInfo": {"text": row["info"]}, "detailPreview": {"text": row["preview"]},
    }


def full_list(reply, rows):
    """Replace the whole list, keeping the selected session if it is still there."""
    current = selected_row(rows)
    updates = reply["update"]["ids"]
    updates["list"] = {"data": {"src": rows}, "select": current["uid"] if current else ""}
    updates.update(detail_widgets(current))
    reply["functions"] = {"ids": {"list": "filterdata"}}


def changed_rows(reply, rows, digests, before):
    """Redraw only the rows that differ, leaving selection, scrolling and an unchanged preview alone."""
    changed = {r["uid"]: r for r in rows if digests[r["uid"]] != before[r["uid"]]}
    if not changed:
        return
    updates = reply["update"]["ids"]
    updates["list"] = {"data": {"modify": {"replace": {"srcmap": changed, "autoselect": False}}}}
    reply["functions"] = {"ids": {"list": "filterdata"}}
    selected = os.environ.get("selected", "")
    if selected in changed:
        for field, widget in DETAIL_FIELDS.items():
            if digests[selected][field] != before[selected][field]:
                updates[widget] = {"text": changed[selected][field]}


def remove_row(reply, rows, digests, shown, removed):
    """Take one row out of the drawn list, and add the one that moved up into the list limit.

    False, with nothing sent, if the list differs from the drawn one in any further way.
    """
    order = [r["uid"] for r in rows]
    drawn = shown.get("order", [])
    kept = [uid for uid in drawn if uid != removed]
    added = [uid for uid in order if uid not in kept]
    if removed not in drawn or removed in order or len(added) > 1 or [u for u in order if u not in added] != kept:
        return False
    modify = {"delete": {"uids": [removed], "autoselect": False}}
    if added:
        place = order.index(added[0])
        where = {"position": "after", "uid": order[place - 1]} if place else {"position": "before"}
        modify["insert"] = dict(where, src=[rows[place]], autoselect=False)
    changed = {r["uid"]: r for r in rows if r["uid"] in kept and digests[r["uid"]] != shown["rows"][r["uid"]]}
    if changed:
        modify["replace"] = {"srcmap": changed, "autoselect": False}
    current = selected_row(rows)
    updates = reply["update"]["ids"]
    updates["list"] = {"data": {"modify": modify}, "select": current["uid"] if current else ""}
    updates.update(detail_widgets(current))
    reply["functions"] = {"ids": {"list": "filterdata"}}
    return True


def send_list(reply, rows, stamp, context, force, removed):
    """Send the whole list, or only what differs when the window is known to show the list last sent.

    What was last sent is remembered with its stamp; a window showing any other stamp gets everything.
    """
    record = config.CACHE_DIR / "shown.json"
    shown = helpers.read_json(record, {})
    order = [r["uid"] for r in rows]
    digests = {r["uid"]: dict({field: digest(r[field]) for field in DETAIL_FIELDS}, row=digest(r)) for r in rows}
    known = shown.get("stamp") == os.environ.get("stamp") and shown.get("context") == context
    if known and not force and shown.get("order") == order:
        changed_rows(reply, rows, digests, shown["rows"])
    elif not (known and removed and remove_row(reply, rows, digests, shown, removed)):
        full_list(reply, rows)
    helpers.write_private(record, json.dumps({"stamp": stamp, "context": context, "order": order, "rows": digests}))
