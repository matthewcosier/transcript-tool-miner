"""Conservative, configurable rules. Unknown and mixed shell actions are barriers."""
import json
import re
import shlex
from pathlib import Path

DETERMINISTIC = {"git_diff", "git_status", "git_log", "find_related_files", "read_file",
                 "run_tests", "inspect_test_results", "build", "lint"}
BUILTINS = [
    ("git_diff", r"^git\s+(?:-C\s+\S+\s+)?diff\b"),
    ("git_status", r"^git\s+(?:-C\s+\S+\s+)?status\b"),
    ("git_log", r"^git\s+(?:-C\s+\S+\s+)?(?:log|show)\b"),
    ("run_tests", r"^(?:(?:python[\d.]*\s+-m\s+)?(?:pytest|unittest)\b|(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:test|test:[\w-]+)\b|dotnet\s+test\b|cargo\s+test\b|go\s+test\b|(?:npx\s+)?(?:vitest|jest|playwright\s+test)\b)"),
    ("inspect_test_results", r"^(?:cat|head|tail|less|sed)\b.*(?:test[-_.]?results?|junit|pytest|test[-_.]?output)"),
    ("find_related_files", r"^(?:rg|grep|find|fd|ls)\b"),
    ("read_file", r"^(?:cat|head|tail|less|sed\s+-n)\b"),
    ("build", r"^(?:dotnet\s+build|cargo\s+build|(?:npm|pnpm|yarn)\s+(?:run\s+)?build)\b"),
    ("lint", r"^(?:ruff\s+check|eslint|(?:npm|pnpm|yarn)\s+(?:run\s+)?lint)\b"),
]
TOOL_NAMES = {"read": "read_file", "read_file": "read_file", "glob": "find_related_files",
              "grep": "find_related_files", "list_directory": "find_related_files"}
SHELL_TOOLS = {"bash", "shell", "shell_command", "exec_command", "run_shell_command"}


class Normalizer:
    def __init__(self, matchers=None):
        rules = []
        if matchers:
            raw = json.loads(Path(matchers).read_text())
            if not isinstance(raw, list):
                raise ValueError("Matcher file must be an array of {action, pattern} objects")
            for rule in raw:
                if not isinstance(rule, dict) or not re.fullmatch(r"[a-z][a-z0-9_]*", rule.get("action", "")):
                    raise ValueError("Matcher actions must be lower_case identifiers")
                rules.append((rule["action"], rule["pattern"]))
        self.rules = [(action, re.compile(pattern, re.I)) for action, pattern in rules + BUILTINS]

    def classify(self, tool, command):
        name = tool.split(".")[-1].lower()
        if name in TOOL_NAMES:
            return TOOL_NAMES[name]
        if name not in SHELL_TOOLS:
            return "unknown"
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()`")
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            return "unknown"
        # Compound commands can hide writes or unrelated actions. Keep them as barriers.
        if any(t in {";", "&&", "||", "|", "&", ">", ">>", "<", "(", ")", "`"} for t in tokens):
            return "unknown"
        if "\n" in command or "$(" in command or "`" in command:
            return "unknown"
        normalized = " ".join(tokens)
        normalized = re.sub(r"^(?:[A-Za-z_]\w*=\S+\s+)+", "", normalized)
        for action, pattern in self.rules:
            if pattern.search(normalized):
                return action
        return "unknown"
