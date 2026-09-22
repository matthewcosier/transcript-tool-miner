"""Adapters for Claude Code messages and Codex rollout response items.

Only commands, request snippets and size/outcome metadata are retained. Tool output
and reasoning bodies are measured, not stored. No commands are ever executed.
"""
from collections import OrderedDict
import hashlib
import json
import re
from pathlib import Path
from .models import Action, Session, as_text, estimate_tokens
from .results import result_metadata, compact_validation
from .usage import UsageCollector

PARSER_VERSION = "2.3"


def records(path, warnings, digest=None):
    with Path(path).open('rb') as stream:
        if Path(path).suffix == '.json':
            raw = stream.read()
            if digest is not None: digest.update(raw)
            try: data = json.loads(raw)
            except (ValueError, UnicodeError):
                warnings.append('Invalid JSON file'); return
            legacy = isinstance(data, dict) and isinstance(data.get('session'), dict) and isinstance(data.get('items'), list)
            if legacy:
                yield 0, {'type': 'session_meta', 'payload': data['session']}
            if isinstance(data, dict): data = data.get('messages', data.get('items', [data]))
            if not isinstance(data, list):
                warnings.append('Expected a JSON object or array'); return
            for index, record in enumerate(data, 1):
                if isinstance(record, dict):
                    if legacy and record.get('type') in {'message', 'reasoning', 'function_call', 'function_call_output', 'custom_tool_call', 'custom_tool_call_output'}:
                        record = {'type': 'response_item', 'payload': record}
                    yield index, record
            return
        for index, raw in enumerate(stream, 1):
            if digest is not None: digest.update(raw)
            if not raw.strip(): continue
            try: record = json.loads(raw)
            except (ValueError, UnicodeError):
                warnings.append(f'Invalid JSON at line {index}; skipped'); continue
            if isinstance(record, dict): yield index, record


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
        self.processes = {}
        self.next_turn = 0
        self.results_seen = set()
        self.cached_results = OrderedDict()
        self.usage = UsageCollector()

    def user(self, text):
        if text:
            self.request += 1
            self.pending_tokens = 0
            self.cached_results.clear()
            self.session.requests.append(text[:1000])

    def model(self, key=None):
        if key and key in self.turn_ids:
            self.turn = self.turn_ids[key]
        else:
            self.next_turn += 1
            self.turn = self.next_turn
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
        operations = self.normalizer.operations(tool, args, self.session.project)
        action.operations = [op.record() for op in operations]
        action.category = operations[0].kind if len(operations) == 1 else 'compound'
        action.output_attribution = 'single_operation' if len(operations) == 1 and operations[0].kind not in {'unknown', 'poll'} else 'unattributed_batch'
        if len(operations) == 1 and operations[0].kind == 'poll':
            action.process_id = operations[0].process_id
            origin = self.processes.get(action.process_id)
            if origin and origin.request == action.request:
                action.linked_to = origin.call_id
        self.session.actions.append(action)
        self.usage.tool(self.turn, call_id)
        if call_id:
            self.calls[call_id] = action

    def result(self, call_id, output, error=None, line=0):
        action = self.calls.get(call_id)
        if not action: return
        result_key = (call_id, error, hashlib.sha256(as_text(output).encode('utf-8', errors='replace')).digest())
        if result_key in self.results_seen: return
        self.results_seen.add(result_key)
        success, reference, complete, text = result_metadata(output, error)
        if reference and not action.linked_to:
            action.process_id = reference
            self.processes[reference] = action
        target = self.calls.get(action.linked_to, action)
        target.output_tokens += estimate_tokens(output)
        target.result_lines.append(line)
        if action.linked_to and not complete and not text.strip():
            action.empty_poll = True
            target._empty_poll_tokens = getattr(target, '_empty_poll_tokens', 0) + estimate_tokens(output)
        if complete:
            target.completion_observed = True
            target.poll_reduction_tokens = getattr(target, '_empty_poll_tokens', 0)
        if success is not None:
            target.success = success if target.success is not False else False
        if action.linked_to:
            action.success = success
        readonly = {'read_file', 'find_related_files', 'search_text', 'git_status', 'git_log', 'git_diff_patch', 'git_diff_names', 'git_diff_stat'}
        labels = [op['label'] for op in target.operations]
        # An explicit result-reference contract can reuse byte-identical content,
        # but never suppress the fresh read needed to establish that equality.
        if labels and all(label in readonly for label in labels) and target.success is not False and not reference and len(target.result_lines) == 1 and not action.linked_to:
            # Hash the delivered content, not a guessed substring after words
            # such as "Output:" that can occur inside source files. Only direct
            # shell tools' documented structured envelope is stripped once.
            content = as_text(output)
            if target.tool.split('.')[-1].lower() in {'exec_command', 'shell', 'shell_command'}:
                envelope = output
                if isinstance(output, str):
                    try: envelope = json.loads(output)
                    except ValueError: envelope = None
                if isinstance(envelope, dict) and 'exit_code' in envelope and isinstance(envelope.get('output'), str):
                    content = envelope['output']
            if content:
                key = (tuple(op['exact'] for op in target.operations), hashlib.sha256(content.encode('utf-8', errors='replace')).digest())
                previous = self.cached_results.get(key)
                if previous and previous[0] != target.call_id and 0 < self.turn - previous[1] <= 5:
                    replacement = estimate_tokens({'unchanged': True, 'previous_result': previous[0], 'full_result_available': True})
                    target.repeat_output_reduction_tokens = max(0, estimate_tokens(output)-max(200,replacement))
                    target.repeat_of = previous[0]
                self.cached_results[key] = (target.call_id, self.turn)
                self.cached_results.move_to_end(key)
                if len(self.cached_results) > 64: self.cached_results.popitem(last=False)
        # Do not apply a single-command result contract to aggregate batches.
        if target.output_attribution == 'single_operation':
            compact = compact_validation(text, target.operations[0], target.success, complete)
            if compact:
                # Keep all earlier output: only the known successful terminal
                # result is reduced; polling/intermediate output gets no credit.
                target.compact_output_tokens = target.output_tokens - estimate_tokens(output) + compact['tokens']
                target.reduction_reason = 'validation_status_contract'


def parse(path, normalizer, source=None):
    session = Session(session_id=Path(path).stem, source=source or "unknown")
    builder = Builder(session, normalizer)
    codex_assistant_open = False
    pending_user_event = None
    digest = hashlib.sha256()
    for line, record in records(path, session.warnings, digest):
        kind = record.get("type", "")
        payload_type = record.get('payload', {}).get('type', '') if isinstance(record.get('payload'), dict) else ''
        if any('compact' in str(value).lower() for value in (kind, record.get('subtype', ''), payload_type)) or kind == 'summary':
            builder.cached_results.clear()
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
                        if item.get('type') == 'text': builder.usage.text(builder.turn, item.get('text',''))
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
                        builder.result(item.get("tool_use_id"), item.get("content", ""), item.get("is_error"), line)
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
                        builder.usage.text(builder.turn, text)
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
                    builder.result(payload.get("call_id"), payload.get("output", ""), line=line)
                    codex_assistant_open = False
        builder.usage.observe(record, builder.turn)
    session.turn_usage = builder.usage.rows(session.actions)
    session.digest = digest.hexdigest()
    return session
