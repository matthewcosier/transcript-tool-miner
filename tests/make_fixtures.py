"""Generate ONLY invented transcripts. Never reads a home directory or real history."""
import json
from pathlib import Path


def fixture(provider, suffix, project):
    rows = [{"_synthetic_fixture": True, "description": "Invented workflow. Contains no real transcript data."}]
    commands = ["git diff --name-only " + ("HEAD" if suffix == "a" else "HEAD~1"),
                "rg --files tests", "pytest tests/test_example.py"]
    outputs = ["src/example.py\n" * 100, "tests/test_example.py\n" * 100, "2 passed in 0.1s"]
    if provider == "claude":
        rows.append({"type": "user", "sessionId": "synthetic-claude-" + suffix, "cwd": project,
                     "message": {"role": "user", "content": "Check the changed example files."}})
        for i, (command, output) in enumerate(zip(commands, outputs)):
            rows.append({"type": "assistant", "uuid": f"synthetic-{suffix}-{i}",
                         "message": {"id": f"model-{i}", "role": "assistant", "content": [
                             {"type": "text", "text": "Inspect the synthetic result and continue."},
                             {"type": "tool_use", "id": f"call-{i}", "name": "Bash", "input": {"command": command}}]}})
            rows.append({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"call-{i}", "content": output, "is_error": False}]}})
    else:
        rows += [{"type": "session_meta", "payload": {"id": "synthetic-codex-" + suffix, "cwd": project}},
                 {"type": "event_msg", "payload": {"type": "user_message", "message": "Check the changed example files."}},
                 {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Check the changed example files."}]}}]
        for i, (command, output) in enumerate(zip(commands, outputs)):
            rows += [{"type": "response_item", "payload": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "Inspect the synthetic result and continue."}]}},
                     {"type": "response_item", "payload": {"type": "function_call", "name": "functions.exec_command", "call_id": f"call-{i}", "arguments": json.dumps({"cmd": command})}},
                     {"type": "response_item", "payload": {"type": "function_call_output", "call_id": f"call-{i}", "output": json.dumps({"exit_code": 0, "output": output})}}]
    return rows


if __name__ == "__main__":
    root = Path(__file__).parent / "fixtures"
    for provider in ("claude", "codex"):
        for suffix in ("a", "b"):
            (root / f"{provider}-{suffix}.json").write_text(json.dumps(fixture(provider, suffix, "/synthetic/repo-" + suffix), indent=2) + "\n")
