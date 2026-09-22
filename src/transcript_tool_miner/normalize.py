"""Static, conservative operation extraction. Never evaluates transcript code."""
from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import shlex

SHELL_TOOLS = {'bash', 'shell', 'shell_command', 'exec_command', 'run_shell_command'}
POLL_TOOLS = {'wait', 'write_stdin', 'taskoutput'}
VALIDATION = {'run_tests', 'build', 'lint', 'git_diff_check'}
REPOSITORY = {'git_status', 'git_diff_check', 'git_diff_names', 'git_diff_stat', 'git_log'}
STATUS_OUTPUT = VALIDATION | {'github_pr_checks', 'github_ci_status'}
DETERMINISTIC = VALIDATION | REPOSITORY | {'read_file', 'find_related_files', 'search_text', 'github_pr_checks', 'github_ci_status'}


def hash_text(text):
    return hashlib.sha256(text.encode('utf-8', errors='replace')).hexdigest()[:24]


@dataclass(frozen=True)
class Operation:
    kind: str
    variant: str = ''
    argv: tuple = ()
    cwd: str = ''
    command: str = ''
    process_id: str = ''

    @property
    def label(self):
        return f'{self.kind}_{self.variant}' if self.kind == 'git_diff' and self.variant else self.kind

    def record(self):
        value = asdict(self)
        value['label'] = self.label
        # Input values are parameters; executable, operation, flags and runner
        # remain part of the interface. Exact argument recurrence is separate.
        interface = [self.argv[0]] if self.argv else []
        for index, arg in enumerate(self.argv[1:], 1):
            if arg.startswith('-'):
                interface.append(arg.split('=', 1)[0])
            elif index == 1 and self.argv[0] in {'git', 'gh', 'dotnet', 'cargo', 'go', 'npm', 'pnpm', 'yarn', 'bun'}:
                interface.append(arg)
            else:
                interface.append('<argument>')
        value['template'] = hash_text(json.dumps([self.label, interface]))
        value['exact'] = hash_text(json.dumps([self.cwd, self.argv, self.command]))
        return value


def literal_string(source, start):
    quote = source[start]
    out = []; i = start + 1
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v', '\\': '\\', '/': '/', '"': '"', "'": "'", '`': '`'}
    while i < len(source):
        char = source[i]
        if char == quote:
            return ''.join(out), i + 1
        if quote == '`' and source.startswith('${', i):
            raise ValueError('Dynamic template')
        if char == '\\':
            i += 1
            if i >= len(source): break
            char = source[i]
            if char in escapes: out.append(escapes[char])
            elif char == '\n': pass
            elif char in {'u', 'x'}:
                size = 4 if char == 'u' else 2
                out.append(chr(int(source[i + 1:i + 1 + size], 16))); i += size
            else: raise ValueError('Unsupported string escape')
        else:
            out.append(char)
        i += 1
    raise ValueError('Unclosed string')


def js_tokens(source):
    tokens = []; i = 0
    while i < len(source):
        if source[i].isspace(): i += 1; continue
        if source.startswith('//', i):
            end = source.find('\n', i); i = len(source) if end < 0 else end + 1; continue
        if source.startswith('/*', i):
            end = source.find('*/', i + 2)
            if end < 0: raise ValueError('Unclosed comment')
            i = end + 2; continue
        if source[i] in {'"', "'", '`'}:
            value, i = literal_string(source, i); tokens.append(('string', value)); continue
        match = re.match(r'[A-Za-z_$][\w$]*|\d+(?:\.\d+)?', source[i:])
        if match:
            tokens.append(('word', match.group())); i += len(match.group())
        else:
            tokens.append(('punct', source[i])); i += 1
    return tokens


def literal_value(tokens, index):
    kind, value = tokens[index]
    if kind == 'string': return value, index + 1
    if value in {'true', 'false', 'null'}: return {'true': True, 'false': False, 'null': None}[value], index + 1
    if kind == 'word' and re.fullmatch(r'\d+(?:\.\d+)?', value): return float(value), index + 1
    if value == '[':
        result = []; index += 1
        while tokens[index][1] != ']':
            item, index = literal_value(tokens, index); result.append(item)
            if tokens[index][1] == ',': index += 1
            elif tokens[index][1] != ']': raise ValueError('Dynamic array')
        return result, index + 1
    if value == '{':
        result = {}; index += 1
        while tokens[index][1] != '}':
            key = tokens[index]
            if key[0] not in {'word', 'string'} or tokens[index + 1][1] != ':': raise ValueError('Dynamic object')
            item, index = literal_value(tokens, index + 2); result[key[1]] = item
            if tokens[index][1] == ',': index += 1
            elif tokens[index][1] != '}': raise ValueError('Dynamic object')
        return result, index + 1
    raise ValueError('Non-literal argument')


