"""Usage limits, their setup and sign-in."""
import json
import os
import subprocess
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from support import SESSION, Base  # noqa: E402  (sets the import path)
import dashboard  # noqa: E402
from aidash import config, helpers, quota, resume, rowactions, screen  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
