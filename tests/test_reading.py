"""Reading session logs and databases."""
import json
import os
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from support import SESSION, stamp, claude_user, claude_reply, Base  # noqa: E402  (sets the import path)
import dashboard  # noqa: E402
from aidash import collect, config, helpers, logparse, quota, resume, rowactions, screen  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
