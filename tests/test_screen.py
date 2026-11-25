"""What the window shows: layout, list updates and preview."""
import json
import os
import plistlib
import re
import shlex
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from support import SESSION, claude_user, claude_reply, Base  # noqa: E402  (sets the import path)
import dashboard  # noqa: E402
from aidash import (  # noqa: E402
    archiving, config, helpers, logparse, quota, resume, rowactions,
    screen, preview)


class DemoTests(Base):
    def test_sample_data_reads_nothing_real_and_opens_unfiltered(self):
        self.claude_log([claude_user("REAL PRIVATE TEXT", self.cwd)])
        os.environ.update(input="demo", filter="claude")
        with mock.patch.object(logparse, "Parser", side_effect=AssertionError), \
                mock.patch.object(quota, "claude_limits", side_effect=AssertionError):
            reply = screen.load(force=True, first=True)
        text = json.dumps(reply)
        self.assertNotIn("REAL PRIVATE TEXT", text)
        self.assertNotIn(str(self.root), text)
        self.assertEqual(len(reply["update"]["ids"]["list"]["data"]["src"]), 9)
        self.assertEqual(reply["pulse"], {"interval": 0})
        self.assertNotIn("filter", reply["variables"])

    def test_sample_archive_opens_directly(self):
        os.environ["input"] = "demo archived"
        reply = screen.load(force=True, first=True)
        self.assertEqual(reply["variables"]["view"], "archived")
        self.assertEqual(len(reply["update"]["ids"]["list"]["data"]["src"]), 3)


class AppearanceTests(Base):
    def test_matches_the_theme_background(self):
        for background, look in (("rgba(0,0,0,0.20)", "dark"), ("rgba(40,44,52,1.00)", "dark"),
                                 ("rgba(255,255,255,0.98)", "light"), ("rgba(230,236,250,1)", "light"),
                                 ("", "automatic"), (None, "automatic"), ("rgba(.,1,2,3)", "automatic")):
            self.assertEqual(dashboard.window_appearance(background), look, background)

    def test_script_keeps_the_argument(self):
        script = str(Path(dashboard.__file__).resolve())
        env = dict(os.environ, alfred_theme_background="rgba(0,0,0,0.2)")
        out = subprocess.run([sys.executable, "-B", script, "appearance", 'demo "quoted"'], env=env, check=True,
                             capture_output=True, text=True).stdout
        self.assertEqual(json.loads(out)["alfredworkflow"],
                         {"arg": 'demo "quoted"', "variables": {"window_appearance": "dark"}})

    def test_only_the_separate_window_goes_through_it(self):
        import plistlib
        with open(Path(dashboard.__file__).resolve().parents[2] / "info.plist", "rb") as handle:
            plist = plistlib.load(handle)
        by_uid = {o["uid"]: o for o in plist["objects"]}
        branch = [o for o in plist["objects"] if o["type"].endswith("conditional")][0]
        targets = {bool(c.get("sourceoutputuid")): by_uid[c["destinationuid"]]
                   for c in plist["connections"][branch["uid"]]}
        self.assertIn("scripts/run appearance", targets[True]["config"]["script"])
        window = by_uid[plist["connections"][targets[True]["uid"]][0]["destinationuid"]]
        self.assertEqual((window["config"]["launchmode"], targets[False]["config"]["launchmode"]), (1, 0))


