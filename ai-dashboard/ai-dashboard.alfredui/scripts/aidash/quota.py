"""Plan limits: where they come from, how they are shown and how to set them up."""
import time

from . import config, helpers, replies


def window_widgets(prefix, windows):
    """Label and bar updates for two limit windows: (name, used percent, reset time) or None."""
    updates = {}
    for index, window in enumerate(windows):
        label, bar = "%s%dLabel" % (prefix, index), "%s%dBar" % (prefix, index)
        if not window:
            updates[label] = {"text": ""}
            updates[bar] = {"hidden": True}
            continue
        name, used, resets = window
        if resets and resets <= time.time():
            # The reading predates the reset, so what is left now is unknown.
            updates[label] = {"text": "%s reset %s · no new reading" % (name, helpers.reset_time(resets))}
            updates[bar] = {"hidden": True}
        else:
            left = min(100, max(0, 100 - round(used)))
            shown = 100 - left if config.LIMIT_STYLE == "used" else left
            updates[label] = {"text": "%s %d%% %s%s" % (name, shown, config.LIMIT_STYLE,
                                                        " · resets %s" % helpers.reset_time(resets) if resets else "")}
            colour = "systemRedColor" if left <= 20 else "systemYellowColor" if left <= 50 else "systemGreenColor"
            updates[bar] = {"hidden": False, "progress": shown / 100, "colour": colour}
    return updates


def limit_widgets(reading):
    """Codex limits, as found in its session logs."""
    note = "No limit reading in Codex logs yet"
    windows = [None, None]
    if reading:
        stamp, limits = reading
        for index, key in enumerate(("primary", "secondary")):
            window = limits.get(key)
            if window:
                minutes = window.get("window_minutes") or 0
                name = "%dd" % (minutes // 1440) if minutes >= 1440 else "%dh" % (minutes // 60)
                windows[index] = (name, float(window.get("used_percent") or 0), window.get("resets_at") or 0)
        plan = limits.get("plan_type")
        note = "As of %s%s" % (helpers.clock(stamp), " · %s plan" % plan if plan else "")
    return dict(window_widgets("limit", windows), limitNote={"text": helpers.clean(helpers.one_line(note, 80))})


def status_line_limits():
    """The reading saved by the status line; Claude's session logs do not hold it.

    Accepts Claude Code's rate_limits object as it is (or the whole status line input), and the
    {"windows": ...} form with its own reading times.
    """
    path = config.CLAUDE_HOME / config.LIMITS_FILE
    saved = helpers.read_json(path, {})
    if not isinstance(saved, dict):
        return {}
    if isinstance(saved.get("windows"), dict):
        return saved
    raw = saved.get("rate_limits", saved)
    if not isinstance(raw, dict):
        return {}
    seen = helpers.mtime(str(path))
    windows = {name: dict(raw[name], seen_at=seen) for name in ("five_hour", "seven_day")
               if isinstance(raw.get(name), dict)}
    return {"windows": windows} if windows else {}


def cached_limits():
    """Claude Code's own copy of the limits. Undocumented and often hours old, but needs no setup.

    Only the usage field is used; the rest of the file, which holds account details, is ignored.
    """
    standard = config.CLAUDE_HOME.resolve() == (config.HOME / ".claude").resolve()
    data = helpers.read_json((config.HOME if standard else config.CLAUDE_HOME) / ".claude.json", {})
    try:
        cache = data["cachedUsageUtilization"]
        seen = float(cache["fetchedAtMs"]) / 1000
        usage = dict(cache["utilization"])
    except (KeyError, TypeError, ValueError):
        return {}
    windows = {}
    for name in ("five_hour", "seven_day"):
        window = usage.get(name)
        if not isinstance(window, dict) or not isinstance(window.get("utilization"), (int, float)):
            continue
        resets = window.get("resets_at")
        stamp = helpers.parse_time(resets) if resets else 0
        if resets and not stamp:
            continue  # an unfamiliar time format
        windows[name] = {"used_percentage": window["utilization"], "resets_at": stamp, "seen_at": seen,
                         "source": "cache"}
    return {"windows": windows} if windows else {}


def limit_window(window):
    """One saved window with numeric values, or None if it cannot be used."""
    try:
        resets = window.get("resets_at") or 0
        return dict(window, resets_at=helpers.parse_time(resets) if isinstance(resets, str) else float(resets),
                    used_percentage=float(window["used_percentage"]), seen_at=float(window["seen_at"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def claude_limits():
    """For each window, the better of the status line reading and Claude Code's cache.

    A later window (by reset time) wins; within the same window, the more recent reading does.
    """
    best = {}
    for saved in (status_line_limits(), cached_limits()):
        for name in ("five_hour", "seven_day"):
            window = limit_window((saved.get("windows") or {}).get(name))
            if window is None:
                continue
            held = best.get(name)
            if held is not None:
                gap = window["resets_at"] - held["resets_at"]
                # The two sources round the same reset time slightly differently.
                if gap < -300 or (gap <= 300 and window["seen_at"] <= held["seen_at"]):
                    continue
            best[name] = window
    return {"windows": best} if best else {}


def claude_limit_widgets(saved):
    windows, seen, cached = [None, None], [], []
    for index, (key, name) in enumerate((("five_hour", "5h"), ("seven_day", "7d"))):
        try:
            window = limit_window(saved["windows"][key])
        except (KeyError, TypeError):
            continue
        if window is None:
            continue
        windows[index] = (name, window["used_percentage"], window["resets_at"])
        seen.append(window["seen_at"])
        cached.append(window.get("source") == "cache")
    if not seen:
        note = "Click here to set up Claude limits"
    elif any(cached):
        # With mixed sources, give the older time so a stale figure never looks fresh.
        note = "As of %s · cached, click for live limits" % helpers.clock(max(seen) if all(cached) else min(seen))
    else:
        note = "As of %s" % helpers.clock(max(seen))
    return dict(window_widgets("claudeLimit", windows), claudeLimitNote={"text": note})


def limits_help():
    """Say how to set up Claude Code limits. Nothing is written; Claude Code's /statusline does the setup."""
    if status_line_limits().get("windows"):
        return {}
    target = config.CLAUDE_HOME / config.LIMITS_FILE
    shown = "~/.claude/" + config.LIMITS_FILE if target == config.HOME / ".claude" / config.LIMITS_FILE else str(target)
    command = ("/statusline keep my status line as it is, and when rate_limits is present save that object to "
               + shown)
    label = lambda text, **extra: replies.label(text, constraints={"size": {"width": 440}}, **extra)
    return replies.popup("limitsSetup", [
        label("Show Claude Code limits", font={"size": "large", "weight": "semibold"}),
        label("Claude Code gives its limits to the status line. In Claude Code, use /statusline to set it up:"),
        label(command, selectable=True, font={"monospaced": True}),
        replies.button("Done", replies.close_popup("limitsSetup"), shortcut="↩"),
    ])