def wrapped_calls(source):
    """Accept literal tool calls in straight-line wrappers, never script control flow."""
    try:
        tokens = js_tokens(source)
        tool_sites = [i for i in range(len(tokens)-3) if tokens[i] == ('word','tools') and tokens[i+1][1] == '.' and tokens[i+3][1] == '(']
        if not tool_sites: return None
        last_tool = max(tool_sites)
        for i, (kind, value) in enumerate(tokens):
            if kind == 'word' and value in {'function','eval','fetch','require','import'}: return None
            if kind == 'word' and value in {'if','for','while','switch'} and i < last_tool: return None
            if i < last_tool and value == '=' and i+1 < len(tokens) and tokens[i+1][1] == '>': return None
        calls = []; i = 0
        while i < len(tokens):
            if tokens[i] == ('word', 'tools') and i + 3 < len(tokens) and tokens[i + 1][1] == '.' and tokens[i + 3][1] == '(':
                name = tokens[i + 2][1]
                try:
                    args, end = literal_value(tokens, i + 4)
                    if tokens[end][1] != ')' or not isinstance(args, dict): raise ValueError('Tool arguments')
                    calls.append((name, args)); i = end + 1; continue
                except (ValueError, IndexError):
                    return None
            # Unknown executable helpers may hide additional work. Rendering and
            # Promise aggregation are the only recognised non-tool call sites.
            if tokens[i][1] == '(' and i and tokens[i - 1][0] == 'word':
                if tokens[i - 1][1] not in {'text', 'notify', 'all', 'allSettled', 'stringify'} and not (i > last_tool and tokens[i - 1][1] in {'if', 'for', 'map', 'forEach'}):
                    return None
            i += 1
        return calls or None
    except (ValueError, IndexError, OverflowError):
        return None


def split_shell(command):
    if '<<' in command or '$(' in command or '`' in command:
        return None
    result = []; start = 0; quote = None; escaped = False; connector = ''; i = 0
    while i < len(command):
        char = command[i]
        if escaped: escaped = False; i += 1; continue
        if char == '\\' and quote != "'": escaped = True; i += 1; continue
        if quote:
            if char == quote: quote = None
            i += 1; continue
        if char in {"'", '"'}: quote = char; i += 1; continue
        if char in {'>', '<', '(', ')'}: return None
        if char in {';', '&', '|', '\n'}:
            result.append((command[start:i], connector))
            doubled = i + 1 < len(command) and command[i + 1] == char and char in {'&', '|'}
            connector = char * (2 if doubled else 1)
            i += 2 if doubled else 1; start = i; continue
        i += 1
    if quote: return None
    result.append((command[start:], connector))
    return result


def command_arg(args):
    command = args.get('command', args.get('cmd', args.get('input', '')))
    if isinstance(command, list):
        if len(command) >= 3 and command[1] in {'-c', '-lc'}: return str(command[2])
        return shlex.join(map(str, command))
    return command if isinstance(command, str) else ''


