"""Exact contiguous 2..10 action mining with conservative nested-pattern reduction."""
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
from statistics import mean
from .normalize import DETERMINISTIC
from .storage import load_sessions


def boundary(sequence):
    if "git_diff" in sequence and "run_tests" in sequence:
        name = "validate_changed_files"
    elif "run_tests" in sequence:
        name = "run_targeted_validation"
    elif "find_related_files" in sequence and "read_file" in sequence:
        name = "collect_related_context"
    else:
        name = "_then_".join(sequence[:3])
    inputs = ["repository path"]
    if any(a.startswith("git_") for a in sequence):
        inputs.append("base revision or comparison scope")
    if "find_related_files" in sequence:
        inputs.append("file or search scope")
    if "run_tests" in sequence:
        inputs.extend(["test runner", "optional test scope"])
    return dict(name=name, deterministic_boundary=f"Actions 1 through {len(sequence)}; return a concise structured result",
                suggested_inputs=inputs, suggested_output=["actions executed", "affected files", "pass/fail/unknown", "concise failure details"],
                review_notes=["A repeated sequence is a proposal, not proof that its decisions are deterministic.",
                              "Review argument dependencies, execution order and repository-specific test selection before implementation."])


def mine(sessions, min_length=2, max_length=10, min_occurrences=2, min_sessions=2, result_budget=200, keep_nested=False):
    if not 2 <= min_length <= max_length <= 10:
        raise ValueError("Sequence lengths must satisfy 2 <= min <= max <= 10")
    if min_occurrences < 2 or min_sessions < 1 or result_budget < 0:
        raise ValueError("Require at least 2 occurrences, 1 session and a non-negative result budget")
    by_sequence = defaultdict(list)
    session_map = {session["id"]: session for session in sessions}
    for session in sessions:
        actions = session["actions"]
        for start, first in enumerate(actions):
            sequence = []
            for end in range(start, min(start + max_length, len(actions))):
                action = actions[end]
                if action["request"] != first["request"] or action["category"] == "unknown":
                    break
                sequence.append(action["category"])
                if len(sequence) >= min_length:
                    key = tuple(sequence)
                    spans = by_sequence[key]
                    # A pattern cannot claim overlapping occurrences in one session.
                    if not spans or spans[-1][0] != session["id"] or spans[-1][2] < start:
                        spans.append((session["id"], start, end))
    eligible = {}
    for sequence, spans in by_sequence.items():
        logical_sessions = {(session_map[s]["source"], session_map[s]["session_id"]) for s, _, _ in spans}
        if len(spans) >= min_occurrences and len(logical_sessions) >= min_sessions:
            eligible[sequence] = spans
    # Suppress a shorter candidate only if a longer candidate covers every one of
    # its exact occurrences. More broadly recurring short patterns survive.
    suppressed = set()
    if not keep_nested:
        for sequence, spans in eligible.items():
            for length in range(min_length, len(sequence)):
                for offset in range(len(sequence) - length + 1):
                    sub = sequence[offset:offset + length]
                    sub_spans = eligible.get(sub)
                    if sub_spans and len(sub_spans) == len(spans):
                        projected = {(s, start + offset, start + offset + length - 1) for s, start, _ in spans}
                        if projected == set(sub_spans):
                            suppressed.add(sub)
    results = []
    for sequence, spans in eligible.items():
        if sequence in suppressed:
            continue
        stats = []
        projects = set()
        distinct = set()
        outcomes = {"success": 0, "failure": 0, "unknown": 0}
        for session_id, start, end in spans:
            session = session_map[session_id]
            actions = session["actions"][start:end + 1]
            projects.add(session["project"])
            distinct.add((session["source"], session["session_id"]))
            # The terminal tool output is retained for the caller, so it is not
            # counted as intermediate. Visible model work after the first call is.
            output = sum(a["output_tokens"] for a in actions)
            intermediate = sum(a["output_tokens"] for a in actions[:-1]) + sum(a["assistant_tokens"] for a in actions[1:])
            statuses = [a["success"] for a in actions]
            status = "failure" if False in statuses else "success" if all(s is True for s in statuses) else "unknown"
            outcomes[status] += 1
            stats.append((len({a["turn"] for a in actions}), intermediate, output, max(0, intermediate - result_budget)))
        turns, tokens, output, avoidable = (mean(row[i] for row in stats) for i in range(4))
        repeatability = sum(action in DETERMINISTIC for action in sequence) / len(sequence)
        candidate = dict(id=hashlib.sha256("|".join(sequence).encode()).hexdigest()[:12],
                         sequence=list(sequence), occurrences=len(spans), session_count=len(distinct),
                         projects=sorted(projects), project_count=len(projects - {"unknown"}),
                         average_model_turns=round(turns, 2), average_intermediate_tokens=round(tokens, 2),
                         average_tool_output_tokens=round(output, 2), average_avoidable_tokens=round(avoidable, 2),
                         estimated_historical_avoidable_tokens=round(avoidable * len(spans)), outcomes=outcomes,
                         score=round(len(spans) * avoidable * repeatability, 2),
                         score_components=dict(frequency=len(spans), estimated_avoidable_tokens_per_occurrence=round(avoidable, 2),
                                               repeatability=round(repeatability, 3), concise_result_budget=result_budget),
                         estimates_note="Approximate visible-text tokens (ceil(characters / 4)), not provider billing or measured savings. Candidates overlap; do not sum their estimates.",
                         repeatability_note="Share of actions in the built-in deterministic rule set; a heuristic, not a measured probability.",
                         **boundary(sequence))
        results.append((candidate, spans))
    return sorted(results, key=lambda row: (-row[0]["score"], row[0]["id"]))


def analyse(db, **options):
    result = mine(list(load_sessions(db)), **options)
    with db:
        db.execute("DELETE FROM patterns")
        for candidate, spans in result:
            db.execute("INSERT INTO patterns(id,score,data) VALUES(?,?,?)", (candidate["id"], candidate["score"], json.dumps(candidate)))
            db.executemany("INSERT INTO occurrences(pattern,session,start,end) VALUES(?,?,?,?)",
                           [(candidate["id"], *span) for span in spans])
        db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('analysis',?)", (json.dumps({"timestamp": datetime.now(timezone.utc).isoformat(), "options": options}),))
    return [candidate for candidate, _ in result]
