"""Result metadata and a deterministic, explicitly scoped validation-log reducer."""
import json
import re
from .models import as_text, estimate_tokens
from .normalize import STATUS_OUTPUT


def result_metadata(value, error=None):
    """Return (success, process reference, complete, text); never infer success from prose."""
    text = as_text(value)
    data = value
    if isinstance(value, str):
        try: data = json.loads(value)
        except ValueError: pass
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and data[0].get('type') == 'text':
        return result_metadata(data[0].get('text', ''), error)
    if isinstance(data, dict):
        code = data.get('exit_code')
        process = data.get('session_id', data.get('cell_id', ''))
        if isinstance(process, float) and process.is_integer(): process = int(process)
        if code is not None:
            try: return int(code) == 0 and not error, str(process or ''), True, as_text(data.get('output', text))
            except (TypeError, ValueError): pass
        if process:
            return None, str(process), False, as_text(data.get('output', text))
        content = data.get('content')
        if isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict) and content[0].get('type') == 'text':
            return result_metadata(content[0].get('text', ''), error or data.get('isError'))
    # The wrapper can return one JSON result after its transport header. Only
    # accept a single complete JSON object; do not grep arbitrary logs for keys.
    if isinstance(data, str) and data.startswith(('Script completed', 'Script running')) and '\nOutput:\n' in data:
        body = data.split('\nOutput:\n', 1)[1].strip()
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict): return result_metadata(parsed, error)
        except ValueError: pass
    match = re.search(r'(?m)^(?:Process exited with code|Process exit code|Exit code)\s*:?\s*(-?\d+)\s*$', text, re.I)
    if match: return int(match.group(1)) == 0 and not error, '', True, text
    running = re.search(r'(?:[Ss]ession ID|cell ID)\s+([\w-]+)', text)
    if running:
        body = text.split('\nOutput:\n', 1)[1] if '\nOutput:\n' in text else text
        if re.fullmatch(r'(?:Script|Process) running with (?:cell|session) ID [\w-]+\.?', body.strip(), re.I): body = ''
        return None, running.group(1), False, body
    if error is not None: return not bool(error), '', True, text
    return None, '', False, text


def compact_validation(text, operation, success, complete):
    """Replay a 'status plus diagnostics, full log on demand' output contract.

    This is a measured text reduction under that contract, not measured billing
    savings or a guarantee that no caller will request the full log afterward.
    Failed/incomplete/ambiguous/batched results are never reduced.
    """
    if operation.get('label') not in STATUS_OUTPUT or success is not True or not complete:
        return None
    lines = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text).splitlines()
    # Tool transport success alone must not turn a visibly failed validation
    # into a passing summary. Keep such output in full, even if metadata conflicts.
    for line in lines:
        checked = re.sub(r'\b(?:0|no)\s+(?:failures?|failed|errors?)\b', '', line, flags=re.I)
        if re.search(r'\b(?:FAILED|FAILURE|ERROR|FATAL|TRACEBACK)\b', checked, re.I):
            return None
    nonempty = [i for i, line in enumerate(lines) if line.strip()]
    retained = set(nonempty[-12:])
    for i, line in enumerate(lines):
        if re.search(r'warning|error|fail|exception|fatal|traceback|security|deprecated|skip|xfail', line, re.I):
            retained.update(range(max(0, i - 2), min(len(lines), i + 3)))
    summary = {'command_completed': True, 'exit_code': 0,
               'diagnostics_and_tail': [lines[i] for i in sorted(retained)],
               'full_log_available_on_request': True,
               'contract': 'validation status, diagnostics and final output; full log retained locally'}
    size = estimate_tokens(json.dumps(summary, ensure_ascii=False))
    return {'tokens': size, 'summary': summary}
