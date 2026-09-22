"""Provider-independent records. Token counts are character-based estimates."""
from dataclasses import dataclass, field
import json
import math


def as_text(value):
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def estimate_tokens(value):
    return math.ceil(len(as_text(value)) / 4) if value else 0


@dataclass
class Action:
    tool: str
    command: str
    raw_arguments: str
    call_id: str
    line: int
    timestamp: str = ""
    request: int = 0
    turn: int = 0
    assistant_tokens: int = 0
    output_tokens: int = 0
    success: bool | None = None
    category: str = "unknown"
    operations: list[dict] = field(default_factory=list)
    linked_to: str = ""
    process_id: str = ""
    output_attribution: str = "unknown"
    compact_output_tokens: int | None = None
    reduction_reason: str = ""
    result_lines: list[int] = field(default_factory=list)
    poll_reduction_tokens: int = 0
    completion_observed: bool = False
    repeat_output_reduction_tokens: int = 0
    repeat_of: str = ""
    empty_poll: bool = False


@dataclass
class Session:
    session_id: str
    source: str
    project: str = "unknown"
    timestamp: str = ""
    actions: list[Action] = field(default_factory=list)
    requests: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    digest: str = ""
    turn_usage: list[tuple] = field(default_factory=list)
