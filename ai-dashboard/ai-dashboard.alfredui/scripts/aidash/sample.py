"""Invented data for `aidash demo`."""
import os
import time

from . import config


def demo_words():
    """What followed the keyword: ["demo"] or ["demo", "archived"] asks for sample data."""
    return os.environ.get("input", "").lower().split()


def is_demo():
    return demo_words()[:1] == ["demo"]


def demo_data():
    """Invented sessions, safe to show in screenshots. Nothing here is read from this Mac."""
    now = time.time()
    models = {"claude": "claude-sonnet-5-5", "codex": "gpt-6.1-sol"}

    def session(tool, number, title, folder, branch, age, count, ask, answer):
        return {"tool": tool, "id": "00000000-0000-4000-8000-%012d" % number, "path": "",
                "cwd": str(config.HOME / "Demo" / folder), "title": title, "branch": branch,
                "model": models[tool], "updated": now - age, "tokens": count,
                "messages": [["user", ask, now - age - 60], ["assistant", answer, now - age]]}

    claude = [
        session("claude", 1, "Add dark mode to the settings page", "web-app", "feature/dark-mode", 40, 182000,
                "Add a dark mode toggle to settings. Remember the choice.",
                "Done. The toggle follows the system theme until you change it.\n\n"
                "- `settings.tsx`: new switch\n- `theme.css`: dark colour tokens\n\n"
                "```ts\nconst theme = saved ?? systemTheme()\n```"),
        session("claude", 2, "Fix flaky login test", "api-server", "main", 2400, 64000,
                "Why does the login test fail sometimes?",
                "The test relied on wall-clock time. It now uses a fixed clock and passed 200 runs in a row."),
        session("claude", 7, "Speed up the search endpoint", "api-server", "perf/search", 11000, 231000,
                "Search takes two seconds. Can you find out why?",
                "Each result ran its own query. One joined query brings it to about 120 ms."),
        session("claude", 8, "Draft the onboarding guide", "docs", "main", 95000, 38000,
                "Write a getting-started guide for new contributors.", "Added `CONTRIBUTING.md` with setup steps."),
        session("claude", 9, "Migrate the config to TOML", "cli-tool", "refactor/config", 260000, 97000,
                "Move our config from JSON to TOML.", "Migrated, with a fallback that still reads the old file."),
    ]
    codex = [
        session("codex", 3, "Write a release checklist", "cli-tool", "main", 900, 41000,
                "Draft a release checklist.", "## Checklist\n\n1. Tag the build\n2. Update the notes\n3. Publish"),
        session("codex", 10, "Add keyboard shortcuts", "web-app", "feature/shortcuts", 6200, 118000,
                "Add shortcuts for search and new item.", "Added both, with a help panel on `?`."),
        session("codex", 4, "Tidy shell aliases", "dotfiles", "main", 90000, 12000,
                "Clean up my aliases.", "Removed duplicates and grouped the rest."),
        session("codex", 11, "Resize images in the build step", "marketing-site", "main", 180000, 54000,
                "Images are too large. Resize them during the build.", "The build now writes three sizes per image."),
    ]
    limits = [now - 120, {"plan_type": "demo",
                          "primary": {"used_percent": 62, "window_minutes": 300, "resets_at": now + 5400},
                          "secondary": {"used_percent": 23, "window_minutes": 10080, "resets_at": now + 400000}}]
    if os.environ.get("view") == "archived":
        claude = [session("claude", 5, "Old prototype notes", "prototype", "main", 900000, 8000,
                          "Summarise the prototype.", "Summary saved."),
                  session("claude", 12, "Spike: offline sync", "web-app", "spike/offline", 1700000, 143000,
                          "Could the app work offline?", "Yes, with a queued write layer. Notes are in `sync.md`.")]
        codex = [session("codex", 6, "One-off rename script", "scratch", "main", 1300000, 3000,
                         "Rename these files.", "Renamed twelve files.")]
    return (
        {"sessions": claude, "days": [210e3, 480e3, 350e3, 90e3, 620e3, 540e3, 410e3], "week": 14, "found": True,
         "limits": {"windows": {
             "five_hour": {"used_percentage": 28, "resets_at": now + 9000, "seen_at": now - 60},
             "seven_day": {"used_percentage": 57, "resets_at": now + 200000, "seen_at": now - 60}}}},
        {"sessions": codex, "days": [120e3, 40e3, 260e3, 310e3, 80e3, 190e3, 225e3], "week": 8, "found": True,
         "limits": limits, "error": ""},
    )
