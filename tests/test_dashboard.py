"""Offline tests with synthetic logs. Run: /usr/bin/python3 -m unittest discover -s tests"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-dashboard/ai-dashboard.alfredui/scripts"))
import dashboard  # noqa: E402
from aidash import (archiving, collect, config, helpers, logparse, quota, resume, rowactions,  # noqa: E402
                    sample, screen)

SESSION = "11111111-2222-4333-8444-555555555555"


def stamp(offset=0):
    return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat()


def claude_user(text, cwd, **extra):
    return dict({"type": "user", "timestamp": stamp(), "cwd": cwd, "message": {"content": text}}, **extra)


def claude_reply(key, text, output, cwd):
    return {"type": "assistant", "timestamp": stamp(), "cwd": cwd, "message": {
        "id": key, "model": "m", "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "output_tokens": output,
                  "cache_read_input_tokens": 1000}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.root))
        settings = ("LIST_LIMIT", "PREVIEW_MESSAGES", "ENABLED", "LIMIT_STYLE", "REFRESH_SECONDS",
                    "ALLOW_PURGE", "TERMINAL_APP", "EXTRA_ARGS", "TMUX_MODE", "TERMINAL_COMMAND")
        patches = [mock.patch.object(config, name, getattr(config, name)) for name in settings]
        patches += [mock.patch.object(config, "CLAUDE_HOME", self.root / "claude"),
                   mock.patch.object(config, "CODEX_HOME", self.root / "codex"),
                   mock.patch.object(config, "CACHE_DIR", self.root / "cache"),
                   mock.patch.dict(os.environ, {"input": "", "filter": "all", "selected": "", "stamp": "", "view": "active"})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.cwd = str(self.root / "my project")
        os.mkdir(self.cwd)

    def write(self, path, rows, mode="w"):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode) as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def claude_log(self, rows, **kw):
        project = re.sub(r"[^A-Za-z0-9]", "-", self.cwd)
        path = self.root / "claude/projects" / project / (SESSION + ".jsonl")
        self.write(path, rows, **kw)
        return path


class ClaudeTests(Base):
    def test_streamed_lines_count_once_and_meta_is_hidden(self):
        self.claude_log([
            claude_user("<system-reminder>hidden</system-reminder>", self.cwd),
            claude_user("injected", self.cwd, isMeta=True),
            claude_user("Real question", self.cwd),
            claude_reply("a", "Part one", 1, self.cwd),
            claude_reply("a", "Part two", 20, self.cwd),
            {"type": "ai-title", "aiTitle": "A title"},
        ])
        data = collect.collect_claude(logparse.Parser())
        session = data["sessions"][0]
        self.assertEqual(session["tokens"], 35)  # 10 + 5 + 20; cache reads excluded
        self.assertEqual(data["days"][-1], 35)
        self.assertEqual(session["title"], "A title")
        self.assertEqual(session["cwd"], self.cwd)
        self.assertEqual([m[:2] for m in session["messages"]],
                         [["user", "Real question"], ["assistant", "Part one\n\nPart two"]])

    def test_appended_and_partial_lines(self):
        path = self.claude_log([claude_user("one", self.cwd)])
        parser = logparse.Parser()
        collect.collect_claude(parser)
        parser.save()
        line = json.dumps(claude_reply("b", "two", 2, self.cwd))
        with open(path, "a") as handle:
            handle.write(line[:20])
        parser = logparse.Parser()
        self.assertEqual(collect.collect_claude(parser)["sessions"][0]["tokens"], 0)
        parser.save()
        with open(path, "a") as handle:
            handle.write(line[20:] + "\n")
        session = collect.collect_claude(logparse.Parser())["sessions"][0]
        self.assertEqual(session["tokens"], 17)
        self.assertEqual(len(session["messages"]), 2)


class CodexTests(Base):
    def rows(self):
        def count(total):
            return {"type": "event_msg", "timestamp": stamp(), "payload": {"type": "token_count", "info": {
                "total_token_usage": {"total_tokens": total},
                "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 5,
                                     "total_tokens": 105}},
                "rate_limits": {"limit_id": "codex", "primary": {
                    "used_percent": 90, "window_minutes": 300, "resets_at": time.time() + 60}}}}

        def say(role, kind, text):
            return {"type": "response_item", "timestamp": stamp(),
                    "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}}

        return [say("developer", "input_text", "secret setup"), say("user", "input_text", "# AGENTS.md x"),
                say("user", "input_text", "Question"), count(105), count(105), say("assistant", "output_text", "Answer"),
                count(210)]

    def check(self):
        data = collect.collect_codex(logparse.Parser())
        self.assertEqual(data["days"][-1], 90)  # two distinct counts of 40 + 5
        self.assertEqual(data["limits"][1]["primary"]["used_percent"], 90)
        return data

    def test_usage_limits_and_missing_database(self):
        self.write(self.root / "codex/sessions/2026/r.jsonl", self.rows())
        data = self.check()
        self.assertEqual(data["sessions"], [])

    @unittest.skipUnless(helpers.find_binary("zstd"), "zstd not installed")
    def test_compressed_log(self):
        path = self.root / "codex/sessions/2026/r.jsonl"
        self.write(path, self.rows())
        subprocess.run([helpers.find_binary("zstd"), "-q", "--rm", str(path)], check=True)
        self.check()
        state = logparse.Parser().parse(str(path) + ".zst", "codex")
        self.assertEqual([m[1] for m in state["messages"]], ["Question", "Answer"])

    def test_expired_limit_is_not_shown_as_used(self):
        widgets = quota.limit_widgets([time.time() - 900, {"primary": {
            "used_percent": 99, "window_minutes": 300, "resets_at": time.time() - 60}, "secondary": None}])
        self.assertIn("reset", widgets["limit0Label"]["text"])
        self.assertTrue(widgets["limit0Bar"]["hidden"])
        self.assertTrue(widgets["limit1Bar"]["hidden"])


class ClaudeLimitTests(Base):
    def test_saved_reading_fills_both_bars(self):
        helpers.write_private(self.root / "claude" / config.LIMITS_FILE, json.dumps({"windows": {
            "five_hour": {"used_percentage": 12.4, "resets_at": time.time() + 60, "seen_at": time.time()},
            "seven_day": {"used_percentage": 85, "resets_at": time.time() - 60, "seen_at": time.time()}}}))
        ids = screen.load(force=True)["update"]["ids"]
        self.assertTrue(ids["claudeLimit0Label"]["text"].startswith("5h 88% left · resets"))
        self.assertTrue(ids["claudeLimit1Bar"]["hidden"])

    def test_both_tools_show_what_is_left(self):
        later = time.time() + 600
        codex = quota.limit_widgets([time.time(), {"primary": {
            "used_percent": 95, "window_minutes": 300, "resets_at": later}}])
        claude = quota.claude_limit_widgets({"windows": {"five_hour": {
            "used_percentage": 95, "resets_at": later, "seen_at": time.time()}}})
        self.assertEqual(codex["limit0Label"], claude["claudeLimit0Label"])
        self.assertTrue(codex["limit0Label"]["text"].startswith("5h 5% left"))
        self.assertEqual(codex["limit0Bar"], {"hidden": False, "progress": 0.05, "colour": "systemRedColor"})

    def test_missing_or_damaged_reading(self):
        for saved in ({}, {"windows": {"five_hour": None, "seven_day": {"used_percentage": "x"}}}, []):
            widgets = quota.claude_limit_widgets(saved)
            self.assertTrue(widgets["claudeLimit0Bar"]["hidden"])
            self.assertIn("set up", widgets["claudeLimitNote"]["text"])


class LimitsSetupTests(Base):
    def saved(self, content):
        helpers.write_private(self.root / "claude" / config.LIMITS_FILE, json.dumps(content))
        return quota.claude_limit_widgets(quota.claude_limits())

    def test_accepts_what_a_status_line_is_likely_to_save(self):
        later = time.time() + 600
        limits = {"five_hour": {"used_percentage": 11, "resets_at": later},
                  "seven_day": {"used_percentage": 7, "resets_at": later}}
        for content in (limits, {"rate_limits": limits, "model": {}},
                        {"windows": {k: dict(v, seen_at=time.time()) for k, v in limits.items()}}):
            widgets = self.saved(content)
            self.assertTrue(widgets["claudeLimit0Label"]["text"].startswith("5h 89% left"), content)
            self.assertTrue(widgets["claudeLimit1Label"]["text"].startswith("7d 93% left"))
            self.assertTrue(widgets["claudeLimitNote"]["text"].startswith("As of"))

    def test_empty_or_unexpected_content_offers_setup(self):
        for content in ({}, [], None, {"rate_limits": None}, {"five_hour": "x"}):
            self.assertIn("set up", self.saved(content)["claudeLimitNote"]["text"], content)

    def test_setup_shows_the_command_and_writes_nothing(self):
        os.mkdir(self.root / "claude")
        screen.load(force=True)
        sheet = json.dumps(rowactions.action("limits-setup", ""))
        self.assertIn("/statusline keep my status line as it is", sheet)
        self.assertIn(str(self.root / "claude" / config.LIMITS_FILE), sheet)
        self.assertNotIn('"copy"', sheet)
        self.assertEqual(os.listdir(str(self.root / "claude")), [])
        self.saved({"five_hour": {"used_percentage": 1}})
        self.assertEqual(rowactions.action("limits-setup", ""), {})

    def test_readme_command_matches_the_window(self):
        project = Path(dashboard.__file__).resolve().parents[3]
        config.CLAUDE_HOME = config.HOME / ".claude"
        with mock.patch.object(quota, "status_line_limits", return_value={}):
            sheet = quota.limits_help()["sheet"]["root"]["components"]
        self.assertIn(sheet[2]["text"], (project / "README.md").read_text())


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


class SafetyTests(Base):
    def stored(self, cwd, path=""):
        session = {"tool": "claude", "id": SESSION, "cwd": cwd, "path": path, "title": "t"}
        helpers.write_private(self.root / "cache/index.json", json.dumps({"sessions": {"u": session}}))

    def test_bundles_are_revealed_not_opened(self):
        app = self.root / "thing.app"
        (app / "Contents").mkdir(parents=True)
        plain = self.root / "unnamed bundle"
        (plain / "Contents/MacOS").mkdir(parents=True)
        link = self.root / "looks-ordinary"
        link.symlink_to(app)
        for folder in (app, plain, link):
            self.stored(str(folder))
            with mock.patch.object(helpers.subprocess, "run") as run:
                rowactions.action("folder", "u")
            self.assertEqual(run.call_args[0][0][:2], ["/usr/bin/open", "-R"], folder)
        for name in ("my project", "next.js", "me.github.io", "api.v2", "site.com", "with Contents"):
            folder = self.root / "ordinary" / name
            (folder / "Contents").mkdir(parents=True)  # a plain folder of that name is not enough
            self.assertEqual(rowactions.folder_command(str(folder)), ["/usr/bin/open", os.path.realpath(str(folder))])
        upper = self.root / "Shouting.APP"  # the disk ignores case, so use a new name
        upper.mkdir()
        self.assertEqual(rowactions.folder_command(str(upper))[1], "-R")

    def test_unusual_paths_are_not_used(self):
        newline = str(self.root / "a\nb")
        os.mkdir(newline)
        escape = str(self.root / "a\x1b[201~b")
        for cwd in ("relative/folder", "-n", newline, escape):
            self.stored(cwd)
            for name in ("folder", "resume", "copycmd"):
                with mock.patch.object(helpers.subprocess, "run") as run:
                    reply = rowactions.action(name, "u")
                run.assert_not_called()
                self.assertNotIn("copy", reply)
                self.assertIn("unusual", reply["update"]["ids"]["status"]["text"])

    def test_previews_off_caches_no_message_text(self):
        self.claude_log([claude_user("SECRET OPENING WORDS", self.cwd), claude_reply("a", "SECRET REPLY", 1, self.cwd)])
        config.configure({"preview_count": "0"})
        reply = json.dumps(screen.load(force=True))
        cached = (self.root / "cache/parsed.json").read_text() + (self.root / "cache/index.json").read_text()
        self.assertNotIn("SECRET", reply + cached)
        self.assertIn("Untitled session", reply)

    def test_codex_opening_message_is_not_a_title_when_previews_are_off(self):
        thread = {"title": "please fix my secret thing…", "first_user_message": "please fix my secret thing in prod"}
        self.assertTrue(collect.codex_title(thread, "").startswith("please fix"))
        config.configure({"preview_count": "0"})
        self.assertEqual(collect.codex_title(thread, ""), "Untitled session")
        self.assertEqual(collect.codex_title(thread, "Generated name"), "Generated name")
        self.assertEqual(collect.codex_title(dict(thread, title="Fix production"), ""), "Fix production")

    def test_plan_name_cannot_expand_placeholders(self):
        note = quota.limit_widgets([time.time(), {"plan_type": "{var:x}{file:/etc/hosts}"}])["limitNote"]["text"]
        self.assertNotIn("{var:", note)
        self.assertNotIn("{file:", note)


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

    def test_script_only_updates_components_that_exist(self):
        ids = {n["id"] for n in self.nodes(screen.layout()["window"]) if "id" in n}
        self.claude_log([claude_user("hello", self.cwd)])
        for view in ("active", "archived"):
            os.environ["view"] = view
            self.assertLessEqual(set(screen.load(force=True, first=True)["update"]["ids"]), ids)


class ChangedRowTests(Base):
    """After the first draw, only rows that changed are sent."""

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


class ClosedCodexTests(Base):
    def test_database_is_read_after_codex_closes_it(self):
        path = self.root / "codex/state_5.sqlite"
        path.parent.mkdir(parents=True)
        db = collect.sqlite3.connect(str(path))
        db.execute("pragma journal_mode=wal")
        db.execute("create table threads(id text, updated_at integer, archived integer)")
        db.execute("insert into threads values(?, 1, 0)", (SESSION,))
        db.commit()
        db.execute("pragma wal_checkpoint(truncate)")
        db.close()
        for suffix in ("-wal", "-shm"):  # Codex removes these when it exits
            if os.path.exists(str(path) + suffix):
                os.remove(str(path) + suffix)
        before = path.read_bytes()
        self.assertEqual([t["id"] for t in collect.codex_threads()], [SESSION])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(os.listdir(str(path.parent)), [path.name])


class WrapperTests(Base):
    RUN = Path(dashboard.__file__).resolve().parent / "run"

    def test_runs_the_dashboard(self):
        env = dict(os.environ, alfred_theme_background="rgba(0,0,0,0.2)")
        out = subprocess.run([str(self.RUN), "appearance", "demo"], env=env, check=True, capture_output=True,
                             text=True).stdout
        self.assertEqual(json.loads(out)["alfredworkflow"]["variables"]["window_appearance"], "dark")

    def test_without_python_it_explains_and_still_opens(self):
        lone = self.root / "run"
        text = self.RUN.read_text().replace("/usr/bin/xcode-select -p", "false")
        text = text.replace("/opt/homebrew/bin/python3", "/none/a").replace("/usr/local/bin/python3", "/none/b")
        helpers.write_private(lone, text, 0o700)
        run = lambda *a: subprocess.run([str(lone), *a], env={"PATH": "/usr/bin:/bin"}, check=True,
                                        capture_output=True, text=True).stdout
        reply = json.loads(run("init"))
        self.assertEqual(reply["update"]["ids"]["status"]["text"], "Python 3 is needed")
        self.assertIn("xcode-select --install", reply["update"]["ids"]["detailPreview"]["text"])
        self.assertEqual(run("appearance", "demo"), "demo")

    def test_the_window_only_calls_the_wrapper(self):
        package = Path(dashboard.__file__).resolve().parent.parent
        self.assertNotIn("scripts/dashboard.py", (package / "ui.json").read_text())
        modules = [package / "scripts/dashboard.py"] + sorted((package / "scripts/aidash").glob("*.py"))
        self.assertGreater(len(modules), 8)
        for module in modules:
            self.assertNotIn('"src": "scripts/dashboard.py"', module.read_text(), module.name)


class PortabilityTests(Base):
    def test_tool_found_through_the_login_shell(self):
        tool = self.root / "odd prefix/mytool"
        helpers.write_private(tool, "#!/bin/sh\n", 0o700)
        helpers.find_binary.cache_clear()
        self.addCleanup(helpers.find_binary.cache_clear)
        reply = mock.Mock(stdout="welcome banner\n%s\n" % tool)
        with mock.patch.object(helpers.subprocess, "run", return_value=reply):
            self.assertEqual(helpers.find_binary("mytool"), str(tool))

    def test_custom_terminal_command(self):
        config.configure({"terminal_app": "custom", "terminal_command": "~/bin/term -e"})
        with mock.patch.object(helpers.subprocess, "Popen") as popen:
            resume.open_in_terminal("/tmp/a b.command")
        self.assertEqual(popen.call_args[0][0], [os.path.expanduser("~/bin/term"), "-e", "/tmp/a b.command"])
        config.configure({"terminal_app": "custom"})
        with self.assertRaises(ValueError):
            resume.open_in_terminal("/tmp/x")

    def test_package_has_no_machine_specific_paths(self):
        package = Path(dashboard.__file__).resolve().parents[2]
        for path in package.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".json", ".plist"):
                text = path.read_text(errors="replace")
                self.assertNotIn(str(Path.home()), text, path.name)
                self.assertNotIn("/Users/", text, path.name)


class CachedLimitsTests(Base):
    def cache(self, five, resets, age, extra=None):
        iso = datetime.fromtimestamp(resets, timezone.utc).isoformat() if resets else None
        usage = {"five_hour": {"utilization": five, "resets_at": iso}, "seven_day": None}
        helpers.write_private(self.root / "claude/.claude.json", json.dumps(dict(
            {"cachedUsageUtilization": {"fetchedAtMs": (time.time() - age) * 1000, "utilization": usage},
             "oauthAccount": {"emailAddress": "private@example.com"}}, **(extra or {}))))

    def status(self, five, resets, age):
        helpers.write_private(self.root / "claude" / config.LIMITS_FILE, json.dumps({"windows": {
            "five_hour": {"used_percentage": five, "resets_at": resets, "seen_at": time.time() - age}}}))

    def shown(self):
        widgets = quota.claude_limit_widgets(quota.claude_limits())
        return widgets["claudeLimit0Label"]["text"], widgets["claudeLimitNote"]["text"]

    def test_cache_alone_needs_no_setup_but_still_offers_it(self):
        os.mkdir(self.root / "claude")
        self.cache(30, time.time() + 3600, 7200)
        label, note = self.shown()
        self.assertTrue(label.startswith("5h 70% left"))
        self.assertIn("cached, click for live limits", note)
        reply = screen.load(force=True)
        self.assertNotIn("private@example.com", json.dumps(reply))
        self.assertIn("sheet", rowactions.action("limits-setup", ""))

    def test_newer_reading_wins_within_a_window(self):
        later = time.time() + 3600
        self.status(45, int(later), 60)
        self.cache(3, later + 2, 7200)  # same window, reset time rounded differently
        self.assertEqual(self.shown()[0][:11], "5h 55% left")
        self.assertNotIn("cached", self.shown()[1])
        self.cache(80, later, 5)
        self.assertEqual(self.shown()[0][:11], "5h 20% left")
        self.assertIn("cached", self.shown()[1])

    def test_later_window_wins_whatever_its_age(self):
        self.status(90, time.time() - 600, 700)
        self.cache(10, time.time() + 3600, 1800)
        self.assertEqual(self.shown()[0][:11], "5h 90% left")
        self.status(25, time.time() + 9000, 5000)
        self.cache(3, time.time() - 600, 10)
        self.assertEqual(self.shown()[0][:11], "5h 75% left")

    def test_expired_or_unfamiliar_cache(self):
        self.cache(3, time.time() - 600, 3600)
        self.assertIn("no new reading", self.shown()[0])
        for content in ({"cachedUsageUtilization": None}, {"cachedUsageUtilization": {"fetchedAtMs": "x"}},
                        {"cachedUsageUtilization": {"fetchedAtMs": 1, "utilization": {"five_hour": {
                            "utilization": "high"}}}},
                        {"cachedUsageUtilization": {"fetchedAtMs": 1, "utilization": {"five_hour": {
                            "utilization": 3, "resets_at": "soon"}}}}, {}):
            helpers.write_private(self.root / "claude/.claude.json", json.dumps(content))
            self.assertEqual(quota.claude_limits(), {}, content)

    def test_standard_folder_uses_the_file_in_the_home_folder(self):
        config.CLAUDE_HOME = config.HOME / ".claude"
        with mock.patch.object(helpers, "read_json", return_value={}) as read:
            quota.cached_limits()
        self.assertEqual(read.call_args[0][0], config.HOME / ".claude.json")


class ArchiveTests(Base):
    def claude_session(self, age=3600):
        log = self.claude_log([claude_user("hello", self.cwd), claude_reply("a", "hi", 5, self.cwd)])
        (log.parent / SESSION / "subagents").mkdir(parents=True)
        (log.parent / "memory").mkdir()
        old = time.time() - age
        os.utime(str(log), (old, old))
        screen.load(force=True)
        return log, "claude:" + SESSION

    def archived_log(self, log):
        return self.root / "claude" / config.ARCHIVE_DIR / log.parent.name / log.name

    def status(self, reply):
        return reply["update"]["ids"]["status"]["text"]

    def pid_record(self, pid):
        helpers.write_private(self.root / "claude/sessions/1.json",
                                json.dumps({"pid": pid, "sessionId": SESSION}))

    def test_asking_changes_nothing(self):
        log, uid = self.claude_session()
        for name, confirmed in (("delete", "delete-confirmed"), ("remove", "delete-confirmed"),
                                ("purge", "purge-confirmed")):
            reply = rowactions.action(name, uid)
            self.assertIn('"%s", "%s"' % (confirmed, uid), json.dumps(reply))
        self.assertTrue(log.exists())

    def test_archive_view_and_restore_round_trip(self):
        log, uid = self.claude_session()
        content = log.read_text()
        reply = rowactions.action("delete-confirmed", uid)
        self.assertEqual(reply["update"]["ids"]["list"]["data"]["src"], [])
        stored = self.archived_log(log)
        self.assertTrue(stored.is_file() and (stored.parent / SESSION / "subagents").is_dir())
        self.assertFalse(log.exists() or (log.parent / SESSION).exists())
        self.assertTrue((log.parent / "memory").is_dir())
        self.assertIn("Today 20 ", reply["update"]["ids"]["claudeUsage"]["text"])  # usage still counts

        os.environ["view"] = "archived"
        ids = screen.load(force=True)["update"]["ids"]
        self.assertEqual([r["uid"] for r in ids["list"]["data"]["src"]], [uid])
        self.assertEqual((ids["btnPrimary"]["text"], ids["btnRemove"]["text"]), ("Restore", "Delete…"))
        with mock.patch.object(helpers.subprocess, "run") as run:
            self.assertIn("Restore this session first", self.status(rowactions.action("resume", uid)))
        run.assert_not_called()
        reply = rowactions.action("primary", uid)
        self.assertIn("Restored", self.status(reply))
        self.assertEqual(log.read_text(), content)
        self.assertFalse(stored.exists())

    def test_restore_never_overwrites(self):
        log, uid = self.claude_session()
        rowactions.action("delete-confirmed", uid)
        os.environ["view"] = "archived"
        screen.load(force=True)
        log.write_text("newer")
        with self.assertRaises(ValueError):
            rowactions.action("restore", uid)
        self.assertEqual(log.read_text(), "newer")
        self.assertTrue(self.archived_log(log).exists())

    def test_permanent_removes_only_this_session(self):
        log, uid = self.claude_session()
        rowactions.action("purge-confirmed", uid)
        self.assertFalse(log.exists() or (log.parent / SESSION).exists())
        self.assertTrue((log.parent / "memory").is_dir())

    def test_permanent_from_archive(self):
        log, uid = self.claude_session()
        rowactions.action("delete-confirmed", uid)
        os.environ["view"] = "archived"
        screen.load(force=True)
        self.assertIn("sheet", rowactions.action("remove", uid))
        rowactions.action("purge-confirmed", uid)
        self.assertFalse(self.archived_log(log).exists())

    def test_open_or_just_written_session_is_refused(self):
        log, uid = self.claude_session()
        self.pid_record(os.getpid())
        for name in ("delete", "delete-confirmed", "purge-confirmed"):
            self.assertIn("open in Claude Code", self.status(rowactions.action(name, uid)))
        self.pid_record(99999999)
        os.utime(str(log))
        self.assertIn("last minute", self.status(rowactions.action("purge-confirmed", uid)))
        self.assertTrue(log.exists())

    def test_paths_outside_projects_or_links_are_refused(self):
        outside = self.root / (SESSION + ".jsonl")
        outside.write_text("keep")
        link = self.root / "claude/projects/p" / (SESSION + ".jsonl")
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        for path in (outside, link, link.parent):
            for change in ("archive", "restore", "purge"):
                with self.assertRaises(ValueError):
                    archiving.change_session({"tool": "claude", "id": SESSION, "path": str(path)}, change)
        with self.assertRaises(ValueError):
            archiving.change_session({"tool": "codex", "id": "abcdef0123", "path": ""}, "purge")
        self.assertEqual(outside.read_text(), "keep")

    def test_codex_commands(self):
        for change, expected in (("archive", ("archive",)), ("restore", ("unarchive",)),
                                 ("purge", ("delete", "--force"))):
            with mock.patch.object(archiving, "run_codex") as run:
                archiving.change_session({"tool": "codex", "id": SESSION, "path": ""}, change)
            run.assert_called_once_with(*expected, SESSION)

    def test_codex_open_lock_is_refused(self):
        lock = self.root / "codex/thread-writer-locks" / (SESSION + ".lock")
        lock.parent.mkdir(parents=True)
        session = {"tool": "codex", "id": SESSION, "path": ""}
        with open(lock, "w"):
            self.assertIn("Codex still has", archiving.open_reason(session))
        self.assertEqual(archiving.open_reason(session), "")
        with mock.patch.object(config, "LSOF", "/no/such/lsof"), self.assertRaises(OSError):
            archiving.open_reason(session)

    def test_view_toggle_and_demo_changes_nothing(self):
        os.environ["input"] = "demo"
        with mock.patch("builtins.print") as out:
            dashboard.main(["", "view", "toggle"])
        reply = json.loads(out.call_args[0][0])
        self.assertEqual(reply["variables"]["view"], "archived")
        uid = reply["update"]["ids"]["list"]["data"]["src"][0]["uid"]
        with mock.patch.object(archiving, "change_session") as change:
            for name in ("restore", "purge-confirmed"):
                rowactions.action(name, uid)
        change.assert_not_called()


class ConfigurationTests(Base):
    def test_blank_or_bad_values_keep_defaults(self):
        before = config.CLAUDE_HOME
        config.configure({"claude_folder": "  ", "list_limit": "lots", "limit_style": ""})
        self.assertEqual(config.CLAUDE_HOME, before)
        self.assertEqual((config.LIST_LIMIT, config.ENABLED, config.LIMIT_STYLE),
                         (60, ("claude", "codex"), "left"))

    def test_one_tool_removes_the_other_card_and_filters(self):
        os.environ["input"] = "demo"
        os.environ["filter"] = "claude"
        config.configure({"show_claude": "0", "show_codex": "1", "refresh_seconds": "0"})
        reply = screen.load(force=True, first=True)
        ids = reply["update"]["ids"]
        self.assertEqual(reply["remove"]["ids"], ["filter_claude", "filter_all", "filter_codex", "view_separator"])
        card = reply["replace"]["ids"]["cards"]
        self.assertEqual([c["id"] for c in card["components"]], ["codexCard"])
        self.assertIn("7 days", json.dumps(card))
        self.assertNotIn("codexUsage", ids)
        self.assertEqual(reply["pulse"], {"interval": 0})
        self.assertEqual({r["rowTool.text"] for r in ids["list"]["data"]["src"]}, {"Codex"})
        self.assertFalse([k for k in ids if k.startswith(("claude", "filter_"))])
        self.assertNotIn("remove", screen.load(force=True))

    def test_both_tools_remove_nothing(self):
        reply = screen.load(force=True, first=True)
        self.assertNotIn("remove", reply)
        self.assertEqual(reply["pulse"], {"interval": 30})

    def test_previews_off_keeps_text_out_of_cache(self):
        self.claude_log([claude_user("first words", self.cwd), claude_reply("a", "private answer", 1, self.cwd)])
        config.configure({"preview_count": "0"})
        reply = screen.load(force=True)
        self.assertEqual(len(reply["update"]["ids"]["list"]["data"]["src"]), 1)
        self.assertNotIn("private answer", (self.root / "cache/parsed.json").read_text())
        self.assertNotIn("private answer", json.dumps(reply))
        config.configure({"preview_count": "6"})
        self.assertIn("private answer", json.dumps(screen.load(force=True)))

    def test_used_style(self):
        config.configure({"limit_style": "used"})
        widgets = quota.window_widgets("limit", [("5h", 95, time.time() + 60), None])
        self.assertTrue(widgets["limit0Label"]["text"].startswith("5h 95% used"))
        self.assertEqual((widgets["limit0Bar"]["progress"], widgets["limit0Bar"]["colour"]), (0.95, "systemRedColor"))

    def test_resume_options_folder_and_terminal(self):
        folder = str(self.root / "other codex")
        config.configure({"codex_folder": folder, "codex_args": "--yolo -c 'a=$(boom)'", "terminal_app": "iTerm"})
        session = {"tool": "codex", "id": SESSION, "cwd": self.cwd}
        lines = resume.resume_script(session).splitlines()
        self.assertEqual(shlex.split(lines[-4]), ["export", "CODEX_HOME=" + folder])
        self.assertEqual(shlex.split(lines[-2]), ["codex", "resume", "--yolo", "-c", "a=$(boom)", SESSION])
        with mock.patch.object(helpers.subprocess, "run") as run:
            resume.open_in_terminal("/tmp/x.command")
        self.assertEqual(run.call_args[0][0], ["/usr/bin/open", "-a", "iTerm", "/tmp/x.command"])

    def test_permanent_delete_can_be_switched_off(self):
        os.environ["input"] = "demo"
        screen.load(force=True)
        uid = next(iter(json.loads((self.root / "cache/index.json").read_text())["sessions"]))
        config.configure({"allow_purge": "0"})
        for name in ("purge", "purge-confirmed"):
            reply = rowactions.action(name, uid)
            self.assertNotIn("sheet", reply)
        self.assertIn("sheet", rowactions.action("delete", uid))


class SignInTests(Base):
    def test_resume_script_supplies_the_user_name(self):
        script = resume.resume_script({"tool": "claude", "id": SESSION, "cwd": self.cwd})
        lines = [l for l in script.splitlines() if l.startswith(("export USER", "export LOGNAME"))]
        out = subprocess.run(["/bin/bash", "-c", "\n".join(lines + ['echo "$USER $LOGNAME"'])], env={},
                             capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(out[0], helpers.pwd.getpwuid(os.getuid()).pw_name)
        self.assertEqual(out[0], out[1])

    def test_standard_folder_never_overrides_the_profile(self):
        link = self.root / "claude-link"
        link.symlink_to(config.HOME / ".claude")
        for folder in ("", "~/.claude", "~/.claude/", str(link)):
            config.CLAUDE_HOME = config.HOME / ".claude"
            config.configure({"claude_folder": folder})
            self.assertEqual(resume.folder_exports("claude"), "", folder)


class TerminalTests(Base):
    CLIENTS = "100\t11\t$1\n200\t22\t$4\n"

    def launch(self, mode, terminal="", clients="", sessions="", app="/Applications/Ghostty.app"):
        """Run open_session against a pretend tmux; returns (tmux calls, programs run, opened script)."""
        config.configure({"tmux_mode": mode, "terminal_app": terminal})
        calls = []

        def fake(*args):
            calls.append(args)
            return {"list-clients": clients, "list-sessions": sessions}.get(args[0], "")

        script = self.root / "cache/resume-x.command"
        session = {"tool": "claude", "cwd": self.cwd}
        with mock.patch.object(resume, "tmux", fake), mock.patch.object(resume, "app_of", return_value=app), \
                mock.patch.object(resume, "tmux_binary", return_value="/x/tmux"), \
                mock.patch.object(helpers.subprocess, "run") as run:
            resume.open_session(session, script)
        command = run.call_args[0][0]
        opener = Path(command[-1])
        return calls, command, opener.read_text() if opener.name.startswith("tmux-") else ""

    def test_auto_adds_window_to_the_latest_visible_client(self):
        calls, command, _ = self.launch("auto", clients=self.CLIENTS)
        window = calls[-1]
        self.assertEqual(window[:3], ("new-window", "-t", "$4:"))
        self.assertNotIn(self.cwd, " ".join(window))
        self.assertEqual(command, ["/usr/bin/open", "-a", "/Applications/Ghostty.app"])

    def test_auto_without_a_client_opens_a_plain_window(self):
        for terminal, clients in (("", ""), ("Terminal", self.CLIENTS)):
            calls, command, _ = self.launch("auto", terminal, clients)
            self.assertNotIn("new-window", [c[0] for c in calls])
            self.assertTrue(command[-1].endswith("resume-x.command"))

    def test_never_ignores_tmux(self):
        calls, command, _ = self.launch("never", "Ghostty", self.CLIENTS)
        self.assertEqual(calls, [])
        self.assertEqual(command[:4], ["/usr/bin/open", "-na", "Ghostty", "--args"])
        self.assertEqual(command[-2], "-e")

    def test_always_attaches_or_starts_tmux(self):
        calls, command, script = self.launch("always", "Terminal", sessions="5\t$2\n9\t$7\n")
        self.assertEqual(calls[-1][:3], ("new-window", "-t", "$7:"))
        self.assertEqual(command[:3], ["/usr/bin/open", "-a", "Terminal"])
        self.assertIn("attach-session -t '$7'", script)
        calls, command, script = self.launch("always", "Terminal")
        self.assertIn("new-session", script)
        self.assertNotIn(self.cwd, script)

    def test_app_is_found_through_parent_processes(self):
        answers = iter(["22 tmux", "33 -/bin/bash", "1 /Applications/Ghostty.app/Contents/MacOS/ghostty"])
        with mock.patch.object(helpers.subprocess, "run",
                               side_effect=lambda *a, **k: mock.Mock(stdout=next(answers))):
            self.assertEqual(resume.app_of(11), "/Applications/Ghostty.app")


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
