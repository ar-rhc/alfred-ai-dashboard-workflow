"""Small Alfred UI replies and popup windows shared by other modules."""
from . import helpers


def message(text, error=False):
    reply = {"update": {"ids": {"status": {"text": helpers.clean(text),
                                           "colour": "systemRedColor" if error else "secondaryLabelColor"}}}}
    if error:
        reply["sound"] = {"src": "Basso"}
    return reply


def label(text, **extra):
    return dict({"type": "label", "text": helpers.clean(text), "linebreak": "wordwrap"}, **extra)


def close_popup(name):
    return {"type": "json", "src": {"sheet": {"close": name}}}


def button(text, action, **extra):
    return dict({"type": "button", "text": text, "events": [{"triggers": "actioned", "action": action}]}, **extra)


def popup(name, components):
    """A sheet that slides over the window, with its parts stacked top to bottom."""
    return {"sheet": {"id": name, "root": {"type": "view", "layout": "inline", "direction": "vertical",
                                           "padding": 20, "spacing": 10, "components": components}}}