class LayoutTests(Base):
    @staticmethod
    def nodes(value):
        if isinstance(value, dict):
            yield value
        for child in (value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()):
            yield from LayoutTests.nodes(child)

    def test_templates_expand_completely_with_unique_ids(self):
        ui = screen.layout()
        text = json.dumps(ui)
        self.assertNotIn("{param:", text)
        self.assertNotIn('"type": "template"', text)
        ids = [n["id"] for n in self.nodes(ui["window"]) if "id" in n]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_template_use_supplies_exactly_its_parameters(self):
        path = Path(dashboard.__file__).resolve().parents[1] / "ui.json"
        ui = json.loads(path.read_text(encoding="utf-8"))
        uses = [n for n in self.nodes(ui["window"]) if n.get("type") == "template"]
        self.assertEqual({n["src"] for n in uses}, set(ui["templates"]))
        for use in uses:
            wanted = set(re.findall(r"\{param:(\w+)\}", json.dumps(ui["templates"][use["src"]])))
            self.assertEqual(set(use["params"]), wanted, use)

    def test_another_row_height_replaces_the_list_with_its_rows(self):
        self.claude_log([claude_user("hello", self.cwd)])
        self.assertNotIn("list", screen.load(force=True, first=True).get("replace", {}).get("ids", {}))
        config.configure({"row_height": "54"})
        reply = screen.load(force=True, first=True)
        rows = reply["replace"]["ids"]["list"]
        self.assertEqual((rows["cell"]["height"], len(rows["data"]["src"]), rows["select"]),
                         (54, 1, "claude:" + SESSION))
        self.assertNotIn("list", reply["update"]["ids"])

    def test_script_only_updates_components_that_exist(self):
        ids = {n["id"] for n in self.nodes(screen.layout()["window"]) if "id" in n}
        self.claude_log([claude_user("hello", self.cwd)])
        for view in ("active", "archived"):
            os.environ["view"] = view
            self.assertLessEqual(set(screen.load(force=True, first=True)["update"]["ids"]), ids)


class ShortcutTests(Base):
    def test_action_keys_act_on_the_selected_session(self):
        keys = {s["shortcut"]: s["action"] for s in screen.layout()["window"]["root"]["shortcuts"]}
        for key, name in (("⌘↩", "primary"), ("⌘O", "folder"), ("⇧⌘C", "copycmd"), ("⇧⌘R", "reveal"),
                          ("⌘⌫", "remove"), ("⇧⌘⌫", "purge")):
            self.assertEqual(keys[key]["args"], ["action", name, "{value:list.selected.key}"])

    def test_no_key_is_used_twice_in_the_window(self):
        keys = [n["shortcut"] for n in LayoutTests.nodes(screen.layout()["window"]) if "shortcut" in n]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertLessEqual({"⌘⌫", "⇧⌘C", "⌘1", "⌘R"}, set(keys))
        self.assertNotIn("⌘C", keys)  # left to copy selected text

    def test_buttons_and_captions_follow_the_view_and_the_delete_setting(self):
        active, archived = screen.toolbar_widgets("all", False), screen.toolbar_widgets("all", True)
        self.assertEqual((active["btnRemove"]["enabled"], active["btnDeleteKey"]["text"]), (True, "⇧⌘⌫"))
        self.assertEqual((archived["btnRemove"]["enabled"], archived["btnDeleteKey"]["text"]), (False, "⌘⌫"))
        config.configure({"allow_purge": "0"})
        off = screen.toolbar_widgets("all", False)
        self.assertEqual((off["btnDelete"]["enabled"], off["btnDeleteKey"]["text"]), (False, ""))

    def test_removal_never_blocks_the_window(self):
        scripts = [n for n in LayoutTests.nodes(screen.layout()["window"]) if n.get("type") == "script"]
        removing = [n for n in scripts if n["args"][:1] == ["action"] and n["args"][1] in ("remove", "delete", "purge")]
        self.assertTrue(removing and all(n.get("async") for n in removing))
        sheet = archiving.confirm_sheet({"tool": "claude", "title": "t", "cwd": self.cwd}, "u", True)
        self.assertTrue(all(n.get("async") for n in LayoutTests.nodes(sheet) if n.get("type") == "script"))

    def test_return_confirms_and_escape_cancels(self):
        for permanent in (False, True):
            sheet = archiving.confirm_sheet({"tool": "claude", "title": "t", "cwd": self.cwd}, "u", permanent)
            buttons = [n for n in LayoutTests.nodes(sheet) if n.get("type") == "button"]
            self.assertEqual([b["shortcut"] for b in buttons], ["⎋", "↩"])
            self.assertEqual(buttons[0]["text"], "Cancel")


