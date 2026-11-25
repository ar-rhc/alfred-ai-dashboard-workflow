#!/usr/bin/env python3
"""Build preview/dashboard-preview.json for the "Try Alfred UI" workflow (tryaui).

It is generated from the real ui.json plus the built-in sample data, so edit those
and rerun this. Try Alfred UI cannot run this workflow's scripts, so script actions
are replaced with JSON actions: filtering works, row actions only report themselves.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent / "ai-dashboard/ai-dashboard.alfredui"
sys.path.insert(0, str(PACKAGE / "scripts"))
from aidash import config, screen  # noqa: E402


def build():
    ui = screen.layout()  # Try Alfred UI is given the window only, so templates are expanded here
    with tempfile.TemporaryDirectory() as cache:
        config.CACHE_DIR = Path(cache)
        replies = {}
        for name in ("all", "claude", "codex", "archived"):
            os.environ.update(input="demo", filter="all" if name == "archived" else name, selected="", stamp="",
                              view="archived" if name == "archived" else "active")
            replies[name] = screen.load(force=True)["update"]["ids"]

    def filtered(name):
        ids = {k: v for k, v in replies[name].items() if k.startswith(("filter_", "detail", "status", "view_", "btn", "search"))}
        ids["list"] = {"data": {"src": replies[name]["list"]["data"]["src"]}}
        return {"type": "json", "src": {"update": {"ids": ids}}}

    def stand_in(action):
        args = action["args"]
        if args[0] == "filter":
            return filtered(args[1])
        if args[0] == "view":
            return filtered("archived")
        if args[0] == "load":
            return filtered("all")
        text = "Preview only: “%s” works in the installed workflow" % args[1]
        return {"type": "json", "src": {"update": {"ids": {"status": {"text": text}}}}}

    def convert(node):
        if isinstance(node, list):
            items = [convert(item) for item in node]
            return [item for item in items if item is not None]
        if not isinstance(node, dict):
            return node
        if node.get("type") == "script":
            return stand_in(node)
        if set(node) == {"variables"}:
            return None  # only fed the scripts
        result = {key: convert(value) for key, value in node.items()}
        update = replies["all"].get(node.get("id"))
        if update:
            for key, value in update.items():
                if key == "data":
                    result["data"] = dict(result["data"], **value)
                else:
                    result[key] = value
        return result

    root = ui["window"]["root"]
    root.pop("events")  # the initial load; its result is already inlined
    root = convert(root)
    root["constraints"] = {"position": "fit"}
    root["padding"] = 10
    return {"window": {"root": root}}


if __name__ == "__main__":
    target = HERE / "dashboard-preview.json"
    # Sample folders sit under the real home folder; do not put its path in a shareable file.
    text = json.dumps(build(), indent=2, ensure_ascii=False).replace(str(Path.home()), "~")
    target.write_text(text + "\n", encoding="utf-8")
    print(target)
