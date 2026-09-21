"""Adapters for Claude Code messages and Codex rollout response items.

Only commands, request snippets and size/outcome metadata are retained. Tool output
and reasoning bodies are measured, not stored. No commands are ever executed.
"""
import json
import re
from pathlib import Path
from .models import Action, Session, as_text, estimate_tokens

PARSER_VERSION = "1"


def records(path, warnings):
    with Path(path).open(encoding="utf-8", errors="replace") as stream:
        if Path(path).suffix == ".json":
            try:
                data = json.load(stream)
            except json.JSONDecodeError:
                warnings.append("Invalid JSON file")
                return
            if isinstance(data, dict):
                data = data.get("messages", data.get("items", [data]))
            if not isinstance(data, list):
                warnings.append("Expected a JSON object or array")
                return
            for index, record in enumerate(data, 1):
                if isinstance(record, dict):
                    yield index, record
            return
        for index, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"Invalid JSON at line {index}; skipped")
                continue
            if isinstance(record, dict):
                yield index, record


def arguments(raw):
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"input": raw}
    except (ValueError, TypeError):
        return {"input": raw}


def command_for(tool, args):
    command = args.get("command", args.get("cmd", args.get("input", "")))
    if isinstance(command, list):
        if len(command) >= 3 and command[1] in {"-lc", "-c"}:
            return str(command[2])
        return " ".join(map(str, command))
    if command:
        return as_text(command)
    return as_text(args)


def outcome(value, explicit_error=None):
    if explicit_error:
        return False
    data = value
    if isinstance(value, str):
        try:
            data = json.loads(value)
        except ValueError:
            match = re.search(r"(?:Process exited with code|Process exit code|Exit code:)\s*(-?\d+)", value, re.I)
            return int(match.group(1)) == 0 if match else (True if explicit_error is False else None)
    if isinstance(data, dict):
        code = data.get("exit_code")
        if code is not None:
            try:
                return int(code) == 0
            except (TypeError, ValueError):
                return None
        if "is_error" in data:
            return not bool(data["is_error"])
    return True if explicit_error is False else None


class Builder:
    def __init__(self, session, normalizer):
        self.session = session
        self.normalizer = normalizer
        self.calls = {}
        self.seen = set()
        self.turn_ids = {}
        self.turn = 0
        self.request = 0
        self.pending_tokens = 0

    def user(self, text):
        if text:
            self.request += 1
            self.pending_tokens = 0
            self.session.requests.append(text[:1000])

    def model(self, key=None):
        if key and key in self.turn_ids:
            self.turn = self.turn_ids[key]
        else:
            self.turn = max(self.turn_ids.values(), default=self.turn) + 1
            if key:
                self.turn_ids[key] = self.turn

    def call(self, tool, raw, call_id, line, timestamp):
        if call_id and call_id in self.calls:
            return
        args = arguments(raw)
        command = command_for(tool, args)
        action = Action(tool=tool, command=command, raw_arguments=as_text(raw), call_id=call_id,
                        line=line, timestamp=timestamp, request=self.request, turn=self.turn,
                        assistant_tokens=self.pending_tokens + estimate_tokens(raw))
        self.pending_tokens = 0
        action.category = self.normalizer.classify(tool, command)
        self.session.actions.append(action)
        if call_id:
            self.calls[call_id] = action

    def result(self, call_id, output, error=None):
        action = self.calls.get(call_id)
        if action:
            action.output_tokens += estimate_tokens(output)
            result = outcome(output, error)
            if result is not None:
                action.success = result if action.success is not False else False


def parse(path, normalizer, source=None):
    session = Session(session_id=Path(path).stem, source=source or "unknown")
    builder = Builder(session, normalizer)
    codex_assistant_open = False
    pending_user_event = None
    for line, record in records(path, session.warnings):
        kind = record.get("type", "")
        timestamp = record.get("timestamp", "")
        if not session.timestamp:
            session.timestamp = timestamp
        if session.source == "unknown":
            if kind in {"session_meta", "response_item", "turn_context", "event_msg"}:
                session.source = "codex"
            elif kind in {"assistant", "user"} and "message" in record:
                session.source = "claude"
        if session.source == "claude":
            session.session_id = record.get("sessionId", session.session_id)
            session.project = record.get("cwd", session.project)
            message = record.get("message", {})
            if not isinstance(message, dict):
                continue
            record_id = record.get("uuid")
            if record_id and record_id in builder.seen:
                continue
            if record_id:
                builder.seen.add(record_id)
            content = message.get("content", [])
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list):
                continue
            content = [item for item in content if isinstance(item, dict)]
            role = message.get("role", kind)
            if role == "assistant":
                builder.model(message.get("id"))
                for item in content:
                    if item.get("type") in {"text", "thinking"}:
                        builder.pending_tokens += estimate_tokens(item.get("text", item.get("thinking", "")))
                    elif item.get("type") == "tool_use":
                        builder.call(item.get("name", "unknown"), item.get("input", {}), item.get("id", ""), line, timestamp)
            elif role == "user":
                text = "\n".join(i.get("text", "") for i in content if i.get("type") == "text")
                # Tool responses are user-role transport messages, not new requests.
                if text and not any(i.get("type") == "tool_result" for i in content):
                    builder.user(text)
                for item in content:
                    if item.get("type") == "tool_result":
                        builder.result(item.get("tool_use_id"), item.get("content", ""), item.get("is_error"))
        elif session.source == "codex":
            payload = record.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if kind == "session_meta":
                session.session_id = payload.get("id", session.session_id)
                session.project = payload.get("cwd", session.project)
            elif kind == "turn_context":
                session.project = payload.get("cwd", session.project)
            elif kind == "event_msg" and payload.get("type") == "user_message":
                pending_user_event = str(payload.get("message", ""))
                builder.user(pending_user_event)
                codex_assistant_open = False
            elif kind == "response_item":
                item_type = payload.get("type")
                if item_type == "message":
                    content = payload.get("content", [])
                    text = "\n".join(i.get("text", "") for i in content if isinstance(i, dict)) if isinstance(content, list) else str(content)
                    if payload.get("role") == "user":
                        if pending_user_event != text:
                            builder.user(text)
                        pending_user_event = None
                        codex_assistant_open = False
                    elif payload.get("role") == "assistant":
                        if not codex_assistant_open:
                            builder.model()
                        builder.pending_tokens += estimate_tokens(text)
                        codex_assistant_open = True
                elif item_type == "reasoning":
                    if not codex_assistant_open:
                        builder.model()
                    builder.pending_tokens += estimate_tokens(payload.get("summary", []))
                    codex_assistant_open = True
                elif item_type in {"function_call", "custom_tool_call"}:
                    if not codex_assistant_open:
                        builder.model()
                    builder.call(payload.get("name", "unknown"), payload.get("arguments", payload.get("input", {})), payload.get("call_id", ""), line, timestamp)
                    codex_assistant_open = True
                elif item_type in {"function_call_output", "custom_tool_call_output"}:
                    builder.result(payload.get("call_id"), payload.get("output", ""))
                    codex_assistant_open = False
    return session