class DrawnList(Base):
    """Three sessions, already drawn in the window."""

    def setUp(self):
        super().setUp()
        project = re.sub(r"[^A-Za-z0-9]", "-", self.cwd)
        self.logs = []
        for number, age in ((1, 100), (2, 5000), (3, 9000)):
            log = self.root / "claude/projects" / project / ("00000000-0000-4000-8000-%012d.jsonl" % number)
            self.write(log, [claude_user("question %d" % number, self.cwd)])
            os.utime(str(log), (time.time() - age,) * 2)
            self.logs.append(log)
        self.uids = ["claude:" + log.stem for log in self.logs]
        self.refresh(force=True)

    def refresh(self, force=False, selected=None):
        if selected is not None:
            os.environ["selected"] = selected
        reply = screen.load(force=force)
        os.environ["stamp"] = reply["variables"]["stamp"]
        return reply

    def append(self, index, text, age):
        self.write(self.logs[index], [claude_reply("m%f" % time.time(), text, 3, self.cwd)], mode="a")
        os.utime(str(self.logs[index]), (time.time() - age,) * 2)


class ChangedRowTests(DrawnList):
    """After the first draw, only rows that changed are sent."""

    def test_nothing_changed_sends_no_list(self):
        ids = self.refresh()["update"]["ids"]
        self.assertNotIn("list", ids)

    def test_only_the_changed_row_is_sent(self):
        self.append(0, "newest answer", 50)
        reply = self.refresh(selected=self.uids[1])
        replace = reply["update"]["ids"]["list"]["data"]["modify"]["replace"]
        self.assertEqual(list(replace["srcmap"]), [self.uids[0]])
        self.assertFalse(replace["autoselect"])
        self.assertNotIn("select", reply["update"]["ids"]["list"])
        self.assertFalse([k for k in reply["update"]["ids"] if k.startswith("detail")])
        self.assertEqual(reply["functions"], {"ids": {"list": "filterdata"}})

    def test_selected_row_updates_only_the_parts_that_changed(self):
        self.append(0, "newest answer", 50)
        ids = self.refresh(selected=self.uids[0])["update"]["ids"]
        self.assertIn("newest answer", ids["detailPreview"]["text"])
        self.assertNotIn("detailTitle", ids)
        self.assertNotIn("detailPlace", ids)
        os.utime(str(self.logs[0]), (time.time() - 40,) * 2)  # touched, same messages
        ids = self.refresh()["update"]["ids"]
        self.assertNotIn("detailPreview", ids)

    def test_a_second_change_builds_on_the_first(self):
        self.append(0, "first", 60)
        self.refresh()
        self.append(1, "second", 500)
        srcmap = self.refresh()["update"]["ids"]["list"]["data"]["modify"]["replace"]["srcmap"]
        self.assertEqual(list(srcmap), [self.uids[1]])

    def test_new_order_new_filter_old_stamp_or_force_send_everything(self):
        def full(reply):
            data = reply["update"]["ids"]["list"]["data"]
            return "src" in data and "modify" not in data

        self.append(2, "now newest", 10)
        self.assertTrue(full(self.refresh()), "order changed")
        self.append(2, "again", 9)
        os.environ["stamp"] = "stale"
        self.assertTrue(full(self.refresh()), "window shows something else")
        os.environ["filter"] = "claude"
        self.assertTrue(full(self.refresh()), "filter changed")
        self.assertTrue(full(self.refresh(force=True)), "forced")
        self.append(2, "one more", 8)
        config.configure({"limit_style": "used"})
        self.assertTrue(full(self.refresh()), "setting changed")
        (self.root / "cache/shown.json").write_text("damaged")
        self.append(2, "and again", 7)
        self.assertTrue(full(self.refresh()), "record unreadable")


