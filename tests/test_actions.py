"""Archiving, resuming and settings."""
import json
import os
import plistlib
import shlex
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from support import SESSION, claude_user, claude_reply, Base  # noqa: E402  (sets the import path)
import dashboard  # noqa: E402
from aidash import archiving, config, helpers, quota, resume, rowactions, screen  # noqa: E402


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

    def test_dialog_passes_its_tick_to_the_confirmed_action(self):
        _log, uid = self.claude_session()
        sheet = json.dumps(rowactions.action("delete", uid))
        self.assertIn('"dont_ask": "{value:dontAsk.checked}"', sheet)
        self.assertIn('"delete-confirmed", "%s", "{var:dont_ask}"' % uid, sheet)

    def test_tick_turns_asking_off_only_after_the_change(self):
        log, uid = self.claude_session()
        with mock.patch.object(archiving.subprocess, "run") as run:
            reply = rowactions.action("delete-confirmed", uid, "0")
            run.assert_not_called()
            os.environ["view"] = "archived"
            screen.load(force=True)
            reply = rowactions.action("purge-confirmed", uid, "1")
        self.assertEqual(run.call_args[0][0][-2:], ["confirm_purge", config.BUNDLE_ID])
        self.assertIn("won’t ask again", self.status(reply))

    def test_saved_setting_beats_what_the_open_window_passed_in(self):
        config.configure({"confirm_archive": "0"})  # the window opened while asking was off
        self.assertTrue(config.asks("archive"))  # no saved file: the default
        for saved, asks in (({"confirm_archive": "0"}, False), ({"confirm_archive": False}, False),
                            ({"confirm_archive": True}, True), ({}, True)):  # {}: back to the default
            config.PREFS.write_bytes(plistlib.dumps(saved))
            self.assertEqual(config.asks("archive"), asks, saved)
        config.PREFS.write_bytes(b"not a plist")
        self.assertFalse(config.asks("archive"))
        self.assertTrue(config.asks("purge"))

    def test_failed_save_is_reported_and_keeps_asking(self):
        _log, uid = self.claude_session()
        with mock.patch.object(archiving.subprocess, "run", side_effect=OSError("no")):
            reply = rowactions.action("delete-confirmed", uid, "true")
        self.assertIn("could not save", self.status(reply))

    def test_without_asking_it_acts_at_once_but_not_on_a_repeat(self):
        log, uid = self.claude_session()
        config.PREFS.write_bytes(plistlib.dumps({"confirm_archive": "0", "confirm_purge": "0"}))
        self.assertIn("Archived", self.status(rowactions.action("delete", uid)))
        self.assertTrue(self.archived_log(log).exists())
        os.environ["view"] = "archived"
        screen.load(force=True)
        self.assertIn("repeated", self.status(rowactions.action("purge", uid)))
        self.assertTrue(self.archived_log(log).exists())
        with mock.patch.object(config, "REPEAT_SECONDS", 0):
            self.assertIn("Permanently deleted", self.status(rowactions.action("purge", uid)))
        self.assertFalse(self.archived_log(log).exists())

    def test_without_asking_open_sessions_are_still_refused(self):
        log, uid = self.claude_session()
        config.PREFS.write_bytes(plistlib.dumps({"confirm_archive": "0", "confirm_purge": "0"}))
        self.pid_record(os.getpid())
        for name in ("delete", "purge"):
            self.assertIn("open", self.status(rowactions.action(name, uid)))
        self.assertTrue(log.exists())

    def test_each_setting_is_separate_and_defaults_to_asking(self):
        _log, uid = self.claude_session()
        config.PREFS.write_bytes(plistlib.dumps({"confirm_archive": "0"}))
        self.assertIn("sheet", rowactions.action("purge", uid))

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
        self.assertEqual((ids["btnPrimary"]["text"], ids["btnRemove"]["enabled"]), ("Restore", False))
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
        self.assertEqual(reply["remove"]["ids"], ["col_filter_claude", "col_filter_all", "col_filter_codex", "view_separator"])
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


if __name__ == "__main__":
    unittest.main()
