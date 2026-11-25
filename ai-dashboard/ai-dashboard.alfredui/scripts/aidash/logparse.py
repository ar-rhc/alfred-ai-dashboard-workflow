"""Incremental parsing of Claude Code and Codex session logs."""
import contextlib
import json
import os
import re
import subprocess

from . import config, helpers


def new_state():
    return {"title": "", "custom_title": "", "first_prompt": "", "cwd": "", "branch": "", "model": "",
            "entrypoint": "", "talked": False, "messages": [], "usage": {}, "limits": {}, "total": None}


def add_message(state, role, text, stamp):
    text = text.strip()
    if not text:
        return
    state["talked"] = True
    if not config.PREVIEW_MESSAGES:
        return
    messages = state["messages"]
    if messages and messages[-1][0] == role == "assistant":
        messages[-1] = [role, helpers.clip(messages[-1][1] + "\n\n" + text), stamp]
    else:
        messages.append([role, helpers.clip(text), stamp])
    del messages[:-config.PREVIEW_MESSAGES]


def text_blocks(content, kinds):
    if isinstance(content, str):
        return [content]
    return [b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") in kinds]


CLAUDE_MARKERS = (b'"assistant"', b'"user"', b'"ai-title"', b'"custom-title"')


def claude_line(state, row, project, is_subagent):
    kind = row.get("type")
    if kind == "ai-title":
        state["title"] = row.get("aiTitle") or state["title"]
        return
    if kind == "custom-title":
        state["custom_title"] = row.get("customTitle") or state["custom_title"]
        return
    if kind not in ("user", "assistant"):
        return
    message = row.get("message") or {}
    stamp = helpers.parse_time(row.get("timestamp"))
    cwd = row.get("cwd") or ""
    # `claude --resume` looks a session up by the folder it was created in, which
    # is the one whose name matches the log's project directory.
    if cwd and (not state["cwd"] or re.sub(r"[^A-Za-z0-9]", "-", cwd) == project):
        state["cwd"] = cwd
    state["branch"] = row.get("gitBranch") or state["branch"]
    state["entrypoint"] = state["entrypoint"] or row.get("entrypoint") or ""

    if kind == "assistant":
        model = message.get("model") or ""
        if model == "<synthetic>":
            return
        usage = message.get("usage")
        key = message.get("id") or row.get("requestId") or row.get("uuid")
        if usage and key:
            # Streaming writes several lines per message: keep one entry per id.
            fresh = sum(usage.get(k) or 0 for k in
                        ("input_tokens", "cache_creation_input_tokens", "output_tokens"))
            old = state["usage"].get(key)
            if not old or fresh >= old[1]:
                state["usage"][key] = [stamp, fresh, usage.get("cache_read_input_tokens") or 0]
        if not (is_subagent or row.get("isSidechain")):
            state["model"] = model or state["model"]
            add_message(state, "assistant", "\n\n".join(text_blocks(message.get("content"), ("text",))), stamp)
        return

    if (is_subagent or row.get("isSidechain") or row.get("isMeta") or row.get("isCompactSummary")
            or (row.get("origin") or {}).get("kind") not in (None, "human")):
        return
    parts = [t.strip() for t in text_blocks(message.get("content"), ("text",))]
    text = "\n\n".join(t for t in parts if t and not t.startswith(("<", "[Request interrupted")))
    if text:
        if config.PREVIEW_MESSAGES:  # an excerpt used as a fallback title, so it follows the same setting
            state["first_prompt"] = state["first_prompt"] or helpers.one_line(text)
        add_message(state, "user", text, stamp)


CODEX_MARKERS = (b'"token_count"', b'"message"', b'"turn_context"')


def codex_line(state, row):
    kind, payload = row.get("type"), row.get("payload") or {}
    stamp = helpers.parse_time(row.get("timestamp"))
    if kind == "turn_context":
        state["model"] = payload.get("model") or state["model"]
    elif kind == "event_msg" and payload.get("type") == "token_count":
        limits = payload.get("rate_limits")
        if limits and (limits.get("primary") or limits.get("secondary")):
            state["limits"][limits.get("limit_id") or "codex"] = [stamp, limits]
        info = payload.get("info") or {}
        total, last = info.get("total_token_usage"), info.get("last_token_usage")
        # The same usage is re-sent with rate-limit updates; count it once.
        if total and last and total != state["total"]:
            state["total"] = total
            fresh = max(0, (last.get("input_tokens") or 0) - (last.get("cached_input_tokens") or 0))
            fresh += last.get("output_tokens") or 0
            key = "%s|%s|%s" % (row.get("timestamp"), total.get("total_tokens"), last.get("total_tokens"))
            state["usage"][key] = [stamp, fresh, last.get("cached_input_tokens") or 0]
    elif kind == "response_item" and payload.get("type") == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return
        parts = [t.strip() for t in text_blocks(payload.get("content"), ("input_text", "output_text"))]
        if role == "user":
            parts = [t for t in parts if t and not t.startswith(("<", "# AGENTS.md"))]
        add_message(state, role, "\n\n".join(parts), stamp)


@contextlib.contextmanager
def open_log(path):
    """Open a session log; Codex compresses older ones with Zstandard."""
    if not path.endswith(".zst"):
        with open(path, "rb") as handle:
            yield handle
        return
    binary = helpers.find_binary("zstd")
    if not binary:
        raise OSError("zstd is not installed")
    with subprocess.Popen([binary, "-dcq", "--", path], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        yield process.stdout


class Parser:
    """Parses session logs once, then only the bytes appended since the last run."""

    def __init__(self):
        self.path = config.CACHE_DIR / "parsed.json"
        data = helpers.read_json(self.path, {})
        self.version = [config.CACHE_VERSION, config.PREVIEW_MESSAGES]  # excerpts depend on the setting
        self.old = data.get("files", {}) if data.get("version") == self.version else {}
        self.new = {}
        self.changed = False

    def parse(self, path, tool, project="", is_subagent=False):
        if path in self.new:
            return self.new[path]["state"]
        try:
            info = os.stat(path)
        except OSError:
            return None
        entry = self.old.get(path)
        if entry and entry["mtime"] == info.st_mtime and entry["size"] == info.st_size:
            self.new[path] = entry
            return entry["state"]
        compressed = path.endswith(".zst")
        if not entry or compressed or info.st_size < entry["size"]:
            entry = {"offset": 0, "state": new_state()}
        state, offset = entry["state"], entry["offset"]
        markers = CLAUDE_MARKERS if tool == "claude" else CODEX_MARKERS
        try:
            with open_log(path) as handle:
                if not compressed:
                    handle.seek(offset)
                for line in handle:
                    if not line.endswith(b"\n"):
                        break  # still being written
                    offset += len(line)
                    if not any(m in line for m in markers):
                        continue
                    try:
                        row = json.loads(line.decode("utf-8", "replace"))
                        if tool == "claude":
                            claude_line(state, row, project, is_subagent)
                        else:
                            codex_line(state, row)
                    except (ValueError, AttributeError, TypeError):
                        continue  # one unexpected record must not hide the session
        except OSError:
            return None
        self.new[path] = {"mtime": info.st_mtime, "size": info.st_size, "offset": offset, "state": state}
        self.changed = True
        return state

    def save(self):
        if self.changed or set(self.new) != set(self.old):
            helpers.write_private(self.path, json.dumps({"version": self.version, "files": self.new}))