class RemovedRowTests(DrawnList):
    """After a removal, only that row leaves the drawn list."""

    def remove(self, index):
        os.environ["selected"] = self.uids[index]
        reply = rowactions.action("delete-confirmed", self.uids[index])
        os.environ["stamp"] = reply["variables"]["stamp"]
        return reply["update"]["ids"]

    def test_only_that_row_is_deleted_and_the_next_selected(self):
        ids = self.remove(1)
        self.assertEqual(ids["list"], {"data": {"modify": {"delete": {"uids": [self.uids[1]], "autoselect": False}}},
                                       "select": self.uids[2]})
        self.assertIn("question 3", ids["detailPreview"]["text"])
        self.append(0, "later answer", 50)  # the next refresh builds on the shortened list
        replace = self.refresh()["update"]["ids"]["list"]["data"]["modify"]["replace"]
        self.assertEqual(list(replace["srcmap"]), [self.uids[0]])

    def test_with_a_tool_filter_the_next_row_of_that_tool_is_selected(self):
        record = self.root / "cache/index.json"
        cache = json.loads(record.read_text())
        entries = list(cache["sessions"].items())
        entries.insert(2, ("codex:" + SESSION, dict(entries[1][1], tool="codex")))  # between rows 2 and 3
        helpers.write_private(record, json.dumps(dict(cache, sessions=dict(entries))))
        os.environ["filter"] = "claude"
        self.refresh(force=True)
        helpers.write_private(record, json.dumps(dict(cache, sessions=dict(entries))))
        self.assertEqual(self.remove(1)["list"]["select"], self.uids[2])

    def test_the_session_moving_into_the_limit_is_inserted(self):
        config.LIST_LIMIT = 2
        self.refresh(force=True)
        modify = self.remove(1)["list"]["data"]["modify"]
        insert = modify["insert"]
        self.assertEqual((insert["position"], insert["uid"], insert["autoselect"]), ("after", self.uids[0], False))
        self.assertEqual([r["uid"] for r in insert["src"]], [self.uids[2]])

    def test_other_changed_rows_are_redrawn_with_it(self):
        self.append(0, "new answer", 50)
        modify = self.remove(1)["list"]["data"]["modify"]
        self.assertEqual(list(modify["replace"]["srcmap"]), [self.uids[0]])

    def test_a_window_showing_something_else_gets_the_whole_list(self):
        os.environ["stamp"] = "stale"
        self.assertIn("src", self.remove(1)["list"]["data"])

    def test_a_new_order_gets_the_whole_list(self):
        self.append(2, "now newest", 10)
        self.assertIn("src", self.remove(1)["list"]["data"])


class PreviewTests(Base):
    def page(self, *messages):
        return preview.page({"tool": "claude", "path": "", "messages": list(messages)})

    def body(self, *messages):
        return self.page(*messages)[len(preview.HEAD):]

    def test_each_message_has_a_pill_and_time_above_its_text(self):
        now = time.time()
        clock = datetime.fromtimestamp(now).strftime("%H:%M")
        body = self.body(("assistant", "done", now), ("user", "please", now))
        message = '<div class="m%s"><div class="h"><b class="%s">%s</b><span>%s</span></div><div class="t"><p>%s</p></div></div>'
        self.assertEqual(body, message % (" own", "you", "You", clock, "please") + message % ("", "claude", "Claude Code", clock, "done"))

    def test_log_text_cannot_add_tags_links_or_scripts(self):
        nasty = ('<script>alert(1)</script> **<img src=x onerror=a>** `</code><iframe>`\n```\n</pre><object>\n```\n'
                 '| <a href=//x>a</a> |\n- <form>\n# <meta http-equiv=refresh>\n[link](http://example.com) </style>')
        for role in ("user", "assistant"):
            body = self.body((role, nasty, 0))
            tags = set(re.findall(r"<\s*/?\s*([a-zA-Z0-9]+)", body))
            self.assertLessEqual(tags, {"div", "span", "p", "br", "b", "code", "pre", "ul", "ol", "li", "table", "tr", "th", "td"})
            self.assertNotRegex(body, r"<[^>]*(href|src|on\w+)=")
        self.assertIn("default-src 'none'", self.page(("user", "x", 0)))

    def test_common_markdown(self):
        text = "## Title\ntext `co*de` **bold**\n\n- a\n- b\n1. c\n| h | i |\n|---|---|\n| 1 | 2 |\n```\nx = 1\n\ny\n```\nodd ` tick"
        self.assertEqual(preview.markdown(text),
                         "<p><b>Title</b></p><p>text <code>co*de</code> <b>bold</b></p><ul><li>a</li><li>b</li></ul><ol><li>c</li></ol>"
                         "<table><tr><th>h</th><th>i</th></tr><tr><td>1</td><td>2</td></tr></table><pre>x = 1\n\ny</pre><p>odd ` tick</p>")

    def test_each_earlier_day_is_named_once_and_today_is_not(self):
        noon = datetime.now().replace(hour=12, minute=0).timestamp()
        body = self.body(("user", "a", noon - 3 * 86400), ("assistant", "b", noon - 3 * 86400),
                         ("user", "c", noon - 86400), ("assistant", "d", noon))
        self.assertEqual(re.findall(r'<div class="day">(.*?)</div>', body),
                         [helpers.earlier_day(noon - n * 86400) for n in (1, 3)])
        self.assertTrue(body.startswith('<div class="m">'))


