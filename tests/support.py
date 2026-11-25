"""Shared setup for the offline tests, which use invented logs in a temporary folder."""
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai-dashboard/ai-dashboard.alfredui/scripts"))
from aidash import config  # noqa: E402

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
                    "ALLOW_PURGE", "TERMINAL_APP", "EXTRA_ARGS", "TMUX_MODE", "TERMINAL_COMMAND",
                    "ROW_HEIGHT", "CONFIRM_ARCHIVE", "CONFIRM_PURGE")
        patches = [mock.patch.object(config, name, getattr(config, name)) for name in settings]
        patches += [mock.patch.object(config, "CLAUDE_HOME", self.root / "claude"),
                   mock.patch.object(config, "CODEX_HOME", self.root / "codex"),
                   mock.patch.object(config, "CACHE_DIR", self.root / "cache"),
                   mock.patch.object(config, "PREFS", self.root / "prefs.plist"),
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