class Normalizer:
    def __init__(self, matchers=None):
        self.rules = []; self.cache = OrderedDict()
        if matchers:
            raw = json.loads(Path(matchers).read_text())
            if not isinstance(raw, list): raise ValueError('Matchers must be an array')
            for rule in raw:
                if not isinstance(rule, dict) or not re.fullmatch(r'[a-z][a-z0-9_]*', rule.get('action', '')):
                    raise ValueError('Matcher actions must be lower_case identifiers')
                self.rules.append((rule['action'], re.compile(rule['pattern'], re.I)))

    def classify(self, tool, command):
        ops = self.operations(tool, {'command': command})
        return ops[0].kind if len(ops) == 1 else 'unknown'

    def operations(self, tool, args, cwd=''):
        name = tool.split('.')[-1].lower()
        command = command_arg(args)
        cwd = str(args.get('workdir', args.get('cwd', cwd)) or '')
        if name in {'read', 'read_file'}:
            return [Operation('read_file', argv=(name,), cwd=cwd, command=json.dumps(args))]
        if name in {'glob', 'grep', 'list_directory'}:
            return [Operation('search_text' if name == 'grep' else 'find_related_files', argv=(name,), cwd=cwd, command=json.dumps(args))]
        if name in POLL_TOOLS:
            if args.get('chars') or args.get('terminate'):
                return [Operation('unknown')]
            reference = args.get('session_id', args.get('cell_id', args.get('task_id', '')))
            if isinstance(reference, float) and reference.is_integer(): reference = int(reference)
            return [Operation('poll', process_id=str(reference))]
        if name in {'exec', 'run'}:
            source = next((args[k] for k in ('code', 'source', 'script', 'input', 'command') if isinstance(args.get(k), str)), '')
            calls = wrapped_calls(source)
            if not calls: return [Operation('unknown')]
            ops = []
            for child, values in calls:
                if child.split('.')[-1].lower() in {'exec', 'run'}: ops.append(Operation('unknown'))
                else: ops.extend(self.operations(child, values, cwd))
            return ops or [Operation('unknown')]
        if name not in SHELL_TOOLS: return [Operation('unknown')]
        key = (command, cwd)
        if len(command) <= 8192 and key in self.cache:
            self.cache.move_to_end(key); return list(self.cache[key])
        result = self.shell(command, cwd)
        if len(command) <= 8192:
            self.cache[key] = tuple(result)
            if len(self.cache) > 4096: self.cache.popitem(last=False)
        return result

    def shell(self, command, cwd):
        pieces = split_shell(command)
        if pieces is None: return [Operation('unknown', cwd=cwd, command=command)]
        operations = []
        for text, connector in pieces:
            if connector in {'||', '&'}: operations.append(Operation('unknown'))
            try: tokens = shlex.split(text, comments=True)
            except ValueError: return [Operation('unknown', command=command)]
            while tokens and (re.match(r'^\w+=', tokens[0]) or tokens[0] == 'env'): tokens = tokens[1:]
            if not tokens: continue
            exe = Path(tokens[0]).name.lower()
            if exe == 'cd' and len(tokens) == 2:
                target = tokens[1]
                cwd = str(Path(cwd) / target) if cwd and not Path(target).is_absolute() else target
                continue
            if exe in {'pwd', 'echo', 'printf', 'true', 'date'}: continue
            if connector == '|':
                if exe in {'head', 'tail', 'sort', 'uniq', 'wc', 'cut'}: continue
                # An unrecognised pipe consumer can mutate or reinterpret output.
                operations.append(Operation('unknown')); continue
            if exe in {'timeout', 'gtimeout'} and len(tokens) > 2:
                tokens = tokens[2:]; exe = Path(tokens[0]).name.lower()
            argv = [exe, *tokens[1:]]; kind = 'unknown'; variant = ''
            if exe == 'git':
                args = argv[1:]
                while len(args) >= 2 and args[0] in {'-C', '-c', '--git-dir', '--work-tree'}:
                    if args[0] == '-C': cwd = str(Path(cwd) / args[1]) if cwd else args[1]
                    args = args[2:]
                verb = args[0] if args else ''
                if verb == 'diff':
                    kind = 'git_diff'
                    variant = 'check' if '--check' in args else 'names' if any(a in args for a in ('--name-only', '--name-status')) else 'stat' if any(a in args for a in ('--stat', '--numstat', '--shortstat')) else 'patch'
                else: kind = {'status': 'git_status', 'log': 'git_log', 'show': 'read_file', 'ls-files': 'find_related_files', 'rev-parse': 'git_status'}.get(verb, 'unknown')
            elif exe == 'gh':
                if argv[1:3] == ['pr', 'checks']: kind = 'github_pr_checks'
                elif argv[1:3] in (['run', 'view'], ['run', 'list'], ['run', 'watch']): kind = 'github_ci_logs' if any('--log' in a for a in argv) else 'github_ci_status'
                elif argv[1:3] in (['pr', 'view'], ['pr', 'diff'], ['pr', 'list']): kind = 'read_file'
            elif exe in {'rg', 'grep'}:
                kind = 'find_related_files' if '--files' in argv else 'search_text'
            elif exe in {'ls', 'find', 'fd'}: kind = 'find_related_files'
            elif exe in {'cat', 'head', 'tail', 'less'} or (exe == 'sed' and '-n' in argv): kind = 'read_file'
            elif exe in {'pytest', 'jest', 'vitest'} or (re.fullmatch(r'python[\d.]*', exe) and len(argv) > 2 and argv[1] == '-m' and argv[2] in {'pytest', 'unittest'}): kind = 'run_tests'
            elif exe in {'dotnet', 'cargo', 'go'} and len(argv) > 1:
                kind = {'test': 'run_tests', 'build': 'build', 'clippy': 'lint'}.get(argv[1], 'unknown')
            elif exe in {'npm', 'pnpm', 'yarn', 'bun', 'npx'}:
                args = argv[1:]; args = args[1:] if args and args[0] == 'run' else args
                if args:
                    if args[0] in {'test', 'jest', 'vitest'} or args[0].startswith('test:') or args[:2] == ['playwright', 'test']: kind = 'run_tests'
                    elif args[0] in {'build', 'lint'}: kind = args[0]
            elif exe in {'eslint'} or (exe == 'ruff' and argv[1:2] == ['check']): kind = 'lint'
            if any(a in argv for a in ('--collect-only', '--list-tests', '--listTests', '--help', '--version')):
                kind = 'inspection'
            for action, pattern in self.rules:
                if pattern.search(shlex.join(argv)): kind = action; variant = ''; break
            operations.append(Operation(kind, variant, tuple(argv), cwd, text.strip()))
        return operations or [Operation('unknown', cwd=cwd, command=command)]
