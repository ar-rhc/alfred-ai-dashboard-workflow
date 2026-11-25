"""The conversation preview: a small static web page.

Message text comes from session logs and is untrusted. Every piece of it is escaped before any tag is
added, no links or images are made, and the page's security policy allows nothing but its own style.
"""
import html
import re
from datetime import datetime

from . import config, helpers

HEAD = """<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'">
<style>
:root{color-scheme:light dark;--you:#0a64c8;--claude:#a85300;--codex:#00708a}
body{margin:0;padding:2px 23px 12px 10px;font:13px/1.35 -apple-system,sans-serif;color:#1d1d1f;background:transparent;overflow-wrap:anywhere}
@media (prefers-color-scheme:dark){:root{--you:#4aa8ff;--claude:#ff9f0a;--codex:#40c8e0}body{color:#f2f2f2}}
.t,.note{padding-left:13px}.m{margin-bottom:18px}.own .t{border-left:3px solid #8888;padding-left:10px}
.h{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}.h span,.day,.note{font-weight:400;opacity:.55}
.h b{font-size:11px;font-weight:600;line-height:18px;padding:0 9px;border-radius:4px;color:var(--c);
background:color-mix(in srgb,var(--c) 20%,transparent)}.you{--c:var(--you)}.claude{--c:var(--claude)}.codex{--c:var(--codex)}
p,ul,ol,pre,table{margin:0 0 6px}.t>:last-child{margin-bottom:0}ul,ol{padding-left:20px}
code,pre{font-family:ui-monospace,Menlo,monospace;font-size:12px}pre{white-space:pre-wrap;padding-left:10px}
table{border-collapse:collapse;font:inherit;color:inherit}th,td{text-align:left;vertical-align:top;padding:1px 14px 1px 0}
.day{font-size:12px;margin-bottom:12px}
</style>
"""

ITEM = re.compile(r"^\s*(?:([-*+])|(\d{1,3})[.)])\s+(.*)$")
HEADING = re.compile(r"^\s*#{1,6}\s+(.*)$")
RULE_ROW = re.compile(r"^[\s|:-]+$")


def inline(text):
    """One line of text, escaped, with `code` and **bold**."""
    parts = text.split("`")
    if len(parts) % 2 == 0:  # an unpaired backtick is just a character
        parts[-2:] = ["`".join(parts[-2:])]
    return "".join("<code>%s</code>" % html.escape(part) if index % 2
                   else re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", html.escape(part))
                   for index, part in enumerate(parts))


def table(lines):
    rows = [line.strip().strip("|").split("|") for line in lines if not RULE_ROW.match(line)]
    return "<table>%s</table>" % "".join(
        "<tr>%s</tr>" % "".join("<%s>%s</%s>" % (tag, inline(cell.strip()), tag) for cell in row)
        for row, tag in zip(rows, ["th"] + ["td"] * len(rows)))


def markdown(text):
    """The common Markdown in messages: code blocks, lists, tables, headings and paragraphs."""
    out, block, kind, code = [], [], "", None

    def flush():
        if not block:
            return
        if kind == "table":
            out.append(table(block))
        elif kind in ("ul", "ol"):
            out.append("<%s>%s</%s>" % (kind, "".join("<li>%s</li>" % inline(x) for x in block), kind))
        else:
            out.append("<p>%s</p>" % "<br>".join(inline(x) for x in block))
        del block[:]

    for line in text.splitlines():
        if line.strip().startswith("```"):
            if code is None:
                flush()
                code = []
            else:
                out.append("<pre>%s</pre>" % html.escape("\n".join(code)))
                code = None
            continue
        if code is not None:
            code.append(line)
            continue
        item, heading = ITEM.match(line), HEADING.match(line)
        if heading:
            flush()
            out.append("<p><b>%s</b></p>" % inline(heading.group(1)))
            continue
        if not line.strip():
            flush()
            continue
        wanted = ("table" if line.lstrip().startswith("|") else
                  ("ul" if item.group(1) else "ol") if item else "p")
        if wanted != kind:
            flush()
            kind = wanted
        block.append(item.group(3) if item else line)
    flush()
    if code is not None:
        out.append("<pre>%s</pre>" % html.escape("\n".join(code)))
    return "".join(out)


def message(who, kind, text, stamp):
    """A pill naming the speaker, with the time on the right, above the text. The user's text gets a side bar."""
    time_of_day = datetime.fromtimestamp(stamp).strftime("%H:%M") if stamp else ""
    return ('<div class="m%s"><div class="h"><b class="%s">%s</b><span>%s</span></div><div class="t">%s</div></div>'
            % (" own" if kind == "you" else "", kind, html.escape(who), time_of_day, markdown(text)))


def note(text):
    return HEAD + '<p class="note">%s</p>' % html.escape(text)


def page(session):
    """A session's latest messages, newest first."""
    if not config.PREVIEW_MESSAGES:
        return note("Message previews are turned off in the workflow configuration.")
    if not session["messages"]:
        if session["path"].endswith(".zst") and not helpers.find_binary("zstd"):
            return note("Codex compressed this older session. Install zstd to preview it.")
        return note("No readable messages in this session log.")
    parts, shown_day = [], ""
    for role, text, stamp in reversed(session["messages"][-config.PREVIEW_MESSAGES:]):
        day = helpers.earlier_day(stamp) if stamp else ""
        if day and day != shown_day:  # once for each day before today
            parts.append('<div class="day">%s</div>' % day)
            shown_day = day
        who, kind = ("You", "you") if role == "user" else (config.TOOL_NAMES[session["tool"]], session["tool"])
        parts.append(message(who, kind, text, stamp))
    return HEAD + "".join(parts)