class UITests(Base):
    def test_log_text_cannot_expand_placeholders(self):
        self.claude_log([claude_user("show {var:secret} {file:/etc/hosts} {const:x} {keep}", self.cwd)])
        reply = json.dumps(screen.load(force=True), ensure_ascii=False)
        for name in ("{var:", "{file:", "{const:"):
            self.assertNotIn(name, reply)
        self.assertIn("{keep}", reply)

    def test_unchanged_refresh_leaves_list_alone_and_filter_applies(self):
        self.claude_log([claude_user("hello", self.cwd)])
        first = screen.load(force=True)
        self.assertEqual(len(first["update"]["ids"]["list"]["data"]["src"]), 1)
        os.environ["stamp"] = first["variables"]["stamp"]
        self.assertNotIn("list", screen.load()["update"]["ids"])
        os.environ["filter"] = "codex"
        self.assertEqual(screen.load()["update"]["ids"]["list"]["data"]["src"], [])

    def test_empty_machine(self):
        reply = screen.load(force=True)
        self.assertEqual(reply["update"]["ids"]["claudeUsage"]["text"], "Not found on this Mac")
        self.assertEqual(reply["update"]["ids"]["detailTitle"]["text"], "No session selected")

    def test_demo_never_opens_anything(self):
        os.environ["input"] = "demo"
        screen.load(force=True)
        uid = next(iter(json.loads((self.root / "cache/index.json").read_text())["sessions"]))
        with mock.patch.object(helpers.subprocess, "run") as run:
            for name in ("resume", "folder", "reveal"):
                rowactions.action(name, uid)
        run.assert_not_called()

    def test_resume_launches_quoted_script(self):
        nasty = str(self.root / "it's $(touch pwned) `x`")
        os.mkdir(nasty)
        for tool, expected in (("claude", ["claude", "--resume", SESSION]), ("codex", ["codex", "resume", SESSION])):
            session = {"tool": tool, "id": SESSION, "cwd": nasty, "path": ""}
            helpers.write_private(self.root / "cache/index.json", json.dumps({"sessions": {"u": session}}))
            config.configure({"tmux_mode": "never"})
            with mock.patch.object(helpers.subprocess, "run") as run:
                self.assertEqual(rowactions.action("resume", "u"), {"finish": {}})
            script = Path(run.call_args[0][0][1])
            lines = script.read_text().splitlines()
            self.assertEqual(shlex.split(lines[-3])[:3], ["cd", "--", nasty])
            self.assertEqual(shlex.split(lines[-2]), expected)
            self.assertTrue(lines[-4].startswith("export "))  # these tests use non-default folders
            self.assertEqual(script.stat().st_mode & 0o777, 0o700)
            subprocess.run([helpers.user_shell(), "-n", str(script)], check=True)

    def test_bad_requests(self):
        self.assertIn("sound", rowactions.action("resume", ""))
        with self.assertRaises(ValueError):
            resume.resume_command({"tool": "claude", "id": "x; rm -rf /"})
        session = {"tool": "claude", "id": SESSION, "cwd": "/no/such/folder", "path": ""}
        helpers.write_private(self.root / "cache/index.json", json.dumps({"sessions": {"u": session}}))
        with mock.patch.object(helpers.subprocess, "run") as run:
            rowactions.action("resume", "u")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
